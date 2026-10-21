"""Import an account's full XPENG history from EVConduit's data API.

Uses the versioned, cursor-paged surface at ``/api/v1/xpeng`` (see EVConduit's
``docs/xpeng-data-api.md``): trips, charging sessions, GPS positions, hourly
tyre pressures, charge curves and online/asleep state spans. Every collection is
paged to the end, so this is a real backfill by API key and no database access or
manual export download is required.

Rows are written through the same ``Store`` as the Supabase importer, so the
TeslaMate rows are identical in shape and the natural-key bookkeeping is shared.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any

from import_history import Store, naive_utc
from xpeng_api import ApiError, XpengDataApi, build_curve

_LOGGER = logging.getLogger("xpengmate.history.api")


def state_intervals(spans: list[dict[str, Any]]) -> list[tuple[datetime, datetime, str]]:
    intervals: list[tuple[datetime, datetime, str]] = []
    for span in spans:
        start = naive_utc(span.get("started_at"))
        end = naive_utc(span.get("ended_at"))
        state = str(span.get("state") or "").strip().lower()
        if start is None or end is None or end <= start:
            continue
        if state not in ("online", "asleep", "offline"):
            continue
        intervals.append((start, end, state))
    return intervals


def run() -> int:
    dsn = os.environ.get("XPENGMATE_DATABASE_URL", "")
    vin = os.environ.get("XPENG_VIN", "")
    base_url = os.environ.get("EVCONDUIT_API_URL", "")
    api_key = os.environ.get("EVCONDUIT_API_KEY", "")
    if not dsn or not vin or not base_url or not api_key:
        print(
            "XPENGMATE_DATABASE_URL, XPENG_VIN, EVCONDUIT_API_URL and "
            "EVCONDUIT_API_KEY are required",
            file=sys.stderr,
        )
        return 2

    client = XpengDataApi(base_url, api_key)
    try:
        trips = [trip for trip in client.trips(vin=vin) if _vin_matches(trip, vin)]
        sessions = [
            session
            for session in client.charging_sessions(vin=vin)
            if _vin_matches(session, vin)
        ]
        positions = [
            point for point in client.positions(vin=vin) if _vin_matches(point, vin, allow_empty=True)
        ]
        tyres = [
            reading
            for reading in client.tyre_pressures(vin=vin)
            if _vin_matches(reading, vin)
        ]
        curves = [
            curve for curve in client.charge_curves(vin=vin) if _vin_matches(curve, vin)
        ]
        spans = [
            span for span in client.state_spans(vin=vin) if _vin_matches(span, vin)
        ]
    except ApiError as exc:
        _LOGGER.error("EVConduit data API failed: %s", exc)
        return 1
    _LOGGER.info(
        "EVConduit data API: %d trips, %d charges, %d positions, %d tyre hours, "
        "%d curves, %d state spans (%d requests)",
        len(trips),
        len(sessions),
        len(positions),
        len(tyres),
        len(curves),
        len(spans),
        client.requests,
    )

    curves_by_id = {str(curve.get("id")): curve for curve in curves}
    for session in sessions:
        curve = curves_by_id.get(str(session.get("id")))
        if curve is not None:
            arrays = build_curve(curve)
            if arrays is not None:
                session["curve"] = arrays
                if curve.get("is_dc_fast") is not None:
                    session["is_dc_fast"] = curve.get("is_dc_fast")

    store = Store(dsn, vin, float(os.environ.get("XPENG_USABLE_BATTERY_KWH", "87.5")))
    try:
        store.prepare()
        removed = store.forget(["charging_process", "drive", "position", "state"])
        _LOGGER.info("Removed %d rows from the previous import", removed)
        live_drives = store.untracked_drives()
        live_charges = store.untracked_charge_windows()
        live_positions = store.untracked_position_dates()
        windows = store.insert_drives(trips, live_drives)
        position_times = store.insert_positions(positions, windows, live_positions)
        created = store.insert_charging_processes(sessions, position_times, live_charges)
        store.insert_charge_samples(created)
        store.import_tyre_pressures(tyres)
        intervals = state_intervals(spans)
        store.insert_states(intervals)
    finally:
        store.close()
    _LOGGER.info("EVConduit data API import complete")
    return 0


def _vin_matches(record: dict[str, Any], vin: str, *, allow_empty: bool = False) -> bool:
    value = record.get("vin")
    if value in (None, ""):
        return allow_empty
    return str(value).upper() == vin.upper()


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
