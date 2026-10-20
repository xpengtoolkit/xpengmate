"""Backfill xpengmate's TeslaMate database from EVConduit's XPENG history.

What is written, in TeslaMate's own schema:

  cars                 one row, with eid/vid identical to the engine's stable ids
  car_settings         one row with safe defaults
  drives               from xpeng_trips
  positions            from vehicle_positions (the GPS logger), attached to drives
  charging_processes   from xpeng_charging_sessions
  states               derived from observed coverage (online) and its gaps (asleep)

What is deliberately NOT written: `charges` (the per-sample power curve needs the
export's 1 Hz file, not these aggregates), `updates` (XPENG history has no version
series), addresses and geofences (no reverse geocoding is run here).

Idempotency: every inserted row is recorded in `xpengmate_import` under its
natural key. A re-run deletes what it imported last time and re-creates it; rows
the live logger wrote are never touched, and are detected by *not* appearing in
that table.
"""

from __future__ import annotations

import argparse
import bisect
import datetime as dt
import logging
import os
import sys
import zlib
from typing import Any, Iterable, Sequence

import psycopg

import export_enrich
from evconduit import Evconduit

_LOGGER = logging.getLogger("xpengmate.history")

UTC = dt.timezone.utc


# -- helpers -------------------------------------------------


def naive_utc(value: Any) -> dt.datetime | None:
    """An ISO timestamp or epoch number as a naive UTC datetime.

    TeslaMate's timestamp columns are `timestamp without time zone` holding UTC.
    Handing Postgres a tz-aware value lets the session timezone move the instant,
    so the conversion happens here, once.
    """
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(float(value), UTC).replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        return dt.datetime.fromtimestamp(int(text), UTC).replace(tzinfo=None)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def as_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return None if number is None else int(round(number))


def stable_ids(vin: str) -> tuple[int, int]:
    """The (eid, vid) the engine will report for this VIN.

    Same crc32 scheme as the engine's teslamate_mapping.stable_ids, so a car
    pre-seeded here is the car TeslaMate finds when the provider comes online -
    instead of a second car row.
    """
    def one(salt: str) -> int:
        return (zlib.crc32(f"{salt}:{vin}".encode("utf-8")) & 0x7FFFFFFF) or 1

    return one("eid"), one("vid")


def chunks(items: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# -- database ------------------------------------------------


SCHEMA_CONCESSION = """
ALTER TABLE charging_processes ALTER COLUMN position_id DROP NOT NULL
"""

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS xpengmate_import (
    kind        text        NOT NULL,
    source_key  text        NOT NULL,
    target_id   bigint,
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, source_key)
)
"""


class Store:
    def __init__(self, dsn: str, vin: str, usable_kwh: float) -> None:
        self.conn = psycopg.connect(dsn, autocommit=False)
        self.vin = vin
        self.usable_kwh = usable_kwh
        self.car_id: int | None = None

    def close(self) -> None:
        self.conn.close()

    # -- schema / identity -----------------------------------

    def prepare(self) -> int:
        with self.conn.cursor() as cur:
            cur.execute(TRACKING_TABLE)
            cur.execute(SCHEMA_CONCESSION)
            cur.execute("SELECT id FROM cars WHERE vin = %s", (self.vin,))
            row = cur.fetchone()
            eid, vid = stable_ids(self.vin)
            if row:
                self.car_id = int(row[0])
                _LOGGER.info("Using existing car id %s", self.car_id)
            else:
                cur.execute(
                    "INSERT INTO car_settings (id) VALUES (DEFAULT) RETURNING id"
                )
                settings_id = cur.fetchone()[0]
                now = dt.datetime.now(UTC).replace(tzinfo=None)
                cur.execute(
                    """
                    INSERT INTO cars
                        (eid, vid, vin, name, model, trim_badging, efficiency,
                         inserted_at, updated_at, settings_id, display_priority,
                         marketing_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        eid,
                        vid,
                        self.vin,
                        os.environ.get("XPENG_CAR_NAME", "XPeng"),
                        "xpeng",
                        "G6",
                        None,
                        now,
                        now,
                        settings_id,
                        1,
                        "XPeng",
                    ),
                )
                self.car_id = int(cur.fetchone()[0])
                _LOGGER.info(
                    "Created car id %s (eid=%s vid=%s)", self.car_id, eid, vid
                )
            self.conn.commit()
        return self.car_id

    # -- tracking --------------------------------------------

    def tracked_ids(self, kind: str) -> list[int]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT target_id FROM xpengmate_import WHERE kind = %s AND target_id IS NOT NULL",
                (kind,),
            )
            return [int(row[0]) for row in cur.fetchall()]

    def forget(self, kinds: Sequence[str]) -> int:
        """Delete the rows a previous run created, leaving live rows alone."""
        removed = 0
        order = ["charging_process", "drive", "position", "state"]
        tables = {
            "charging_process": "charging_processes",
            "drive": "drives",
            "position": "positions",
            "state": "states",
        }
        with self.conn.cursor() as cur:
            for kind in order:
                if kind not in kinds:
                    continue
                ids = self.tracked_ids(kind)
                if not ids:
                    continue
                cur.execute(
                    f"DELETE FROM {tables[kind]} WHERE id = ANY(%s)", (ids,)
                )
                removed += cur.rowcount
                cur.execute(
                    "DELETE FROM xpengmate_import WHERE kind = %s", (kind,)
                )
        self.conn.commit()
        return removed

    def remember(self, kind: str, key: str, target_id: int | None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO xpengmate_import (kind, source_key, target_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (kind, source_key) DO UPDATE
                    SET target_id = EXCLUDED.target_id, imported_at = now()
                """,
                (kind, key, target_id),
            )

    # -- untracked (live) coverage ---------------------------

    def untracked_drives(self) -> list[tuple[dt.datetime, dt.datetime]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT start_date, COALESCE(end_date, start_date)
                FROM drives d
                WHERE d.car_id = %s
                  AND NOT EXISTS (SELECT 1 FROM xpengmate_import i
                                  WHERE i.kind IN ('drive','export-drive') AND i.target_id = d.id)
                """,
                (self.car_id,),
            )
            return [(row[0], row[1]) for row in cur.fetchall()]

    def untracked_charge_windows(self) -> list[tuple[dt.datetime, dt.datetime]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT start_date, COALESCE(end_date, start_date)
                FROM charging_processes c
                WHERE c.car_id = %s
                  AND NOT EXISTS (SELECT 1 FROM xpengmate_import i
                                  WHERE i.kind IN ('charging_process','export-charge') AND i.target_id = c.id)
                """,
                (self.car_id,),
            )
            return [(row[0], row[1]) for row in cur.fetchall()]

    def untracked_position_dates(self) -> set[dt.datetime]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT p.date
                FROM positions p
                WHERE p.car_id = %s
                  AND NOT EXISTS (SELECT 1 FROM xpengmate_import i
                                  WHERE i.kind = 'position' AND i.target_id = p.id)
                """,
                (self.car_id,),
            )
            return {row[0] for row in cur.fetchall()}

    # -- inserts ---------------------------------------------

    def insert_drives(self, trips: list[dict[str, Any]], skip: list[tuple]) -> list[tuple]:
        """Insert drives; returns (start, end, id) windows for position linking."""
        windows: list[tuple[dt.datetime, dt.datetime, int]] = []
        inserted = 0
        with self.conn.cursor() as cur:
            for trip in trips:
                start = naive_utc(trip.get("started_at"))
                end = naive_utc(trip.get("ended_at"))
                if start is None or end is None or end <= start:
                    continue
                if _overlaps(start, end, skip):
                    continue
                key = f"{self.vin}:{start.isoformat()}"
                distance = as_float(trip.get("distance_km"))
                duration = as_int(trip.get("duration_seconds"))
                cur.execute(
                    """
                    INSERT INTO drives
                        (start_date, end_date, distance, duration_min, start_km,
                         end_km, speed_max, power_max, power_min, outside_temp_avg,
                         inside_temp_avg, start_ideal_range_km, end_ideal_range_km,
                         start_rated_range_km, end_rated_range_km, ascent, descent,
                         car_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        start,
                        end,
                        distance,
                        round(duration / 60) if duration else None,
                        as_float(trip.get("odometer_start")),
                        as_float(trip.get("odometer_end")),
                        as_int(trip.get("max_speed_kmh")),
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        self.car_id,
                    ),
                )
                drive_id = int(cur.fetchone()[0])
                self.remember("drive", key, drive_id)
                windows.append((start, end, drive_id))
                inserted += 1
        self.conn.commit()
        _LOGGER.info("Imported %d drives", inserted)
        return windows

    def insert_positions(
        self,
        positions: list[dict[str, Any]],
        drive_windows: list[tuple[dt.datetime, dt.datetime, int]],
        skip_dates: set[dt.datetime],
    ) -> list[tuple[dt.datetime, int]]:
        windows = sorted(drive_windows, key=lambda item: item[0])
        starts = [item[0] for item in windows]
        by_time: list[tuple[dt.datetime, int]] = []
        inserted = 0
        with self.conn.cursor() as cur:
            for point in positions:
                when = naive_utc(point.get("recorded_at"))
                if when is None or when in skip_dates:
                    continue
                lat = as_float(point.get("latitude"))
                lon = as_float(point.get("longitude"))
                if lat is None or lon is None:
                    continue
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                drive_id = _containing_drive(windows, starts, when)
                speed = as_float(point.get("speed_kmh"))
                altitude = as_float(point.get("altitude_m"))
                cur.execute(
                    """
                    INSERT INTO positions
                        (date, latitude, longitude, speed, elevation, odometer,
                         est_battery_range_km, battery_level, car_id, drive_id)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        when,
                        round(lat, 6),
                        round(lon, 6),
                        as_int(speed),
                        as_int(altitude),
                        None,
                        None,
                        None,
                        self.car_id,
                        drive_id,
                    ),
                )
                position_id = int(cur.fetchone()[0])
                self.remember(
                    "position", f"{self.vin}:{when.isoformat()}", position_id
                )
                by_time.append((when, position_id))
                inserted += 1
        self.conn.commit()
        _LOGGER.info("Imported %d positions", inserted)
        by_time.sort()
        return by_time

    def insert_charging_processes(
        self,
        sessions: list[dict[str, Any]],
        position_times: list[tuple[dt.datetime, int]],
        skip: list[tuple],
    ) -> list[tuple[dict[str, Any], int]]:
        times = [item[0] for item in position_times]
        inserted = 0
        created: list[tuple[dict[str, Any], int]] = []
        with self.conn.cursor() as cur:
            for session in sessions:
                start = naive_utc(session.get("started_at"))
                end = naive_utc(session.get("ended_at"))
                if start is None or end is None:
                    continue
                if _overlaps(start, end, skip):
                    continue
                key = f"{self.vin}:{start.isoformat()}"
                position_id = _nearest_position(times, position_times, start)
                duration = as_int(session.get("duration_seconds"))
                cur.execute(
                    """
                    INSERT INTO charging_processes
                        (start_date, end_date, charge_energy_added, charge_energy_used,
                         start_battery_level, end_battery_level, duration_min,
                         outside_temp_avg, car_id, position_id, address_id,
                         start_ideal_range_km, end_ideal_range_km,
                         start_rated_range_km, end_rated_range_km, geofence_id, cost)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING id
                    """,
                    (
                        start,
                        end,
                        as_float(session.get("energy_dc_kwh")),
                        as_float(session.get("energy_input_kwh")),
                        as_int(session.get("soc_start")),
                        as_int(session.get("soc_end")),
                        round(duration / 60) if duration else None,
                        None,
                        self.car_id,
                        position_id,
                        None,
                        as_float(session.get("range_start_km")),
                        as_float(session.get("range_end_km")),
                        None,
                        None,
                        None,
                        None,
                    ),
                )
                process_id = int(cur.fetchone()[0])
                self.remember("charging_process", key, process_id)
                created.append((session, process_id))
                inserted += 1
        self.conn.commit()
        _LOGGER.info("Imported %d charging processes", inserted)
        return created

    def insert_charge_samples(
        self, created: list[tuple[dict[str, Any], int]]
    ) -> int:
        """Replay each session's power curve into TeslaMate's `charges` table.

        EVConduit keeps a decimated copy of the export's 1 Hz charge series on the
        session row (`curve`: parallel arrays of seconds-from-start, pack amp,
        SoC, pack volt, wall-side kW, pack temperature, and the stride). That is
        the only surviving shape of the charge detail after the export zip ages
        out, and it is exactly what TeslaMate's charge graph needs.

        `charges.charge_energy_added` is a running total integrated from the
        wall-side power; TeslaMate's own logger writes the car's cumulative
        figure here. `charger_power`/`charger_voltage`/`charger_actual_current`
        are measured at the pack, which is the only place XPENG reports them.
        """
        inserted = 0
        with self.conn.cursor() as cur:
            for session, process_id in created:
                curve = session.get("curve")
                if not isinstance(curve, dict):
                    continue
                seconds = curve.get("t") or []
                power = curve.get("input_kw") or []
                soc = curve.get("soc") or []
                volt = curve.get("volt") or []
                amp = curve.get("amp") or []
                if len(seconds) < 2 or len(power) != len(seconds):
                    continue
                start = naive_utc(session.get("started_at"))
                if start is None:
                    continue
                range_start = as_float(session.get("range_start_km"))
                range_end = as_float(session.get("range_end_km"))
                soc_start = as_float(session.get("soc_start"))
                soc_end = as_float(session.get("soc_end"))
                peak = max((as_float(v) or 0.0) for v in power)
                fast = bool(session.get("is_dc_fast")) or peak >= 20
                # TeslaMate classifies a session AC/DC from the mode of
                # charger_phases across its samples; a fast charger has no
                # phases (NULL), an AC session reports at least one phase.
                # Without this every re-sync marked all sessions as DC again.
                phases = None if fast else 1
                cumulative = 0.0
                previous_t: float | None = None
                for index, t_value in enumerate(seconds):
                    try:
                        t_now = float(t_value)
                    except (TypeError, ValueError):
                        continue
                    kw = as_float(power[index])
                    if kw is None:
                        previous_t = t_now
                        continue
                    if previous_t is not None and t_now > previous_t:
                        cumulative += max(0.0, kw) * (t_now - previous_t) / 3600.0
                    previous_t = t_now
                    when = start + dt.timedelta(seconds=t_now)
                    level = as_int(soc[index] if index < len(soc) else None)
                    ideal_range = _interpolate_range(
                        range_start, range_end, soc_start, soc_end, level
                    )
                    if ideal_range is None:
                        continue
                    cur.execute(
                        """
                        INSERT INTO charges
                            (date, battery_level, usable_battery_level,
                             charge_energy_added, charger_power, charger_actual_current,
                             charger_voltage, charger_phases, ideal_battery_range_km,
                             rated_battery_range_km, outside_temp, fast_charger_present,
                             fast_charger_brand, fast_charger_type, conn_charge_cable,
                             charging_process_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            when,
                            level,
                            level,
                            round(cumulative, 2),
                            max(0, as_int(kw) or 0),
                            abs(as_int(amp[index])) if index < len(amp) else None,
                            as_int(volt[index]) if index < len(volt) else None,
                            phases,
                            ideal_range,
                            ideal_range,
                            None,
                            fast,
                            None,
                            None,
                            None,
                            process_id,
                        ),
                    )
                    inserted += 1
        self.conn.commit()
        _LOGGER.info("Imported %d charge samples", inserted)
        return inserted

    def import_tyre_pressures(self, readings: list[dict[str, Any]]) -> int:
        """Attach the hourly TPMS readings to the nearest position.

        `positions` is where TeslaMate stores tyre pressures, and the logger
        writes them on every sample. The history importer has one reading per
        clock hour, so each position takes the reading closest to its own time,
        within an hour. A reading that misses every position is not invented into
        one.
        """
        if not readings:
            return 0
        rows = []
        for reading in readings:
            when = naive_utc(reading.get("recorded_at"))
            values = (
                as_float(reading.get("front_left_bar")),
                as_float(reading.get("front_right_bar")),
                as_float(reading.get("rear_left_bar")),
                as_float(reading.get("rear_right_bar")),
            )
            if when and any(value is not None for value in values):
                rows.append((when, *values))
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS xpengmate_tyres")
            cur.execute(
                """
                CREATE TEMP TABLE xpengmate_tyres (
                    recorded_at timestamp without time zone PRIMARY KEY,
                    fl numeric, fr numeric, rl numeric, rr numeric
                )
                """
            )
            cur.executemany(
                "INSERT INTO xpengmate_tyres VALUES (%s,%s,%s,%s,%s)",
                rows,
            )
            cur.execute(
                """
                UPDATE positions p SET
                    tpms_pressure_fl = (
                        SELECT t.fl FROM xpengmate_tyres t
                        WHERE abs(extract(epoch FROM (t.recorded_at - p.date))) <= 3600
                        ORDER BY abs(extract(epoch FROM (t.recorded_at - p.date)))
                        LIMIT 1),
                    tpms_pressure_fr = (
                        SELECT t.fr FROM xpengmate_tyres t
                        WHERE abs(extract(epoch FROM (t.recorded_at - p.date))) <= 3600
                        ORDER BY abs(extract(epoch FROM (t.recorded_at - p.date)))
                        LIMIT 1),
                    tpms_pressure_rl = (
                        SELECT t.rl FROM xpengmate_tyres t
                        WHERE abs(extract(epoch FROM (t.recorded_at - p.date))) <= 3600
                        ORDER BY abs(extract(epoch FROM (t.recorded_at - p.date)))
                        LIMIT 1),
                    tpms_pressure_rr = (
                        SELECT t.rr FROM xpengmate_tyres t
                        WHERE abs(extract(epoch FROM (t.recorded_at - p.date))) <= 3600
                        ORDER BY abs(extract(epoch FROM (t.recorded_at - p.date)))
                        LIMIT 1)
                WHERE p.car_id = %s
                """,
                (self.car_id,),
            )
            updated = cur.rowcount
        self.conn.commit()
        _LOGGER.info("Attached tyre pressures to %d positions", updated)
        return updated

    def insert_states(self, intervals: list[tuple[dt.datetime, dt.datetime, str]]) -> int:
        inserted = 0
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT start_date, COALESCE(end_date, start_date) FROM states WHERE car_id = %s",
                (self.car_id,),
            )
            existing = [(row[0], row[1]) for row in cur.fetchall()]
            for start, end, state in intervals:
                if end <= start:
                    continue
                if any(start <= other_end and end >= other_start for other_start, other_end in existing):
                    continue
                key = f"{self.vin}:{start.isoformat()}"
                cur.execute(
                    """
                    INSERT INTO states (state, start_date, end_date, car_id)
                    VALUES (%s,%s,%s,%s) RETURNING id
                    """,
                    (state, start, end, self.car_id),
                )
                state_id = int(cur.fetchone()[0])
                self.remember("state", key, state_id)
                existing.append((start, end))
                inserted += 1
        self.conn.commit()
        _LOGGER.info("Imported %d states", inserted)
        return inserted


def _interpolate_range(
    range_start: float | None,
    range_end: float | None,
    soc_start: float | None,
    soc_end: float | None,
    level: int | None,
) -> float | None:
    """The range implied by this sample's SoC, between two measured endpoints.

    `charges.ideal_battery_range_km` is NOT NULL in TeslaMate, so a sample
    without a range cannot be written at all. EVConduit records the range only
    at the session's ends; between them it moves with SoC, so the interpolation
    is a straight line between two real measurements. When either end is missing
    the answer is None and the sample is skipped rather than made up.
    """
    if range_start is None or range_end is None:
        return None
    if soc_start is None or soc_end is None or soc_start == soc_end:
        return round(range_start, 2)
    if level is None:
        return None
    fraction = (level - soc_start) / (soc_end - soc_start)
    fraction = min(1.0, max(0.0, fraction))
    return round(range_start + (range_end - range_start) * fraction, 2)


# -- geometry/time helpers -----------------------------------


def _overlaps(start: dt.datetime, end: dt.datetime, windows: list[tuple]) -> bool:
    for other_start, other_end in windows:
        if start <= other_end and end >= other_start:
            return True
    return False


def _containing_drive(
    windows: list[tuple[dt.datetime, dt.datetime, int]],
    starts: list[dt.datetime],
    when: dt.datetime,
) -> int | None:
    index = bisect.bisect_right(starts, when) - 1
    if index < 0:
        return None
    start, end, drive_id = windows[index]
    if start <= when <= end:
        return drive_id
    return None


def _nearest_position(
    times: list[dt.datetime],
    positions: list[tuple[dt.datetime, int]],
    when: dt.datetime,
    tolerance: dt.timedelta = dt.timedelta(hours=6),
) -> int | None:
    if not times:
        return None
    index = bisect.bisect_left(times, when)
    candidates = []
    if index < len(times):
        candidates.append(positions[index])
    if index > 0:
        candidates.append(positions[index - 1])
    best: tuple[dt.datetime, int] | None = None
    for candidate in candidates:
        if best is None or abs(candidate[0] - when) < abs(best[0] - when):
            best = candidate
    if best is None or abs(best[0] - when) > tolerance:
        return None
    return best[1]


def derive_states(
    trips: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    merge_gap: dt.timedelta = dt.timedelta(minutes=10),
) -> list[tuple[dt.datetime, dt.datetime, str]]:
    """Online where there is evidence of activity, asleep across the gaps.

    The evidence is: a drive happened, a charge happened, or the GPS logger
    wrote a point. Intervals closer than `merge_gap` are one online stretch; the
    space between two stretches is asleep. `offline` is never emitted - the data
    cannot distinguish "asleep" from "not observed".
    """
    spans: list[tuple[dt.datetime, dt.datetime]] = []
    for trip in trips:
        start, end = naive_utc(trip.get("started_at")), naive_utc(trip.get("ended_at"))
        if start and end and end > start:
            spans.append((start, end))
    for session in sessions:
        start, end = naive_utc(session.get("started_at")), naive_utc(session.get("ended_at"))
        if start and end and end > start:
            spans.append((start, end))
    for point in positions:
        when = naive_utc(point.get("recorded_at"))
        if when:
            spans.append((when, when + dt.timedelta(seconds=30)))
    if not spans:
        return []
    spans.sort()
    merged: list[tuple[dt.datetime, dt.datetime]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start - last_end <= merge_gap:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))

    intervals: list[tuple[dt.datetime, dt.datetime, str]] = []
    for index, (start, end) in enumerate(merged):
        intervals.append((start, end, "online"))
        if index + 1 < len(merged):
            next_start = merged[index + 1][0]
            if next_start > end:
                intervals.append((end, next_start, "asleep"))
    return intervals


# -- entry point ---------------------------------------------


def run(args: argparse.Namespace) -> int:
    dsn = args.database_url or os.environ.get("XPENGMATE_DATABASE_URL", "")
    supabase_url = args.supabase_url or os.environ.get("SUPABASE_URL", "")
    service_key = args.service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    vin = args.vin or os.environ.get("XPENG_VIN", "")
    export_zip = args.export_zip or os.environ.get("XPENG_EXPORT_ZIP", "")
    if not dsn:
        print("XPENGMATE_DATABASE_URL is required", file=sys.stderr)
        return 2
    if not vin:
        print("XPENG_VIN is required", file=sys.stderr)
        return 2

    api = Evconduit(supabase_url, service_key)
    store = Store(dsn, vin, float(args.usable_kwh))
    try:
        store.prepare()
        if args.enrich_only:
            if not export_zip:
                print("--enrich-only needs --export-zip / XPENG_EXPORT_ZIP", file=sys.stderr)
                return 2
            matched = export_enrich.enrich(store, export_enrich.resolve_zip(export_zip))
            _LOGGER.info("Export joined onto %d positions", matched)
            return 0
        if not args.no_rebuild:
            removed = store.forget(
                ["charging_process", "drive", "position", "state"]
            )
            _LOGGER.info("Removed %d rows from the previous import", removed)

        trips = api.trips(vin)
        sessions = api.charging_sessions(vin)
        positions = api.positions(vin)

        live_drives = store.untracked_drives()
        live_charges = store.untracked_charge_windows()
        live_positions = store.untracked_position_dates()
        if live_drives or live_charges or live_positions:
            _LOGGER.info(
                "Live logger coverage found: %d drives / %d charges / %d positions "
                "left untouched",
                len(live_drives),
                len(live_charges),
                len(live_positions),
            )

        drive_windows = store.insert_drives(trips, live_drives)
        position_times = store.insert_positions(positions, drive_windows, live_positions)
        created = store.insert_charging_processes(sessions, position_times, live_charges)
        store.insert_charge_samples(created)
        store.import_tyre_pressures(api.tyre_pressures(vin))
        if export_zip:
            matched = export_enrich.enrich(store, export_enrich.resolve_zip(export_zip))
            _LOGGER.info("Export joined onto %d positions", matched)
        else:
            _LOGGER.info(
                "No XPENG export zip supplied; positions keep GPS only "
                "(set XPENG_EXPORT_ZIP to fill battery/odometer/range)"
            )
        store.insert_states(derive_states(trips, sessions, positions))
        _LOGGER.info("History import complete")
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--supabase-url", default="")
    parser.add_argument("--service-key", default="")
    parser.add_argument("--vin", default="")
    parser.add_argument("--usable-kwh", default="87.5")
    parser.add_argument(
        "--export-zip",
        default="",
        help="XPENG GDPR data export zip: fills SoC/odometer/range on positions",
    )
    parser.add_argument(
        "--enrich-only",
        action="store_true",
        help="only join an export zip onto the already-imported positions",
    )
    parser.add_argument(
        "--no-rebuild",
        action="store_true",
        help="keep this importer's previous rows instead of re-creating them",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
