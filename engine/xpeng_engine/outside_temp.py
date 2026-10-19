"""Outside temperature from a Home Assistant sensor.

The XPENG provider reports its own weather value in ``climate_state``, but the
car's own weather entity (for example
``sensor.xpeng_800783_weather_at_vehicle``) is the value the user trusts. This
reader fetches that entity's state and the relay substitutes it into the
vehicle_data response, which is what TeslaMate stores on every position and
therefore what the drive/charge temperature averages use.
"""

from __future__ import annotations

import logging
import time
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 60.0
DEFAULT_TIMEOUT_SECONDS = 5.0


class OutsideTemperature:
    """Reads a Home Assistant temperature entity, cached for a short while."""

    def __init__(
        self,
        ha_url: str,
        ha_token: str,
        entity_id: str,
        *,
        ttl: float = DEFAULT_TTL_SECONDS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.ha_url = (ha_url or "").rstrip("/")
        self._token = ha_token or ""
        self.entity_id = (entity_id or "").strip()
        self._ttl = ttl
        self._timeout = timeout
        self._session: "aiohttp.ClientSession | None" = None
        self._value: float | None = None
        self._fetched_at = 0.0
        self._error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.ha_url and self._token and self.entity_id)

    @property
    def celsius(self) -> float | None:
        return self._value

    @property
    def last_error(self) -> str | None:
        return self._error

    async def async_start(self) -> None:
        if aiohttp is None or not self.enabled or self._session is not None:
            return
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )

    async def async_close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def async_get(self) -> float | None:
        if not self.enabled:
            return None
        if self._value is not None and (time.monotonic() - self._fetched_at) <= self._ttl:
            return self._value
        if self._session is None:
            await self.async_start()
        if self._session is None:
            return self._value
        url = f"{self.ha_url}/api/states/{self.entity_id}"
        try:
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {self._token}"}
            ) as response:
                if response.status != 200:
                    self._error = f"HTTP {response.status}"
                    return self._value
                payload: Any = await response.json()
        except Exception as exc:  # noqa: BLE001 - a sensor hiccup must not break the relay
            self._error = str(exc)
            _LOGGER.debug("outside temperature %s unavailable: %s", self.entity_id, exc)
            return self._value
        value = _celsius(payload)
        if value is None:
            self._error = "sensor has no numeric temperature"
            return self._value
        self._value = value
        self._fetched_at = time.monotonic()
        self._error = None
        return value


def _celsius(payload: dict[str, Any]) -> float | None:
    attributes = payload.get("attributes") or {}
    try:
        value = float(payload.get("state"))
    except (TypeError, ValueError):
        return None
    unit = str(attributes.get("unit_of_measurement") or "").lower()
    if "f" in unit and "c" not in unit:
        value = (value - 32.0) * 5.0 / 9.0
    return round(value, 1)
