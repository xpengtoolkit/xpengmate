"""Import the recent history EVConduit's public API exposes, with an API key.

The xpengcardata API has no pagination: `/api/user/xpeng/trips` and
`/charging-sessions` return the newest 200 rows. That is enough for a rolling
recent window and their per-trip GPS tracks, but it is not a backfill - the
complete history needs EVConduit's database (`mode=supabase`). The settings page
says which one is configured and how far behind the data is.

Written through the same Store as the Supabase importer, so the rows are
identical in shape and the natural-key bookkeeping is shared.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from import_history import Store, derive_states, naive_utc

_LOGGER = logging.getLogger("xpengmate.history.api")


class ApiClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 30.0) -> None:
        self.base = base_url.rstrip("/")
        self._key = api_key
        self._timeout = timeout

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        url = f"{self.base}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def trips(self) -> list[dict[str, Any]]:
        payload = self._get("/api/user/xpeng/trips")
        return payload.get("trips") or []

    def charging_sessions(self) -> list[dict[str, Any]]:
        payload = self._get("/api/user/xpeng/charging-sessions")
        return payload.get("sessions") or []

    def tyre_pressures(self, vin: str) -> list[dict[str, Any]]:
        payload = self._get(
            "/api/user/xpeng/tyre-pressures", {"vin": vin, "limit": "2000"}
        )
        return payload.get("readings") or []

    def trip_track(self, trip_id: str, max_points: int = 1500) -> dict[str, Any]:
        return self._get(
            f"/api/user/xpeng/trips/{trip_id}/track",
            {"max_points": str(max_points)},
        )


def positions_from_tracks(
    client: ApiClient, trips: list[dict[str, Any]], vin: str
) -> list[dict[str, Any]]:
    """Turn each trip's simplified route into positions at its real times.

    The API exposes the route as parallel arrays of seconds-from-start and
    coordinates; the speed between points is not published, so it stays absent
    rather than being estimated from a decimated track.
    """
    positions: list[dict[str, Any]] = []
    for index, trip in enumerate(trips, start=1):
        trip_id = trip.get("id")
        started = naive_utc(trip.get("started_at"))
        if not trip_id or started is None:
            continue
        try:
            track = client.trip_track(str(trip_id))
        except Exception as exc:  # noqa: BLE001 - a missing track is ordinary
            _LOGGER.info("no track for trip %s: %s", trip_id, exc)
            continue
        seconds = track.get("seconds") or []
        lats = track.get("lat") or []
        lons = track.get("lon") or []
        for position_index, offset in enumerate(seconds):
            if position_index >= len(lats) or position_index >= len(lons):
                break
            try:
                when = started + timedelta(seconds=float(offset))
            except (TypeError, ValueError):
                continue
            positions.append(
                {
                    "recorded_at": when.replace(tzinfo=timezone.utc).isoformat(),
                    "latitude": lats[position_index],
                    "longitude": lons[position_index],
                    "speed_kmh": None,
                    "altitude_m": None,
                }
            )
        if index % 25 == 0:
            _LOGGER.info("tracks fetched for %d/%d trips", index, len(trips))
    return positions


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

    client = ApiClient(base_url, api_key)
    trips = [trip for trip in client.trips() if trip.get("vin") in ("", None, vin)]
    sessions = [s for s in client.charging_sessions() if s.get("vin") in ("", None, vin)]
    _LOGGER.info(
        "EVConduit API: %d trips, %d charging sessions (newest window)",
        len(trips),
        len(sessions),
    )
    positions = positions_from_tracks(client, trips, vin)
    _LOGGER.info("EVConduit API: %d track points", len(positions))
    tyres = client.tyre_pressures(vin)

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
        # The summary columns do not carry the per-session power curve, so there
        # are no charge samples in this mode; the session totals are still real.
        store.insert_charge_samples(created)
        store.import_tyre_pressures(tyres)
        store.insert_states(derive_states(trips, sessions, positions))
    finally:
        store.close()
    _LOGGER.info("API-window import complete")
    return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
