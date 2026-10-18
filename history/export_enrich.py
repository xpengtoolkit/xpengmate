"""Fill battery state onto imported positions from an XPENG data export.

EVConduit's GPS logger records where the car was, not how full the battery was.
XPENG's own GDPR export is the mirror image: it has 1 Hz SoC, odometer and range
for every second of a 30-day window, but no coordinates at all. Joining the two
by timestamp gives TeslaMate positions that carry both - which is what the
Battery Health, Charge Level and efficiency dashboards read.

The join is exact-second, with a 5 s tolerance for clock skew, and nothing is
interpolated across a gap: a position with no export sample within tolerance is
left with its battery fields NULL, exactly as it was imported.
"""

from __future__ import annotations

import csv
import heapq
import io
import logging
import zipfile
from pathlib import Path
from typing import Any, Iterable, Iterator

_LOGGER = logging.getLogger("xpengmate.export")

#: Sentinel values documented in the export field reference. They arrive on the
#: whole row at once, so every consumer must filter them, not clamp them.
SOC_SENTINEL = 255
RANGE_SENTINEL = 1600.0
ODOMETER_MAX = 2_000_000.0

#: How far a position may be from the export sample it borrows.
TOLERANCE_SECONDS = 5


def _float(value: str) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _members(archive: zipfile.ZipFile, suffix: str) -> list[str]:
    """The base/_part1/_part2 members for one dataset, in time order.

    The export splits each dataset into a base file and numbered parts that are
    contiguous and ascending, so the part number is the sort key. (Ordering by
    the file's *last* characters does not work: the base file ends in `_di`,
    which sorts after `part1` and `part2` as text.)
    """
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


def _rows(archive: zipfile.ZipFile, name: str, fields: dict[str, str]) -> Iterator[tuple[int, dict]]:
    with archive.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            try:
                timer = int(float(row.get("timer") or 0))
            except (TypeError, ValueError):
                continue
            values = {}
            for key, source in fields.items():
                values[key] = _float(row.get(source))
            yield timer, values


def _stream(archive: zipfile.ZipFile, members: list[str], fields: dict[str, str]) -> Iterator[tuple[int, dict]]:
    for name in members:
        yield from _rows(archive, name, fields)


def _merge(
    left: Iterator[tuple[int, dict]], right: Iterator[tuple[int, dict]]
) -> Iterator[tuple[int, dict]]:
    """Inner-join two timer-sorted streams on exactly equal timestamps."""
    a = next(left, None)
    b = next(right, None)
    while a is not None and b is not None:
        if a[0] < b[0]:
            a = next(left, None)
        elif b[0] < a[0]:
            b = next(right, None)
        else:
            yield a[0], {**a[1], **b[1]}
            a = next(left, None)
            b = next(right, None)


def iter_samples(zip_path: str) -> Iterator[tuple[int, dict]]:
    """Merged 1 Hz samples: (unix seconds, {soc, odometer_km, range_km})."""
    with zipfile.ZipFile(zip_path) as archive:
        operation = _stream(
            archive,
            _members(archive, "driving_operation_di"),
            {"odometer_km": "cdcu_totalodometer"},
        )
        energy = _stream(
            archive,
            _members(archive, "driving_power_energy_di"),
            {
                "soc": "ldcu_bms_soc_disp",
                "range_km": "ldcu_dstbatdisp_dynamic",
            },
        )
        yield from _merge(operation, energy)


def _clean(sample: dict) -> tuple[int | None, float | None, float | None]:
    soc = sample.get("soc")
    if soc is not None and (soc < 0 or soc >= SOC_SENTINEL):
        soc = None
    odometer = sample.get("odometer_km")
    if odometer is not None and (odometer < 0 or odometer > ODOMETER_MAX):
        odometer = None
    range_km = sample.get("range_km")
    if range_km is not None and (range_km <= 0 or range_km > RANGE_SENTINEL):
        range_km = None
    return (
        int(round(soc)) if soc is not None else None,
        round(odometer, 1) if odometer is not None else None,
        round(range_km, 2) if range_km is not None else None,
    )


def resolve_zip(path: str) -> str:
    """Accept a zip file or a directory holding export zips (newest wins).

    The export filenames change with every download, so pointing an env var at
    the directory is what keeps a scheduled sync from needing an edit each time.
    """
    candidate = Path(path)
    if candidate.is_file():
        return str(candidate)
    if candidate.is_dir():
        zips = sorted(
            candidate.glob("*.zip"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        if zips:
            return str(zips[0])
    raise FileNotFoundError(f"no export zip at {path}")


def enrich(store: Any, zip_path: str) -> int:
    """Join the export onto this car's positions, then re-derive drive ranges."""
    zip_path = resolve_zip(zip_path)
    with store.conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, extract(epoch FROM date)::bigint
            FROM positions
            WHERE car_id = %s
            ORDER BY date
            """,
            (store.car_id,),
        )
        positions = [(int(row[0]), int(row[1])) for row in cur.fetchall()]
    if not positions:
        _LOGGER.warning("no positions to enrich")
        return 0

    samples = iter_samples(zip_path)
    pending = next(samples, None)
    previous: tuple[int, dict] | None = None
    updates: list[tuple[int, int, float | None, float | None, float | None]] = []
    matched = 0
    for position_id, epoch_seconds in positions:
        while pending is not None and pending[0] < epoch_seconds - TOLERANCE_SECONDS:
            previous = pending
            pending = next(samples, None)
        chosen = None
        if pending is not None and abs(pending[0] - epoch_seconds) <= TOLERANCE_SECONDS:
            chosen = pending
        elif previous is not None and abs(previous[0] - epoch_seconds) <= TOLERANCE_SECONDS:
            chosen = previous
        if chosen is None:
            continue
        soc, odometer, range_km = _clean(chosen[1])
        if soc is None and odometer is None and range_km is None:
            continue
        updates.append((position_id, soc, odometer, range_km))
        matched += 1

    _LOGGER.info(
        "export covers %d of %d positions (%.1f%%)",
        matched,
        len(positions),
        100.0 * matched / len(positions),
    )
    with store.conn.cursor() as cur:
        cur.executemany(
            """
            UPDATE positions
            SET battery_level = COALESCE(%s, battery_level),
                usable_battery_level = COALESCE(%s, usable_battery_level),
                odometer = COALESCE(%s, odometer),
                est_battery_range_km = COALESCE(%s, est_battery_range_km),
                ideal_battery_range_km = COALESCE(%s, ideal_battery_range_km)
            WHERE id = %s
            """,
            [
                (soc, soc, odometer, range_km, range_km, position_id)
                for position_id, soc, odometer, range_km in updates
            ],
        )
        # Drive-level range figures, from the enriched positions. TeslaMate's
        # efficiency dashboards use (start_range - end_range), and a live logger
        # fills these when it closes a drive.
        cur.execute(
            """
            UPDATE drives d SET
                start_ideal_range_km = first.ideal_battery_range_km,
                end_ideal_range_km   = last.ideal_battery_range_km,
                start_rated_range_km = first.ideal_battery_range_km,
                end_rated_range_km   = last.ideal_battery_range_km,
                start_km = COALESCE(first.odometer, d.start_km),
                end_km   = COALESCE(last.odometer, d.end_km)
            FROM (
                SELECT DISTINCT ON (drive_id)
                    drive_id, ideal_battery_range_km, odometer
                FROM positions
                WHERE car_id = %s AND drive_id IS NOT NULL
                ORDER BY drive_id, date ASC
            ) first,
            (
                SELECT DISTINCT ON (drive_id)
                    drive_id, ideal_battery_range_km, odometer
                FROM positions
                WHERE car_id = %s AND drive_id IS NOT NULL
                ORDER BY drive_id, date DESC
            ) last
            WHERE d.id = first.drive_id
              AND d.id = last.drive_id
              AND d.car_id = %s
            """,
            (store.car_id, store.car_id, store.car_id),
        )
        drives = cur.rowcount
    store.conn.commit()
    _LOGGER.info("enriched drives: %d", drives)
    return matched
