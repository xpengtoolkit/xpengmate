"""Sleep detection from the xpengtoolkit integration's polling-status entity.

The provider reports the car as ``online`` whenever it has any data, and its
own asleep threshold is two hours. A parked XPENG is polled every so often, so
that threshold is never reached and TeslaMate sits in "online" forever.

The integration publishes what it is actually doing in
``sensor.<prefix>_polling_status``: ``Active`` while the car is awake, ``Silent``
once it has gone quiet, ``Parked`` after a passive poll. This reader maps that
onto "is the car asleep", with hysteresis so a passive poll that finds a sleeping
car does not wake the logger: only ``Active`` returns it to online.
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

DEFAULT_TTL_SECONDS = 30.0
DEFAULT_TIMEOUT_SECONDS = 5.0

ASLEEP_STATES = {"silent", "asleep", "sleeping", "sleep"}
AWAKE_STATES = {"active"}
# Parked / Reconnecting / unavailable: a passive poll or a transient socket
# problem says nothing about the car being awake, so keep the last decision.
NEUTRAL_STATES = {"parked", "reconnecting", "unavailable", "unknown", "off"}


class SleepStatus:
    """Read the integration's polling status and decide asleep vs online."""

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
        self.raw_status: str | None = None
        self._asleep: bool | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.ha_url and self._token and self.entity_id)

    @property
    def is_asleep(self) -> bool | None:
        return self._asleep

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

    async def async_get_status(self) -> str | None:
        """Refresh (at most every ``ttl`` seconds) and return the mapped state."""
        if not self.enabled:
            return None
        if (time.monotonic() - self._fetched_at) < self._ttl:
            return self.raw_status
        if self._session is None:
            await self.async_start()
        if self._session is None:
            return self.raw_status
        url = f"{self.ha_url}/api/states/{self.entity_id}"
        try:
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {self._token}"}
            ) as response:
                if response.status != 200:
                    self._error = f"HTTP {response.status}"
                    return self.raw_status
                payload: Any = await response.json()
        except Exception as exc:  # noqa: BLE001 - an outage must not break the relay
            self._error = str(exc)
            return self.raw_status
        value = str(payload.get("state") or "").strip().lower()
        self.raw_status = value
        self._fetched_at = time.monotonic()
        self._error = None
        self._apply(value)
        return self.raw_status

    def _apply(self, value: str) -> None:
        if value in AWAKE_STATES:
            if self._asleep is not False:
                _LOGGER.info("polling status active: car considered awake")
            self._asleep = False
        elif value in ASLEEP_STATES:
            if self._asleep is not True:
                _LOGGER.info("polling status silent: car considered asleep")
            self._asleep = True
        elif value in NEUTRAL_STATES:
            # Keep the previous decision; a parked poll does not wake the logger.
            return
        else:
            _LOGGER.debug("unhandled polling status %r", value)
