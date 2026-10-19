"""Parse XPENG GDPR data-export zips into drivable/chargeable records.

The export is one row per second per dataset, in three CSV members:
`driving_operation_di` (speed, gear, odometer), `driving_power_energy_di` (SoC,
range, pack volts/amps, charge power) and `driving_status_di` (TPMS, doors).
There is no GPS in it, so this parser produces drives, charging sessions,
hourly tyre readings and online/asleep spans - never positions.

Several files are usually imported together because each covers a rolling
30-day window. The same drive or charge therefore often appears in two files.
`merge_exports/1` resolves that:

* two records that overlap -> the longer one wins (the same drive described
  twice);
* two records that touch at a window boundary -> they are stitched, and the
  integrated distance/energy is summed (each half measured its own seconds);
* duplicated samples are never double counted.

Values are XPENG's own sentinels and must be dropped, not clamped; the rules are
the ones measured in the export field reference.
"""

from __future__ import annotations

import bisect
import csv
import heapq
import io
import logging
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

_LOGGER = logging.getLogger("xpengmate.export_parser")

UTC = timezone.utc

#: Signals and their *invalid* markers, from the measured export field reference.
SPEED_SNA = 255.0
SOC_SNA = 255.0
RANGE_SNA = 1600.0
VOLT_SNA = 1023.0
CURRENT_SNA = 1000.0
TEMP_SNA = 215.0
CHARGE_POWER_SNA = 650.0
ODOMETER_MAX = 2_000_000.0

#: Integration and trip rules.
MAX_STEP_SECONDS = 5.0
DRIVE_SPEED_FLOOR_KMH = 0.5
MAX_DRIVE_STOP_SECONDS = 300
MIN_DRIVE_SECONDS = 60
MIN_DRIVE_KM = 0.2
CHARGE_POWER_FLOOR_KW = 0.1
MAX_CHARGE_GAP_SECONDS = 120
MIN_CHARGE_SECONDS = 60
DC_POWER_THRESHOLD_KW = 20.0
ONLINE_GAP_SECONDS = 300
TMPS_MIN_KPA, TMPS_MAX_KPA = 100.0, 500.0

OPERATION_FIELDS = {
    "speed": "esp_vehspd",
    "odometer": "cdcu_totalodometer",
    "gear": "ldcu_currentgearlev",
}
ENERGY_FIELDS = {
    "soc": "ldcu_bms_soc_disp",
    "range": "ldcu_dstbatdisp_dynamic",
    "volt": "bms_battvolt",
    "amp": "bms_battcurr",
    "charge_power": "ldcu_chrgpwr",
    "temp_max": "bms_batttempmax_gb",
    "temp_min": "bms_battempmin_gb",
}
# The export spells the minimum temperature column with a different name in
# different releases; both are tried.
ENERGY_ALIASES = {"temp_min": ("bms_batttempmin_gb", "bms_battempmin_gb")}
STATUS_FIELDS = {
    "tpms_fl": "ldcu_tpmsprfl",
    "tpms_fr": "ldcu_tpmsprfr",
    "tpms_rl": "ldcu_tpmsprrl",
    "tpms_rr": "ldcu_tpmsprrr",
}


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_speed(value: float | None) -> float | None:
    if value is None or value < 0 or value >= SPEED_SNA:
        return None
    return value


def _clean_soc(value: float | None) -> int | None:
    if value is None or value < 0 or value >= SOC_SNA:
        return None
    return int(round(value))


def _clean_range(value: float | None) -> float | None:
    if value is None or value <= 0 or value > RANGE_SNA:
        return None
    return round(value, 1)


def _clean_volt(value: float | None) -> float | None:
    if value is None or value <= 0 or value >= VOLT_SNA:
        return None
    return value


def _clean_current(value: float | None) -> float | None:
    if value is None or abs(value) >= CURRENT_SNA:
        return None
    return value


def _clean_charge_power(value: float | None) -> float | None:
    if value is None or value < 0 or value >= CHARGE_POWER_SNA:
        return None
    return value


def _clean_temp(value: float | None) -> float | None:
    if value is None or value <= 0 or value >= TEMP_SNA:
        return None
    return value


def _clean_tpms(value: float | None) -> float | None:
    if value is None or value < TMPS_MIN_KPA or value > TMPS_MAX_KPA:
        return None
    return round(value / 100.0, 2)


def _clean_odometer(value: float | None) -> float | None:
    if value is None or value <= 0 or value > ODOMETER_MAX:
        return None
    return round(value, 3)


# -- zip reading ---------------------------------------------


def _members(archive: zipfile.ZipFile, suffix: str) -> list[str]:
    """Base file and numbered parts, in time order."""
    names = [n for n in archive.namelist() if suffix in n and n.endswith(".csv")]

    def part(name: str) -> int:
        stem = Path(name).stem
        if "_part" not in stem:
            return 0
        try:
            return int(stem.rsplit("_part", 1)[1])
        except (ValueError, IndexError):
            return 99

    return sorted(names, key=part)


def _stream(archive: zipfile.ZipFile, suffix: str, fields: dict[str, Any]) -> Iterator[tuple[int, dict]]:
    for name in _members(archive, suffix):
        with archive.open(name) as raw:
            reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig", newline=""))
            for row in reader:
                try:
                    timer = int(float(row.get("timer") or 0))
                except (TypeError, ValueError):
                    continue
                values: dict[str, Any] = {"vin": row.get("vin") or ""}
                for key, column in fields.items():
                    aliases = ENERGY_ALIASES.get(key) if isinstance(fields, dict) else None
                    candidates = aliases or (column,)
                    for candidate in candidates:
                        if candidate in row:
                            values[key] = _num(row.get(candidate))
                            break
                    else:
                        values[key] = None
                yield timer, values


def _merged_samples(zip_path: str) -> Iterator[tuple[int, dict]]:
    """One dict per second, from the three datasets joined on `timer`."""
    with zipfile.ZipFile(zip_path) as archive:
        streams = [
            _stream(archive, "driving_operation_di", OPERATION_FIELDS),
            _stream(archive, "driving_power_energy_di", ENERGY_FIELDS),
            _stream(archive, "driving_status_di", STATUS_FIELDS),
        ]
        heap: list[tuple[int, int, dict]] = []
        for index, stream in enumerate(streams):
            first = next(stream, None)
            if first is not None:
                heapq.heappush(heap, (first[0], index, first[1]))
        current_timer: int | None = None
        current: dict[str, Any] = {}
        while heap:
            timer, index, values = heapq.heappop(heap)
            successor = next(streams[index], None)
            if successor is not None:
                heapq.heappush(heap, (successor[0], index, successor[1]))
            if timer != current_timer:
                if current_timer is not None:
                    yield current_timer, current
                current_timer, current = timer, dict(values)
            else:
                current.update(values)
        if current_timer is not None:
            yield current_timer, current


# -- records --------------------------------------------------


@dataclass
class Drive:
    start: int
    end: int
    distance_km: float
    odometer_start: float | None = None
    odometer_end: float | None = None
    soc_start: int | None = None
    soc_end: int | None = None
    speed_max: float | None = None
    power_max: float | None = None
    power_min: float | None = None
    energy_kwh: float = 0.0
    range_start: float | None = None
    range_end: float | None = None

    @property
    def duration_seconds(self) -> int:
        return max(0, self.end - self.start)

    def key(self) -> str:
        return f"export-drive:{self.start}"


@dataclass
class Charge:
    start: int
    end: int
    soc_start: int | None = None
    soc_end: int | None = None
    range_start: float | None = None
    range_end: float | None = None
    energy_input_kwh: float = 0.0
    energy_dc_kwh: float = 0.0
    peak_power_kw: float = 0.0
    power_sum: float = 0.0
    power_samples: int = 0
    temp_min: float | None = None
    temp_max: float | None = None

    @property
    def duration_seconds(self) -> int:
        return max(0, self.end - self.start)

    @property
    def mean_power_kw(self) -> float:
        return round(self.power_sum / self.power_samples, 2) if self.power_samples else 0.0

    @property
    def is_dc_fast(self) -> bool:
        return self.peak_power_kw >= DC_POWER_THRESHOLD_KW

    def key(self) -> str:
        return f"export-charge:{self.start}"


@dataclass
class ExportData:
    path: str
    vin: str = ""
    timer_start: int | None = None
    timer_end: int | None = None
    samples: int = 0
    drives: list[Drive] = field(default_factory=list)
    charges: list[Charge] = field(default_factory=list)
    tyres: dict[int, tuple] = field(default_factory=dict)
    online: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class MergedExport:
    vin: str = ""
    drives: list[Drive] = field(default_factory=list)
    charges: list[Charge] = field(default_factory=list)
    tyres: dict[int, tuple] = field(default_factory=dict)
    online: list[tuple[int, int]] = field(default_factory=list)
    files: list[ExportData] = field(default_factory=list)

    @property
    def timer_start(self) -> int | None:
        starts = [f.timer_start for f in self.files if f.timer_start]
        return min(starts) if starts else None

    @property
    def timer_end(self) -> int | None:
        ends = [f.timer_end for f in self.files if f.timer_end]
        return max(ends) if ends else None


def parse_zip(zip_path: str) -> ExportData:
    data = ExportData(path=zip_path)
    drive: Drive | None = None
    drive_last_moving: int | None = None
    drive_last_timer: int | None = None
    drive_energy = 0.0

    charge: Charge | None = None
    charge_last_timer: int | None = None
    charge_last_soc: int | None = None
    charge_energy_input = 0.0
    charge_energy_dc = 0.0

    online_start: int | None = None
    online_last: int | None = None
    last_timer: int | None = None
    last_tyre_hour: int | None = None
    tyre_values: dict[str, float | None] = {}

    for timer, values in _merged_samples(zip_path):
        if data.timer_start is None:
            data.timer_start = timer
        data.timer_end = timer
        data.samples += 1
        if not data.vin and values.get("vin"):
            data.vin = str(values["vin"])

        # timestamps are the authoritative clock; dt drives every integral
        dt = 0.0
        if last_timer is not None:
            delta = timer - last_timer
            if 0 < delta <= MAX_STEP_SECONDS:
                dt = float(delta)
        last_timer = timer

        # -- online/asleep coverage -----------------------------
        if online_start is None:
            online_start = timer
        elif online_last is not None and timer - online_last > ONLINE_GAP_SECONDS:
            data.online.append((online_start, online_last))
            online_start = timer
        online_last = timer

        speed = _clean_speed(values.get("speed"))
        odometer = _clean_odometer(values.get("odometer"))
        soc = _clean_soc(values.get("soc"))
        range_km = _clean_range(values.get("range"))
        volt = _clean_volt(values.get("volt"))
        amp = _clean_current(values.get("amp"))
        charge_power = _clean_charge_power(values.get("charge_power"))
        power_kw = None
        if volt is not None and amp is not None:
            power_kw = volt * amp / 1000.0

        # -- drives ---------------------------------------------
        driving = speed is not None and speed > DRIVE_SPEED_FLOOR_KMH
        if driving and drive is None:
            drive = Drive(
                start=timer,
                end=timer,
                distance_km=0.0,
                odometer_start=odometer,
                soc_start=soc,
                speed_max=speed,
                power_max=power_kw,
                power_min=power_kw,
                range_start=range_km,
            )
            drive_last_moving = timer
            drive_last_timer = timer
            drive_energy = 0.0
        elif driving and drive is not None:
            if drive_last_timer is not None and dt:
                drive.distance_km += speed * dt / 3600.0
                if power_kw is not None:
                    drive_energy += power_kw * dt / 3600.0
            drive.end = timer
            drive_last_moving = timer
            drive_last_timer = timer
            if speed > (drive.speed_max or 0):
                drive.speed_max = speed
            if power_kw is not None:
                drive.power_max = power_kw if drive.power_max is None else max(drive.power_max, power_kw)
                drive.power_min = power_kw if drive.power_min is None else min(drive.power_min, power_kw)
            if drive.odometer_start is None and odometer is not None:
                drive.odometer_start = odometer
            if drive.soc_start is None and soc is not None:
                drive.soc_start = soc
            if drive.range_start is None and range_km is not None:
                drive.range_start = range_km
            if range_km is not None:
                drive.range_end = range_km
            drive.odometer_end = odometer or drive.odometer_end
            drive.soc_end = soc if soc is not None else drive.soc_end
        elif drive is not None:
            stopped_for = timer - (drive_last_moving or timer)
            if stopped_for > MAX_DRIVE_STOP_SECONDS:
                drive.end = drive_last_moving or drive.end
                drive.energy_kwh = round(drive_energy, 2)
                drive.distance_km = round(drive.distance_km, 2)
                if (
                    drive.duration_seconds >= MIN_DRIVE_SECONDS
                    and drive.distance_km >= MIN_DRIVE_KM
                ):
                    data.drives.append(drive)
                drive = None
                drive_last_timer = None

        # -- charging -------------------------------------------
        charging = charge_power is not None and charge_power > CHARGE_POWER_FLOOR_KW
        if charging and charge is None:
            charge = Charge(
                start=timer,
                end=timer,
                soc_start=soc,
                range_start=range_km,
                temp_min=_clean_temp(values.get("temp_min")),
                temp_max=_clean_temp(values.get("temp_max")),
            )
            charge_last_timer = timer
            charge_last_soc = soc
            charge_energy_input = 0.0
            charge_energy_dc = 0.0
        if charging and charge is not None:
            dt_charge = 0.0
            if charge_last_timer is not None:
                delta = timer - charge_last_timer
                if 0 < delta <= MAX_STEP_SECONDS:
                    dt_charge = float(delta)
            charge.end = timer
            charge_last_timer = timer
            charge.power_sum += charge_power or 0.0
            charge.power_samples += 1
            charge.peak_power_kw = max(charge.peak_power_kw, charge_power or 0.0)
            if dt_charge:
                charge_energy_input += (charge_power or 0.0) * dt_charge / 3600.0
                if volt is not None and amp is not None:
                    charge_energy_dc += max(0.0, volt * abs(amp)) * dt_charge / 3_600_000.0
            if charge.soc_start is None and soc is not None:
                charge.soc_start = soc
            if charge.range_start is None and range_km is not None:
                charge.range_start = range_km
            charge.soc_end = soc if soc is not None else charge.soc_end
            charge.range_end = range_km if range_km is not None else charge.range_end
            charge_last_soc = soc if soc is not None else charge_last_soc
            temp_min = _clean_temp(values.get("temp_min"))
            temp_max = _clean_temp(values.get("temp_max"))
            if temp_min is not None:
                charge.temp_min = temp_min if charge.temp_min is None else min(charge.temp_min, temp_min)
            if temp_max is not None:
                charge.temp_max = temp_max if charge.temp_max is None else max(charge.temp_max, temp_max)
        elif charge is not None:
            gap = timer - (charge_last_timer or timer)
            soc_falling = (
                soc is not None and charge_last_soc is not None and soc < charge_last_soc
            )
            if gap <= MAX_CHARGE_GAP_SECONDS and not soc_falling:
                # A mid-session blip in charging power, not the end of the charge.
                pass
            else:
                charge.end = charge_last_timer or charge.end
                charge.energy_input_kwh = round(charge_energy_input, 2)
                charge.energy_dc_kwh = round(charge_energy_dc, 2)
                if charge.duration_seconds >= MIN_CHARGE_SECONDS:
                    data.charges.append(charge)
                charge = None
                charge_last_timer = None
                charge_last_soc = None

        # -- hourly tyre readings -------------------------------
        hour = timer // 3600
        wheel_values = {key: _clean_tpms(values.get(key)) for key in STATUS_FIELDS}
        if any(value is not None for value in wheel_values.values()):
            if last_tyre_hour is None:
                last_tyre_hour = hour
                tyre_values = dict(wheel_values)
            elif hour != last_tyre_hour:
                data.tyres[last_tyre_hour] = (
                    tyre_values.get("tpms_fl"),
                    tyre_values.get("tpms_fr"),
                    tyre_values.get("tpms_rl"),
                    tyre_values.get("tpms_rr"),
                )
                last_tyre_hour = hour
                tyre_values = dict(wheel_values)
            else:
                tyre_values.update({k: v for k, v in wheel_values.items() if v is not None})

    # close anything still open at end of file
    if drive is not None:
        drive.end = drive_last_moving or drive.end
        drive.energy_kwh = round(drive_energy, 2)
        drive.distance_km = round(drive.distance_km, 2)
        if drive.duration_seconds >= MIN_DRIVE_SECONDS and drive.distance_km >= MIN_DRIVE_KM:
            data.drives.append(drive)
    if charge is not None:
        charge.end = charge_last_timer or charge.end
        charge.energy_input_kwh = round(charge_energy_input, 2)
        charge.energy_dc_kwh = round(charge_energy_dc, 2)
        if charge.duration_seconds >= MIN_CHARGE_SECONDS:
            data.charges.append(charge)
    if last_tyre_hour is not None:
        data.tyres[last_tyre_hour] = (
            tyre_values.get("tpms_fl"),
            tyre_values.get("tpms_fr"),
            tyre_values.get("tpms_rl"),
            tyre_values.get("tpms_rr"),
        )
    if online_start is not None and online_last is not None:
        data.online.append((online_start, online_last))

    _LOGGER.info(
        "%s: %d samples, %d drives, %d charges, %d tyre hours",
        Path(zip_path).name,
        data.samples,
        len(data.drives),
        len(data.charges),
        len(data.tyres),
    )
    return data


# -- merging overlapping windows ------------------------------

#: A gap this small between two records means they are two halves of one event
#: cut at an export window boundary.
ADJACENT_SECONDS = 120


def merge_exports(files: list[ExportData]) -> MergedExport:
    merged = MergedExport(files=files)
    for item in files:
        if not merged.vin and item.vin:
            merged.vin = item.vin
    merged.drives = _merge_drives([d for f in files for d in f.drives])
    merged.charges = _merge_charges([c for f in files for c in f.charges])
    for item in files:
        merged.tyres.update(item.tyres)
    spans: list[tuple[int, int]] = []
    for item in files:
        spans.extend(item.online)
    merged.online = _merge_spans(spans, gap=ONLINE_GAP_SECONDS)
    return merged


def _merge_drives(drives: list[Drive]) -> list[Drive]:
    drives = sorted(drives, key=lambda item: item.start)
    out: list[Drive] = []
    for drive in drives:
        if not out:
            out.append(drive)
            continue
        previous = out[-1]
        overlap = previous.end - drive.start
        if overlap > 0:
            # The same drive described by two windows: keep the longer record.
            if drive.duration_seconds > previous.duration_seconds:
                out[-1] = drive
            continue
        if drive.start - previous.end <= ADJACENT_SECONDS:
            # Two halves cut at a window boundary: stitch and sum the integrals.
            previous.end = drive.end
            previous.distance_km = round(previous.distance_km + drive.distance_km, 2)
            previous.energy_kwh = round(previous.energy_kwh + drive.energy_kwh, 2)
            previous.odometer_end = drive.odometer_end or previous.odometer_end
            previous.soc_end = drive.soc_end if drive.soc_end is not None else previous.soc_end
            previous.range_end = drive.range_end or previous.range_end
            if previous.range_start is None:
                previous.range_start = drive.range_start
            previous.speed_max = max(previous.speed_max or 0, drive.speed_max or 0) or None
            if drive.power_max is not None:
                previous.power_max = max(previous.power_max or 0, drive.power_max) or None
            if drive.power_min is not None:
                previous.power_min = min(previous.power_min or 0, drive.power_min) or None
            continue
        out.append(drive)
    return out


def _merge_charges(charges: list[Charge]) -> list[Charge]:
    charges = sorted(charges, key=lambda item: item.start)
    out: list[Charge] = []
    for charge in charges:
        if not out:
            out.append(charge)
            continue
        previous = out[-1]
        overlap = previous.end - charge.start
        if overlap > 0:
            if charge.duration_seconds > previous.duration_seconds:
                out[-1] = charge
            continue
        if charge.start - previous.end <= ADJACENT_SECONDS:
            previous.end = charge.end
            previous.energy_input_kwh = round(
                previous.energy_input_kwh + charge.energy_input_kwh, 2
            )
            previous.energy_dc_kwh = round(previous.energy_dc_kwh + charge.energy_dc_kwh, 2)
            previous.peak_power_kw = max(previous.peak_power_kw, charge.peak_power_kw)
            previous.power_sum += charge.power_sum
            previous.power_samples += charge.power_samples
            previous.soc_end = charge.soc_end if charge.soc_end is not None else previous.soc_end
            previous.range_end = charge.range_end or previous.range_end
            if charge.temp_min is not None:
                previous.temp_min = (
                    charge.temp_min if previous.temp_min is None else min(previous.temp_min, charge.temp_min)
                )
            if charge.temp_max is not None:
                previous.temp_max = (
                    charge.temp_max if previous.temp_max is None else max(previous.temp_max, charge.temp_max)
                )
            continue
        out.append(charge)
    return out


def _merge_spans(spans: list[tuple[int, int]], gap: int) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = out[-1]
        if start - last_end <= gap:
            out[-1] = (last_start, max(last_end, end))
        else:
            out.append((start, end))
    return out


def parse_files(paths: list[str]) -> MergedExport:
    files = [parse_zip(path) for path in paths]
    return merge_exports(files)
