"""Import XPENG data-export zips into the xpengmate database.

The zips are rolling 30-day windows, so importing several of them describes the
same drives and charges more than once. This command:

1. parses every zip and merges the windows (`export_parser.merge_exports`), so a
   drive cut at a window edge is stitched and a drive described twice is kept
   once;
2. inserts only what is genuinely new: a record that overlaps an existing drive
   or charging session from any source (live logger, EVConduit, a previous
   export) is skipped, because those sources carry richer data - GPS in
   particular - and re-inserting would duplicate history;
3. attaches the export's SoC/odometer/range to existing GPS positions and the
   hourly tyre pressures to the nearest position;
4. records each imported file by content hash, so re-uploading the same file is
   a no-op and the settings page can list what has been imported.

Deliberately absent: positions. The export has no coordinates, and inventing
them is worse than leaving the maps empty.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import psycopg

import export_enrich
from export_parser import Charge, ExportData, MergedExport, Drive, parse_files
from import_history import Store, naive_utc

_LOGGER = logging.getLogger("xpengmate.export_import")

DRIVE_OVERLAP_RATIO = 0.3
CHARGE_OVERLAP_RATIO = 0.3
POSITION_LINK_TOLERANCE_HOURS = 6
MIN_STATE_SECONDS = 60


SCHEMA = """
CREATE TABLE IF NOT EXISTS xpengmate_export_imports (
    filename     text        PRIMARY KEY,
    sha256       text        NOT NULL UNIQUE,
    vin          text        NOT NULL DEFAULT '',
    filesize     bigint,
    timer_start  bigint,
    timer_end    bigint,
    samples      integer,
    drives       integer,
    charges      integer,
    tyre_hours   integer,
    imported_at  timestamptz NOT NULL DEFAULT now()
)
"""


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat()


class Importer:
    def __init__(self, dsn: str, vin: str, usable_kwh: float) -> None:
        self.store = Store(dsn, vin, usable_kwh)
        self.conn = self.store.conn
        self.imports: list[dict[str, Any]] = []
        self.added_drives = 0
        self.skipped_drives = 0
        self.merged_drives = 0
        self.merged_charges = 0
        self._last_drive_id = 0
        self._last_charge_id = 0
        self.added_charges = 0
        self.skipped_charges = 0
        self.added_states = 0
        self.skipped_states = 0
        self.tyre_updates = 0
        self.enriched = 0

    # -- schema / bookkeeping ---------------------------------

    def prepare(self) -> None:
        self.store.prepare()
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)
        self.conn.commit()

    def seen(self, sha256: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM xpengmate_export_imports WHERE sha256 = %s", (sha256,)
            )
            return cur.fetchone() is not None

    def record_file(self, data: ExportData, sha256: str, size: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO xpengmate_export_imports
                    (filename, sha256, vin, filesize, timer_start, timer_end,
                     samples, drives, charges, tyre_hours)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (filename) DO UPDATE SET
                    sha256 = EXCLUDED.sha256,
                    vin = EXCLUDED.vin,
                    filesize = EXCLUDED.filesize,
                    timer_start = EXCLUDED.timer_start,
                    timer_end = EXCLUDED.timer_end,
                    samples = EXCLUDED.samples,
                    drives = EXCLUDED.drives,
                    charges = EXCLUDED.charges,
                    tyre_hours = EXCLUDED.tyre_hours,
                    imported_at = now()
                """,
                (
                    Path(data.path).name,
                    sha256,
                    data.vin or self.store.vin,
                    size,
                    data.timer_start,
                    data.timer_end,
                    data.samples,
                    len(data.drives),
                    len(data.charges),
                    len(data.tyres),
                ),
            )
        self.conn.commit()
        self.imports.append(
            {
                "filename": Path(data.path).name,
                "vin": data.vin,
                "timer_start": data.timer_start,
                "timer_end": data.timer_end,
                "samples": data.samples,
                "drives": len(data.drives),
                "charges": len(data.charges),
                "tyre_hours": len(data.tyres),
            }
        )

    # -- existing coverage ------------------------------------

    def existing_windows(self, table: str, start_column: str = "start_date") -> list[tuple[int, int, int]]:
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT id,
                       extract(epoch FROM {start_column})::bigint,
                       COALESCE(extract(epoch FROM end_date)::bigint, extract(epoch FROM {start_column})::bigint)
                FROM {table}
                WHERE car_id = %s
                """,
                (self.store.car_id,),
            )
            return [(int(row[0]), int(row[1]), int(row[2])) for row in cur.fetchall()]

    def tracked_export_ids(self, kind: str) -> dict[int, str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT target_id, source_key FROM xpengmate_import WHERE kind = %s",
                (kind,),
            )
            return {int(row[0]): row[1] for row in cur.fetchall() if row[0] is not None}

    # -- drives -----------------------------------------------

    def import_drives(self, drives: list[Drive], existing: list[tuple[int, int, int]]) -> None:
        export_tracked = self.tracked_export_ids("export-drive")
        windows = [(start, end, row_id, row_id in export_tracked) for row_id, start, end in existing]
        for drive in drives:
            duration = max(1, drive.duration_seconds)
            overlapping = [
                (row_id, start, end, is_export)
                for start, end, row_id, is_export in windows
                if drive.start <= end and drive.end >= start
            ]
            if overlapping:
                # A longer version from a newer window replaces a truncated one.
                replace: tuple[int, int, int] | None = None
                for row_id, start, end, is_export in overlapping:
                    if is_export and drive.start <= start and drive.end >= end and duration > (end - start):
                        replace = (row_id, start, end)
                        break
                if replace is None:
                    self.skipped_drives += 1
                    continue
                with self.conn.cursor() as cur:
                    cur.execute("DELETE FROM drives WHERE id = %s", (replace[0],))
                    cur.execute(
                        "DELETE FROM xpengmate_import WHERE kind = 'export-drive' AND target_id = %s",
                        (replace[0],),
                    )
                self.merged_drives += 1
            self._insert_drive(drive)
            windows.append((drive.start, drive.end, self._last_drive_id, True))

    def _insert_drive(self, drive: Drive) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO drives
                    (start_date, end_date, distance, duration_min, start_km, end_km,
                     speed_max, power_max, power_min, outside_temp_avg, inside_temp_avg,
                     start_ideal_range_km, end_ideal_range_km, start_rated_range_km,
                     end_rated_range_km, ascent, descent, car_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    dt.datetime.fromtimestamp(drive.start, dt.timezone.utc).replace(tzinfo=None),
                    dt.datetime.fromtimestamp(drive.end, dt.timezone.utc).replace(tzinfo=None),
                    round(drive.distance_km, 2),
                    round(drive.duration_seconds / 60),
                    drive.odometer_start,
                    drive.odometer_end,
                    int(round(drive.speed_max)) if drive.speed_max is not None else None,
                    int(round(drive.power_max)) if drive.power_max is not None else None,
                    int(round(drive.power_min)) if drive.power_min is not None else None,
                    None,
                    None,
                    drive.range_start,
                    drive.range_end,
                    drive.range_start,
                    drive.range_end,
                    None,
                    None,
                    self.store.car_id,
                ),
            )
            drive_id = int(cur.fetchone()[0])
            self.store.remember("export-drive", drive.key(), drive_id)
            self._last_drive_id = drive_id
        self.conn.commit()
        self.added_drives += 1

    # -- charges ----------------------------------------------

    def import_charges(self, charges: list[Charge], existing: list[tuple[int, int, int]]) -> None:
        export_tracked = self.tracked_export_ids("export-charge")
        windows = [(start, end, row_id, row_id in export_tracked) for row_id, start, end in existing]
        for charge in charges:
            duration = max(1, charge.duration_seconds)
            overlapping = [
                (row_id, start, end, is_export)
                for start, end, row_id, is_export in windows
                if charge.start <= end and charge.end >= start
            ]
            if overlapping:
                replace: tuple[int, int, int] | None = None
                for row_id, start, end, is_export in overlapping:
                    if is_export and charge.start <= start and charge.end >= end and duration > (end - start):
                        replace = (row_id, start, end)
                        break
                if replace is None:
                    self.skipped_charges += 1
                    continue
                with self.conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM charging_processes WHERE id = %s", (replace[0],)
                    )
                    cur.execute(
                        "DELETE FROM xpengmate_import WHERE kind = 'export-charge' AND target_id = %s",
                        (replace[0],),
                    )
                self.merged_charges += 1
            self._insert_charge(charge)
            windows.append((charge.start, charge.end, self._last_charge_id, True))

    def _nearest_position(self, start_epoch: int) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT id FROM positions
                WHERE car_id = %s
                  AND date BETWEEN to_timestamp(%s) - interval '%s hours'
                               AND to_timestamp(%s) + interval '%s hours'
                ORDER BY abs(extract(epoch FROM date) - %s)
                LIMIT 1
                """,
                (
                    self.store.car_id,
                    start_epoch,
                    POSITION_LINK_TOLERANCE_HOURS,
                    start_epoch,
                    POSITION_LINK_TOLERANCE_HOURS,
                    start_epoch,
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None

    def _insert_charge(self, charge: Charge) -> None:
        position_id = self._nearest_position(charge.start)
        with self.conn.cursor() as cur:
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
                    dt.datetime.fromtimestamp(charge.start, dt.timezone.utc).replace(tzinfo=None),
                    dt.datetime.fromtimestamp(charge.end, dt.timezone.utc).replace(tzinfo=None),
                    charge.energy_dc_kwh or None,
                    charge.energy_input_kwh or None,
                    charge.soc_start,
                    charge.soc_end,
                    round(charge.duration_seconds / 60),
                    None,
                    self.store.car_id,
                    position_id,
                    None,
                    charge.range_start,
                    charge.range_end,
                    charge.range_start,
                    charge.range_end,
                    None,
                    None,
                ),
            )
            charge_id = int(cur.fetchone()[0])
            self._insert_charge_sample(cur, charge_id, charge)
            self.store.remember("export-charge", charge.key(), charge_id)
            self._last_charge_id = charge_id
        self.conn.commit()
        self.added_charges += 1

    def _insert_charge_sample(self, cur, charge_id: int, charge: Charge) -> None:
        """One synthetic charge sample.

        TeslaMate classifies AC/DC from the (mode of) ``charger_phases`` in the
        charge samples and derives battery capacity from the last sample of a
        session. Without any samples every imported session read as DC and the
        capacity panels stayed empty. The export only gives a 1 Hz aggregate, so
        one sample at the end of the session is enough to describe it.
        """
        is_dc = charge.peak_power_kw >= 20.0
        cur.execute(
            """
            INSERT INTO charges
                (date, battery_level, usable_battery_level, charge_energy_added,
                 charger_power, charger_phases, fast_charger_present,
                 ideal_battery_range_km, rated_battery_range_km, charging_process_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                dt.datetime.fromtimestamp(charge.end, dt.timezone.utc).replace(tzinfo=None),
                charge.soc_end,
                charge.soc_end,
                round(charge.energy_dc_kwh or charge.energy_input_kwh, 2),
                int(round(charge.mean_power_kw)),
                None if is_dc else 1,
                is_dc,
                charge.range_end or 0,
                charge.range_end or 0,
                charge_id,
            ),
        )

    # -- states -----------------------------------------------

    def import_states(self, online: list[tuple[int, int]], sleeping_gap: int = 300) -> None:
        existing = self.existing_windows("states")
        proposed: list[tuple[str, int, int]] = []
        for index, (start, end) in enumerate(online):
            if end - start >= MIN_STATE_SECONDS:
                proposed.append(("online", start, end))
            if index + 1 < len(online):
                next_start = online[index + 1][0]
                if next_start - end >= sleeping_gap:
                    proposed.append(("asleep", end, next_start))
        for state, start, end in proposed:
            if any(start <= existing_end and end >= existing_start for _id, existing_start, existing_end in existing):
                self.skipped_states += 1
                continue
            with self.conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO states (state, start_date, end_date, car_id)
                    VALUES (%s,%s,%s,%s) RETURNING id
                    """,
                    (
                        state,
                        dt.datetime.fromtimestamp(start, dt.timezone.utc).replace(tzinfo=None),
                        dt.datetime.fromtimestamp(end, dt.timezone.utc).replace(tzinfo=None),
                        self.store.car_id,
                    ),
                )
                state_id = int(cur.fetchone()[0])
                self.store.remember("export-state", f"export-state:{state}:{start}", state_id)
            self.conn.commit()
            existing.append((state_id, start, end))
            self.added_states += 1

    # -- tyres + enrichment -----------------------------------

    def import_tyres(self, tyres: dict[int, tuple]) -> None:
        rows = []
        for hour, values in sorted(tyres.items()):
            if not any(value is not None for value in values):
                continue
            rows.append((hour * 3600, *values))
        if not rows:
            return
        with self.conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS xpengmate_export_tyres")
            cur.execute(
                """
                CREATE TEMP TABLE xpengmate_export_tyres (
                    recorded_at bigint PRIMARY KEY,
                    fl numeric, fr numeric, rl numeric, rr numeric
                )
                """
            )
            cur.executemany(
                "INSERT INTO xpengmate_export_tyres VALUES (%s,%s,%s,%s,%s)", rows
            )
            cur.execute(
                """
                UPDATE positions p SET
                    tpms_pressure_fl = COALESCE(p.tpms_pressure_fl, (
                        SELECT t.fl FROM xpengmate_export_tyres t
                        WHERE abs(t.recorded_at - extract(epoch FROM p.date)) <= 3600
                        ORDER BY abs(t.recorded_at - extract(epoch FROM p.date)) LIMIT 1)),
                    tpms_pressure_fr = COALESCE(p.tpms_pressure_fr, (
                        SELECT t.fr FROM xpengmate_export_tyres t
                        WHERE abs(t.recorded_at - extract(epoch FROM p.date)) <= 3600
                        ORDER BY abs(t.recorded_at - extract(epoch FROM p.date)) LIMIT 1)),
                    tpms_pressure_rl = COALESCE(p.tpms_pressure_rl, (
                        SELECT t.rl FROM xpengmate_export_tyres t
                        WHERE abs(t.recorded_at - extract(epoch FROM p.date)) <= 3600
                        ORDER BY abs(t.recorded_at - extract(epoch FROM p.date)) LIMIT 1)),
                    tpms_pressure_rr = COALESCE(p.tpms_pressure_rr, (
                        SELECT t.rr FROM xpengmate_export_tyres t
                        WHERE abs(t.recorded_at - extract(epoch FROM p.date)) <= 3600
                        ORDER BY abs(t.recorded_at - extract(epoch FROM p.date)) LIMIT 1))
                WHERE p.car_id = %s
                """,
                (self.store.car_id,),
            )
            self.tyre_updates = cur.rowcount
        self.conn.commit()

    def enrich_from(self, paths: list[str]) -> None:
        for path in paths:
            try:
                self.enriched += export_enrich.enrich(self.store, path)
            except Exception:  # noqa: BLE001 - enrichment is additive
                _LOGGER.warning("could not enrich from %s", path, exc_info=True)

    # -- finish -----------------------------------------------

    def summary(self) -> dict[str, Any]:
        return {
            "added_drives": self.added_drives,
            "skipped_drives": self.skipped_drives,
            "merged_drives": self.merged_drives,
            "added_charges": self.added_charges,
            "skipped_charges": self.skipped_charges,
            "added_states": self.added_states,
            "skipped_states": self.skipped_states,
            "tyre_positions": self.tyre_updates,
            "enriched_positions": self.enriched,
            "files": self.imports,
        }


def import_exports(dsn: str, vin: str, usable_kwh: float, paths: list[str]) -> dict[str, Any]:
    importer = Importer(dsn, vin, usable_kwh)
    try:
        importer.prepare()
        fresh: list[str] = []
        for path in paths:
            sha = file_sha256(path)
            if importer.seen(sha):
                _LOGGER.info("%s was already imported (sha %s); skipping", Path(path).name, sha[:12])
                continue
            fresh.append(path)
        if not fresh:
            return {"ok": True, "duplicates": len(paths), **importer.summary()}

        merged: MergedExport = parse_files(fresh)

        importer.import_drives(merged.drives, importer.existing_windows("drives"))
        importer.import_charges(
            merged.charges, importer.existing_windows("charging_processes")
        )
        importer.import_states(merged.online)
        importer.import_tyres(merged.tyres)
        importer.enrich_from(fresh)
        # Recorded only after the rows landed, so a failed import can be retried.
        for path in fresh:
            data = next(item for item in merged.files if item.path == path)
            importer.record_file(data, file_sha256(path), os.path.getsize(path))
        summary = importer.summary()
        summary.update(
            {
                "ok": True,
                "duplicates": len(paths) - len(fresh),
                "files_imported": len(fresh),
                "vin": merged.vin or vin,
            }
        )
        _LOGGER.info("export import complete: %s", json.dumps(summary, default=str)[:800])
        return summary
    finally:
        importer.store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zips", nargs="*", help="export zip paths")
    parser.add_argument("--database-url", default=os.environ.get("XPENGMATE_DATABASE_URL", ""))
    parser.add_argument("--vin", default=os.environ.get("XPENG_VIN", ""))
    parser.add_argument(
        "--usable-kwh", default=os.environ.get("XPENG_USABLE_BATTERY_KWH", "87.5")
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    paths = args.zips or [
        item
        for item in os.environ.get("XPENG_EXPORT_ZIPS", "").split(os.pathsep)
        if item
    ]
    if not paths:
        print("no export zips given", file=sys.stderr)
        return 2
    if not args.database_url or not args.vin:
        print("XPENGMATE_DATABASE_URL and XPENG_VIN are required", file=sys.stderr)
        return 2
    summary = import_exports(
        args.database_url, args.vin, float(args.usable_kwh), paths
    )
    print(json.dumps(summary, indent=2, default=str))
    # A single line the service can find unambiguously in the log stream.
    print("EXPORT_IMPORT_SUMMARY " + json.dumps(summary, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
