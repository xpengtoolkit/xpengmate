"""Read XPENG history from EVConduit's Supabase/PostgREST tables.

EVConduit's own HTTP API caps the lists at 200 rows with no pagination and no
time filter, so it cannot serve a backfill; the tables underneath are the real
source. This client reads them read-only through PostgREST with a service-role
key, which is the credential EVConduit's own backend uses - keep it on the
server, never in a browser.

Only four tables are read, all by VIN:
  xpeng_trips, xpeng_charging_sessions, vehicle_positions, xpeng_tyre_pressures
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Any, Iterator

_LOGGER = logging.getLogger(__name__)

PAGE = 1000


class EvconduitError(RuntimeError):
    pass


class Evconduit:
    def __init__(self, supabase_url: str, service_key: str, timeout: float = 30.0) -> None:
        if not supabase_url or not service_key:
            raise EvconduitError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required to read EVConduit"
            )
        self.base = supabase_url.rstrip("/") + "/rest/v1"
        self._key = service_key
        self.timeout = timeout

    # -- transport --------------------------------------------

    def _get(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(params)
        url = f"{self.base}/{table}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "apikey": self._key,
                "Authorization": f"Bearer {self._key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
            body = exc.read().decode("utf-8", errors="replace")[:400]
            raise EvconduitError(f"{table}: HTTP {exc.code}: {body}") from exc
        except Exception as exc:  # noqa: BLE001
            raise EvconduitError(f"{table}: {exc}") from exc
        if not isinstance(payload, list):
            raise EvconduitError(f"{table}: unexpected payload {str(payload)[:200]}")
        return payload

    def _pages(self, table: str, params: dict[str, str]) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            page = self._get(
                table,
                {**params, "limit": str(PAGE), "offset": str(offset)},
            )
            for row in page:
                yield row
            if len(page) < PAGE:
                return
            offset += PAGE

    # -- tables -----------------------------------------------

    def trips(self, vin: str) -> list[dict[str, Any]]:
        rows = list(
            self._pages(
                "xpeng_trips",
                {
                    "select": "*",
                    "vin": f"eq.{vin}",
                    "order": "started_at.asc",
                },
            )
        )
        _LOGGER.info("EVConduit: %d trips", len(rows))
        return rows

    def charging_sessions(self, vin: str) -> list[dict[str, Any]]:
        rows = list(
            self._pages(
                "xpeng_charging_sessions",
                {
                    "select": "*",
                    "vin": f"eq.{vin}",
                    "order": "started_at.asc",
                },
            )
        )
        _LOGGER.info("EVConduit: %d charging sessions", len(rows))
        return rows

    def positions(self, vin: str, since: str | None = None) -> list[dict[str, Any]]:
        params = {
            "select": "recorded_at,latitude,longitude,speed_kmh,heading,altitude_m,accuracy_m,source",
            "vin": f"eq.{vin}",
            "order": "recorded_at.asc",
        }
        if since:
            params["recorded_at"] = f"gte.{since}"
        rows = list(self._pages("vehicle_positions", params))
        _LOGGER.info("EVConduit: %d positions", len(rows))
        return rows

    def tyre_pressures(self, vin: str) -> list[dict[str, Any]]:
        rows = list(
            self._pages(
                "xpeng_tyre_pressures",
                {"select": "*", "vin": f"eq.{vin}", "order": "recorded_at.asc"},
            )
        )
        _LOGGER.info("EVConduit: %d tyre readings", len(rows))
        return rows
