"""Fill the TeslaMate fields that the export import could not supply.

TeslaMate's dashboards derive almost everything from a handful of columns that
live logging fills as it goes: the rated-range delta of a drive (energy and
efficiency panels), ``charger_phases`` on charge samples (AC/DC split), the rated
range on a charging process (battery capacity), ``cars.efficiency`` (km -> kWh),
and the addresses/geofences at drive and charge boundaries (locations and top
destinations).

The historical import wrote the sessions themselves but left those columns
empty, so the panels read zero or showed everything as DC. This backfill:

1. re-reads the uploaded export zips and completes drive/charge ranges, km and
   battery levels that the rows are missing;
2. falls back to the positions table (which the export enrichment filled with
   SoC/odometer/range) for anything a zip does not cover;
3. classifies each charging session AC or DC from its peak power, and gives
   sessions without samples one synthetic sample so the classification sticks;
4. resolves the start/end/charge locations through Nominatim (cached in the
   ``addresses`` table) and links the drives and charges to them;
5. derives ``cars.efficiency`` from the export range/SOC pairs.

It is idempotent: run it again after importing more zips.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import statistics
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import psycopg

from export_parser import Charge, Drive, MergedExport, parse_files

_LOGGER = logging.getLogger("xpengmate.backfill")

NOMINATIM = "https://nominatim.openstreetmap.org"
USER_AGENT = "XpengMate/4.4 (self-hosted XPENGMate history import)"
DC_POWER_THRESHOLD_KW = 20.0
MATCH_TOLERANCE_SECONDS = 5


def _utc(epoch: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).replace(tzinfo=None)


def _epoch(value: dt.datetime | None) -> int | None:
    if value is None:
        return None
    return int(value.replace(tzinfo=dt.timezone.utc).timestamp())


class Backfill:
    def __init__(self, dsn: str, vin: str, usable_kwh: float, *, geocode: bool = True) -> None:
        self.conn = psycopg.connect(dsn, autocommit=False)
        self.vin = vin
        self.usable_kwh = usable_kwh
        self.geocode = geocode
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM cars WHERE vin = %s", (vin,))
            row = cur.fetchone()
            if not row:
                raise SystemExit(f"car {vin} is not in the database")
            self.car_id = int(row[0])
        self.stats: dict[str, int] = {}
        self._addresses: list[tuple[int, float, float]] = []
        self._geocodes = 0
        self._last_geocode = 0.0

    # -- helpers ---------------------------------------------

    def _count(self, key: str, amount: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + amount

    def _drives(self) -> list[tuple[int, dt.datetime, dt.datetime | None]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT id, start_date, end_date FROM drives WHERE car_id = %s ORDER BY start_date",
                (self.car_id,),
            )
            return [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]

    def _charges(self) -> list[tuple[int, dt.datetime, dt.datetime | None]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, start_date, end_date FROM charging_processes
                WHERE car_id = %s ORDER BY start_date
                """,
                (self.car_id,),
            )
            return [(int(row[0]), row[1], row[2]) for row in cur.fetchall()]

    def _position_ids(self, start: dt.datetime, end: dt.datetime | None) -> tuple[int, int] | None:
        """First and last position inside a window (null when there are none)."""
        if end is None:
            end = start
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    (SELECT id FROM positions
                     WHERE car_id = %s AND date BETWEEN %s AND %s
                     ORDER BY date ASC LIMIT 1),
                    (SELECT id FROM positions
                     WHERE car_id = %s AND date BETWEEN %s AND %s
                     ORDER BY date DESC LIMIT 1)
                """,
                (self.car_id, start, end, self.car_id, start, end),
            )
            first, last = cur.fetchone()
            if first is None or last is None:
                return None
            return int(first), int(last)

    def _position_ranges(self, position_id: int) -> dict[str, float | None]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT ideal_battery_range_km, rated_battery_range_km,
                       est_battery_range_km, odometer, battery_level
                FROM positions WHERE id = %s
                """,
                (position_id,),
            )
            row = cur.fetchone()
        if not row:
            return {}
        ideal, rated, est, odometer, soc = row
        return {
            "ideal": float(ideal) if ideal is not None else None,
            "rated": float(rated) if rated is not None else None,
            "est": float(est) if est is not None else None,
            "odometer": float(odometer) if odometer is not None else None,
            "soc": int(soc) if soc is not None else None,
        }

    # -- ranges ----------------------------------------------

    def apply_export_ranges(self, merged: MergedExport) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, extract(epoch FROM start_date)::bigint,
                       COALESCE(extract(epoch FROM end_date)::bigint, extract(epoch FROM start_date)::bigint)
                FROM drives WHERE car_id = %s
                """,
                (self.car_id,),
            )
            drive_windows = [(int(row_id), int(start), int(end)) for row_id, start, end in cur.fetchall()]

        for drive in merged.drives:
            drive_id = self._match_window(drive_windows, drive.start, drive.end)
            if drive_id is None:
                continue
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE drives SET
                        start_ideal_range_km = COALESCE(start_ideal_range_km, %s),
                        end_ideal_range_km   = COALESCE(end_ideal_range_km, %s),
                        start_rated_range_km = COALESCE(start_rated_range_km, %s),
                        end_rated_range_km   = COALESCE(end_rated_range_km, %s),
                        start_km = COALESCE(start_km, %s),
                        end_km   = COALESCE(end_km, %s)
                    WHERE id = %s
                    """,
                    (
                        drive.range_start,
                        drive.range_end,
                        drive.range_start,
                        drive.range_end,
                        drive.odometer_start,
                        drive.odometer_end,
                        drive_id,
                    ),
                )
                if cur.rowcount:
                    self._count("drives_ranges")

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, extract(epoch FROM start_date)::bigint,
                       COALESCE(extract(epoch FROM end_date)::bigint, extract(epoch FROM start_date)::bigint)
                FROM charging_processes WHERE car_id = %s
                """,
                (self.car_id,),
            )
            charge_windows = [(int(row_id), int(start), int(end)) for row_id, start, end in cur.fetchall()]
        for charge in merged.charges:
            charge_id = self._match_window(charge_windows, charge.start, charge.end)
            if charge_id is None:
                continue
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE charging_processes SET
                        start_ideal_range_km = COALESCE(start_ideal_range_km, %s),
                        end_ideal_range_km   = COALESCE(end_ideal_range_km, %s),
                        start_rated_range_km = COALESCE(start_rated_range_km, %s),
                        end_rated_range_km   = COALESCE(end_rated_range_km, %s),
                        start_battery_level  = COALESCE(start_battery_level, %s),
                        end_battery_level    = COALESCE(end_battery_level, %s),
                        charge_energy_added  = COALESCE(charge_energy_added, NULLIF(%s, 0)),
                        charge_energy_used   = COALESCE(charge_energy_used, NULLIF(%s, 0))
                    WHERE id = %s
                    """,
                    (
                        charge.range_start,
                        charge.range_end,
                        charge.range_start,
                        charge.range_end,
                        charge.soc_start,
                        charge.soc_end,
                        charge.energy_dc_kwh or charge.energy_input_kwh,
                        charge.energy_input_kwh,
                        charge_id,
                    ),
                )
                if cur.rowcount:
                    self._count("charges_ranges")
        self.conn.commit()

    @staticmethod
    def _match_window(windows: list[tuple[int, int, int]], start: int, end: int) -> int | None:
        """The row overlapping this record the most, or an exact-second match."""
        best: tuple[int, float] | None = None
        for row_id, row_start, row_end in windows:
            if start == row_start:
                return row_id
            overlap = min(end, row_end) - max(start, row_start)
            if overlap <= 0:
                continue
            duration = max(1, end - start)
            ratio = overlap / duration
            if ratio >= 0.5 and (best is None or ratio > best[1]):
                best = (row_id, ratio)
        return best[0] if best else None

    @staticmethod
    def _match(index: dict[int, int], epoch: int) -> int | None:
        for delta in range(MATCH_TOLERANCE_SECONDS + 1):
            for candidate in (epoch - delta, epoch + delta):
                if candidate in index:
                    return index[candidate]
        return None

    def ranges_from_positions(self) -> None:
        """Fill remaining ranges from the enriched positions table."""
        for drive_id, start, end in self._drives():
            positions = self._position_ids(start, end)
            if positions is None:
                continue
            first, last = positions
            start_ranges = self._position_ranges(first)
            end_ranges = self._position_ranges(last)
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE drives SET
                        start_position_id = COALESCE(start_position_id, %s),
                        end_position_id   = COALESCE(end_position_id, %s),
                        start_ideal_range_km = COALESCE(start_ideal_range_km, %s),
                        end_ideal_range_km   = COALESCE(end_ideal_range_km, %s),
                        start_rated_range_km = COALESCE(start_rated_range_km, %s),
                        end_rated_range_km   = COALESCE(end_rated_range_km, %s),
                        start_km = COALESCE(start_km, %s),
                        end_km   = COALESCE(end_km, %s)
                    WHERE id = %s
                    """,
                    (
                        first,
                        last,
                        start_ranges.get("ideal"),
                        end_ranges.get("ideal"),
                        start_ranges.get("rated") or start_ranges.get("ideal"),
                        end_ranges.get("rated") or end_ranges.get("ideal"),
                        start_ranges.get("odometer"),
                        end_ranges.get("odometer"),
                        drive_id,
                    ),
                )
                if cur.rowcount:
                    self._count("drives_position_fill")
        self.conn.commit()

        for charge_id, start, end in self._charges():
            positions = self._position_ids(start, end or start)
            if positions is None:
                continue
            first, _last = positions
            offset = 0
            if end is not None:
                with self.conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT id FROM positions
                        WHERE car_id = %s AND date BETWEEN %s AND %s
                        ORDER BY abs(extract(epoch FROM date) - extract(epoch FROM %s::timestamp))
                        LIMIT 1
                        """,
                        (self.car_id, start, end, end),
                    )
                    row = cur.fetchone()
                    if row:
                        offset = int(row[0])
            start_ranges = self._position_ranges(first)
            end_ranges = self._position_ranges(offset) if offset else start_ranges
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE charging_processes SET
                        position_id = COALESCE(position_id, %s),
                        start_ideal_range_km = COALESCE(start_ideal_range_km, %s),
                        end_ideal_range_km   = COALESCE(end_ideal_range_km, %s),
                        start_rated_range_km = COALESCE(start_rated_range_km, %s),
                        end_rated_range_km   = COALESCE(end_rated_range_km, %s)
                    WHERE id = %s
                    """,
                    (
                        first,
                        start_ranges.get("ideal"),
                        end_ranges.get("ideal"),
                        start_ranges.get("rated") or start_ranges.get("ideal"),
                        end_ranges.get("rated") or end_ranges.get("ideal"),
                        charge_id,
                    ),
                )
                if cur.rowcount:
                    self._count("charges_position_fill")
        self.conn.commit()

    def fill_charge_ranges_from_soc(self) -> None:
        """Last resort for a charging session with no position inside it.

        Convert its battery levels with the derived full range; the capacity
        panel only needs a plausible start/end rated radius, and a session that
        is otherwise invisible is worse than an estimated one.
        """
        with self.conn.cursor() as cur:
            cur.execute("SELECT efficiency FROM cars WHERE id = %s", (self.car_id,))
            row = cur.fetchone()
        if not row or not row[0]:
            return
        full_range = self.usable_kwh / float(row[0])
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE charging_processes SET
                    start_rated_range_km = COALESCE(start_rated_range_km, round((%s * start_battery_level / 100.0)::numeric, 2)),
                    end_rated_range_km   = COALESCE(end_rated_range_km, round((%s * end_battery_level / 100.0)::numeric, 2)),
                    start_ideal_range_km = COALESCE(start_ideal_range_km, round((%s * start_battery_level / 100.0)::numeric, 2)),
                    end_ideal_range_km   = COALESCE(end_ideal_range_km, round((%s * end_battery_level / 100.0)::numeric, 2))
                WHERE car_id = %s
                  AND start_battery_level IS NOT NULL
                  AND end_battery_level IS NOT NULL
                  AND (start_rated_range_km IS NULL OR end_rated_range_km IS NULL)
                """,
                (full_range, full_range, full_range, full_range, self.car_id),
            )
            self._count("charges_soc_fallback", cur.rowcount)
        self.conn.commit()

    # -- charge/position samples -----------------------------

    def normalise_positions(self) -> None:
        """The export has one range figure; give the rated column that value.

        TeslaMate's dashboards read the *rated* range (the preferred range in
        this install), while the historical import only filled the ideal one.
        """
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE positions SET
                    rated_battery_range_km = COALESCE(rated_battery_range_km, ideal_battery_range_km),
                    usable_battery_level = COALESCE(usable_battery_level, battery_level)
                WHERE car_id = %s
                """,
                (self.car_id,),
            )
            self._count("positions_range_normalised", cur.rowcount)
        self.conn.commit()

    def normalise_charge_samples(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE charges c SET
                    rated_battery_range_km = COALESCE(c.rated_battery_range_km, c.ideal_battery_range_km),
                    usable_battery_level = COALESCE(c.usable_battery_level, c.battery_level)
                FROM charging_processes cp
                WHERE cp.id = c.charging_process_id AND cp.car_id = %s
                """,
                (self.car_id,),
            )
            self._count("charge_samples_range", cur.rowcount)
        self.conn.commit()

    def classify_charge_type(self, merged: MergedExport | None = None) -> None:
        peaks: dict[int, float] = {}
        if merged is not None:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, extract(epoch FROM start_date)::bigint,
                           COALESCE(extract(epoch FROM end_date)::bigint, extract(epoch FROM start_date)::bigint)
                    FROM charging_processes WHERE car_id = %s
                    """,
                    (self.car_id,),
                )
                charge_windows = [(int(row_id), int(start), int(end)) for row_id, start, end in cur.fetchall()]
            for charge in merged.charges:
                charge_id = self._match_window(charge_windows, charge.start, charge.end)
                if charge_id is not None:
                    peaks[charge_id] = max(peaks.get(charge_id, 0.0), charge.peak_power_kw)

        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT cp.id,
                       COALESCE(max(c.charger_power), 0),
                       count(c.id),
                       extract(epoch FROM cp.start_date)::bigint,
                       extract(epoch FROM cp.end_date)::bigint,
                       cp.start_battery_level,
                       cp.end_battery_level,
                       cp.charge_energy_added,
                       cp.duration_min,
                       cp.start_rated_range_km,
                       cp.end_rated_range_km
                FROM charging_processes cp
                LEFT JOIN charges c ON c.charging_process_id = cp.id
                WHERE cp.car_id = %s
                GROUP BY cp.id
                """,
                (self.car_id,),
            )
            sessions = cur.fetchall()

        for row in sessions:
            (
                charge_id,
                peak,
                sample_count,
                start_epoch,
                end_epoch,
                soc_start,
                soc_end,
                energy,
                duration,
                start_range,
                end_range,
            ) = row
            peak = max(float(peak or 0), peaks.get(int(charge_id), 0.0))
            if sample_count == 0:
                self._insert_synthetic_sample(
                    int(charge_id),
                    start_epoch,
                    end_epoch,
                    soc_start,
                    soc_end,
                    energy,
                    duration,
                    start_range,
                    end_range,
                    peak,
                )
            is_dc = peak >= DC_POWER_THRESHOLD_KW
            with self.conn.cursor() as cur:
                cur.execute(
                    "UPDATE charges SET charger_phases = %s, fast_charger_present = %s WHERE charging_process_id = %s",
                    (None if is_dc else 1, is_dc, charge_id),
                )
                self._count("charge_samples_classified", cur.rowcount)
        self.conn.commit()

    def _insert_synthetic_sample(
        self,
        charge_id: int,
        start_epoch: int | None,
        end_epoch: int | None,
        soc_start: Any,
        soc_end: Any,
        energy: Any,
        duration: Any,
        start_range: Any,
        end_range: Any,
        peak: float,
    ) -> None:
        when = end_epoch or start_epoch
        if when is None:
            return
        energy = float(energy or 0)
        duration = float(duration or 0)
        power = peak or (energy * 60 / duration if duration else 0)
        is_dc = power >= DC_POWER_THRESHOLD_KW
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO charges
                    (date, battery_level, usable_battery_level, charge_energy_added,
                     charger_power, charger_phases, fast_charger_present,
                     ideal_battery_range_km, rated_battery_range_km, charging_process_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    _utc(int(when)),
                    soc_end,
                    soc_end,
                    energy,
                    int(round(power)),
                    None if is_dc else 1,
                    is_dc,
                    end_range or start_range or 0,
                    end_range or start_range or 0,
                    charge_id,
                ),
            )
            self._count("charge_samples_inserted")

    # -- efficiency ------------------------------------------

    def derive_efficiency(self, merged: MergedExport | None) -> None:
        samples: list[float] = []
        if merged is not None:
            for charge in merged.charges:
                if charge.soc_start and charge.range_start and charge.soc_start >= 15:
                    samples.append(charge.range_start * 100.0 / charge.soc_start)
            for drive in merged.drives:
                if drive.soc_start and drive.range_start and drive.soc_start >= 15:
                    samples.append(drive.range_start * 100.0 / drive.soc_start)
        if not samples:
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT rated_battery_range_km * 100.0 / usable_battery_level
                    FROM charges c JOIN charging_processes cp ON cp.id = c.charging_process_id
                    WHERE cp.car_id = %s AND usable_battery_level > 15
                    """,
                    (self.car_id,),
                )
                samples = [float(row[0]) for row in cur.fetchall() if row[0]]
        samples = [value for value in samples if 100 < value < 1200]
        if not samples:
            _LOGGER.warning("no range/SOC pairs to derive efficiency from")
            return
        full_range = statistics.median(samples)
        efficiency = round(self.usable_kwh / full_range, 6)
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE cars SET efficiency = %s, updated_at = now() WHERE id = %s",
                (efficiency, self.car_id),
            )
        self.conn.commit()
        _LOGGER.info("cars.efficiency = %.4f kWh/km (full range %.0f km)", efficiency, full_range)

    # -- locations -------------------------------------------

    def link_positions_and_locations(self) -> None:
        with self.conn.cursor() as cur:
            # Positions belong to the drive whose time window contains them.
            cur.execute(
                """
                UPDATE positions p SET drive_id = d.id
                FROM drives d
                WHERE p.drive_id IS NULL AND p.car_id = d.car_id AND d.car_id = %s
                  AND p.date BETWEEN d.start_date AND COALESCE(d.end_date, d.start_date)
                """,
                (self.car_id,),
            )
            self._count("positions_linked", cur.rowcount)
            cur.execute(
                "SELECT id, latitude, longitude FROM addresses",
                (),
            )
            self._addresses = [
                (int(row[0]), float(row[1]), float(row[2]))
                for row in cur.fetchall()
                if row[1] is not None and row[2] is not None
            ]
        self.conn.commit()

        for drive_id, start, end in self._drives():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT start_address_id, end_address_id FROM drives WHERE id = %s",
                    (drive_id,),
                )
                start_address, end_address = cur.fetchone()
            positions = self._position_ids(start, end)
            if positions is None:
                continue
            for position_id, column in ((positions[0], "start"), (positions[1], "end")):
                if (column == "start" and start_address) or (column == "end" and end_address):
                    continue
                address_id = self._address_for_position(position_id)
                if address_id is None:
                    continue
                with self.conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE drives SET {column}_address_id = %s WHERE id = %s AND {column}_address_id IS NULL",
                        (address_id, drive_id),
                    )
                    self._count("drive_addresses", cur.rowcount)
            self.conn.commit()

        for charge_id, start, end in self._charges():
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT address_id, position_id FROM charging_processes WHERE id = %s",
                    (charge_id,),
                )
                address_id, position_id = cur.fetchone()
            if address_id:
                continue
            positions = self._position_ids(start, end or start)
            if positions is None:
                continue
            address_id = self._address_for_position(positions[0] if not position_id else position_id)
            if address_id is None:
                continue
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE charging_processes SET address_id = COALESCE(address_id, %s),
                                                  position_id = COALESCE(position_id, %s)
                    WHERE id = %s
                    """,
                    (address_id, positions[0], charge_id),
                )
                self._count("charge_addresses", cur.rowcount)
        self.conn.commit()

    def _address_for_position(self, position_id: int) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT latitude, longitude FROM positions WHERE id = %s", (position_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            latitude, longitude = float(row[0]), float(row[1])
        cached = self._cached_address(latitude, longitude)
        if cached is not None:
            return cached
        if not self.geocode:
            return None
        payload = self._reverse_geocode(latitude, longitude)
        if payload is None:
            return None
        return self._insert_address(payload, latitude, longitude)

    def _cached_address(self, latitude: float, longitude: float) -> int | None:
        for address_id, address_lat, address_lon in self._addresses:
            if abs(address_lat - latitude) < 0.0005 and abs(address_lon - longitude) < 0.0005:
                return address_id
        return None

    def _reverse_geocode(self, latitude: float, longitude: float) -> dict[str, Any] | None:
        delay = 1.1 - (time.monotonic() - self._last_geocode)
        if delay > 0:
            time.sleep(delay)
        self._last_geocode = time.monotonic()
        query = urllib.parse.urlencode(
            {"format": "jsonv2", "lat": f"{latitude:.6f}", "lon": f"{longitude:.6f}", "zoom": 18, "addressdetails": 1}
        )
        request = urllib.request.Request(
            f"{NOMINATIM}/reverse?{query}", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read())
        except Exception as exc:  # noqa: BLE001 - geocoding is best effort
            _LOGGER.warning("reverse geocode %.5f,%.5f failed: %s", latitude, longitude, exc)
            return None
        self._geocodes += 1
        if self._geocodes % 10 == 0:
            _LOGGER.info("geocoded %d locations", self._geocodes)
        return payload

    def _insert_address(self, payload: dict[str, Any], latitude: float, longitude: float) -> int | None:
        osm_id = payload.get("osm_id")
        osm_type = payload.get("osm_type")
        if osm_id is not None and osm_type is not None:
            with self.conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM addresses WHERE osm_id = %s AND osm_type = %s",
                    (int(osm_id), str(osm_type)),
                )
                row = cur.fetchone()
            if row:
                address_id = int(row[0])
                self._addresses.append((address_id, latitude, longitude))
                return address_id
        details = payload.get("address") or {}
        road = details.get("road")
        house_number = details.get("house_number")
        name = (
            details.get("amenity")
            or details.get("tourism")
            or details.get("shop")
            or details.get("building")
            or None
        )
        if name is None and road:
            name = street = details.get("road")
        else:
            street = road
        fields = {
            "display_name": payload.get("display_name"),
            "latitude": latitude,
            "longitude": longitude,
            "name": name,
            "house_number": house_number,
            "road": street,
            "neighbourhood": details.get("neighbourhood") or details.get("suburb"),
            "city": details.get("city") or details.get("town") or details.get("village") or details.get("municipality"),
            "county": details.get("county"),
            "postcode": details.get("postcode"),
            "state": details.get("state"),
            "state_district": details.get("state_district"),
            "country": details.get("country"),
            "osm_id": int(osm_id) if osm_id is not None else None,
            "osm_type": str(osm_type) if osm_type is not None else None,
        }
        columns = ", ".join(list(fields) + ["raw", "inserted_at", "updated_at"])
        placeholders = ", ".join(["%s"] * (len(fields) + 3))
        values = list(fields.values()) + [json.dumps(payload), dt.datetime.utcnow(), dt.datetime.utcnow()]
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO addresses ({columns}) VALUES ({placeholders}) RETURNING id", values
            )
            address_id = int(cur.fetchone()[0])
        self.conn.commit()
        self._addresses.append((address_id, latitude, longitude))
        self._count("addresses_created")
        return address_id

    # -- run -------------------------------------------------

    def run(self, merged: MergedExport | None) -> dict[str, int]:
        self.normalise_positions()
        if merged is not None:
            self.apply_export_ranges(merged)
        self.ranges_from_positions()
        self.fill_charge_ranges_from_soc()
        self.normalise_charge_samples()
        self.classify_charge_type(merged)
        self.derive_efficiency(merged)
        self.link_positions_and_locations()
        return self.stats


def load_uploaded(directory: str) -> MergedExport | None:
    paths = sorted(Path(directory).glob("*.zip"))
    if not paths:
        return None
    _LOGGER.info("parsing %d uploaded export(s) from %s", len(paths), directory)
    return parse_files([str(path) for path in paths])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zips", nargs="*")
    parser.add_argument("--database-url", default=os.environ.get("XPENGMATE_DATABASE_URL", ""))
    parser.add_argument("--vin", default=os.environ.get("XPENG_VIN", ""))
    parser.add_argument("--usable-kwh", default=os.environ.get("XPENG_USABLE_BATTERY_KWH", "87.5"))
    parser.add_argument("--exports-dir", default=os.environ.get("HISTORY_EXPORT_DIR", ""))
    parser.add_argument("--no-geocode", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if not args.database_url or not args.vin:
        print("XPENGMATE_DATABASE_URL and XPENG_VIN are required", file=sys.stderr)
        return 2
    merged = parse_files(args.zips) if args.zips else load_uploaded(args.exports_dir)
    backfill = Backfill(
        args.database_url,
        args.vin,
        float(args.usable_kwh),
        geocode=not args.no_geocode,
    )
    stats = backfill.run(merged)
    print(json.dumps(stats, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
