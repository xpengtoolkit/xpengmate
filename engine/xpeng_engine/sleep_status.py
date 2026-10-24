"""Sleep detection from the xpengtoolkit integration's polling-status entity.

The provider reports the car as ``online`` whenever it has any data, and its
own asleep threshold is two hours. A parked XPENG is polled every so often, so
that threshold is never reached and TeslaMate sits in "online" forever.

The integration publishes what it is actually doing in
``sensor.<prefix>_polling_status``: ``Active`` while the car is awake, ``Silent``
once it has gone quiet, ``Parked`` after a passive poll. This reader maps that
onto "is the car asleep", with hysteresis so a passive poll that finds a sleeping
car does not wake the logger: only ``Active`` returns it to online.

The polling status alone is not enough. XPENG allows one telemetry session, so
when the car is being driven the integration's live socket is often the session
that loses out: it then reports ``Silent``/``Parked`` for the whole drive while
its passive cloud polls still refresh the odometer and battery. Waking only on
``Active`` therefore misses drives. The sibling odometer and battery entities are
read as a second arousal signal - a moving odometer or a battery step can only
happen while the car is awake - and keep it online for ``awake_hold`` seconds, so
a drive is logged even when the integration never says ``Active``.
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

#: How long a telemetry change keeps the car online. The integration's passive
#: cloud polls can be 15-30 minutes apart when its live socket is quiet, so a
#: shorter window would drop back to asleep in the middle of a drive.
DEFAULT_AWAKE_HOLD_SECONDS = 2700.0

#: Odometer movement bigger than this is a drive; a parked odometer is exact.
ODOMETER_STEP_KM = 0.1

#: A battery step this big only happens while the car is awake (charging,
#: climate, or the BMS rebalancing); smaller drift is noise.
BATTERY_STEP_PERCENT = 1.0

ASLEEP_STATES = {"silent", "asleep", "sleeping", "sleep"}
AWAKE_STATES = {"active"}
# Parked / Reconnecting / unavailable: a passive poll or a transient socket
# problem says nothing about the car being awake, so keep the last decision.
NEUTRAL_STATES = {"parked", "reconnecting", "unavailable", "unknown", "off"}


def sibling_entity(entity_id: str, suffix: str) -> str:
    """``sensor.xpeng_800783_polling_status`` -> ``sensor.xpeng_800783_<suffix>``.

    The integration exposes odometer and battery next to the polling status. If
    the configured entity is not the integration's polling status there is no
    sibling to read and the reader keeps the polling-status-only behaviour.
    """
    domain, _, leaf = (entity_id or "").partition(".")
    if not domain or not leaf.endswith("_polling_status"):
        return ""
    return f"{domain}.{leaf[: -len('_polling_status')]}_{suffix}"


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
        awake_hold: float = DEFAULT_AWAKE_HOLD_SECONDS,
        odometer_entity: str = "",
        battery_entity: str = "",
    ) -> None:
        self.ha_url = (ha_url or "").rstrip("/")
        self._token = ha_token or ""
        self.entity_id = (entity_id or "").strip()
        # Second arousal signal: telemetry that can only move while awake.
        self.odometer_entity = (
            (odometer_entity or "").strip() or sibling_entity(self.entity_id, "odometer")
        )
        self.battery_entity = (
            (battery_entity or "").strip() or sibling_entity(self.entity_id, "battery")
        )
        self._awake_hold = max(60.0, float(awake_hold))
        self._awake_until = 0.0
        self._last_odometer: float | None = None
        self._last_battery: float | None = None
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
        await self._async_check_telemetry()
        self._apply(value)
        return self.raw_status

    async def _async_check_telemetry(self) -> None:
        """Treat a moving odometer or battery as the car being awake.

        The polling status misses drives when the integration's live socket is
        quiet; its passive polls still refresh odometer and battery, so a step
        in either is proof the car is awake.
        """
        odometer = await self._async_read_number(self.odometer_entity)
        if odometer is not None:
            if (
                self._last_odometer is not None
                and odometer > self._last_odometer + ODOMETER_STEP_KM
            ):
                self._note_awake(f"odometer +{odometer - self._last_odometer:.1f} km")
            self._last_odometer = odometer
        battery = await self._async_read_number(self.battery_entity)
        if battery is not None:
            if (
                self._last_battery is not None
                and abs(battery - self._last_battery) >= BATTERY_STEP_PERCENT
            ):
                self._note_awake(
                    f"battery {self._last_battery:.0f}% -> {battery:.0f}%"
                )
            self._last_battery = battery

    async def _async_read_number(self, entity_id: str) -> float | None:
        if not entity_id or self._session is None:
            return None
        url = f"{self.ha_url}/api/states/{entity_id}"
        try:
            async with self._session.get(
                url, headers={"Authorization": f"Bearer {self._token}"}
            ) as response:
                if response.status != 200:
                    return None
                payload: Any = await response.json()
        except Exception:  # noqa: BLE001 - telemetry must not break the relay
            return None
        try:
            return float(str(payload.get("state") or "").strip())
        except ValueError:
            return None

    def _note_awake(self, reason: str) -> None:
        if self._asleep is not False or not self._awake_held():
            _LOGGER.info("telemetry moved (%s): car considered awake", reason)
        self._awake_until = time.monotonic() + self._awake_hold
        self._asleep = False

    def _awake_held(self) -> bool:
        return time.monotonic() < self._awake_until

    def _apply(self, value: str) -> None:
        if value in AWAKE_STATES:
            if self._asleep is not False:
                _LOGGER.info("polling status active: car considered awake")
            self._asleep = False
        elif value in ASLEEP_STATES:
            if self._awake_held():
                _LOGGER.debug(
                    "polling status %r, but telemetry moved recently; staying awake",
                    value,
                )
                return
            if self._asleep is not True:
                _LOGGER.info("polling status silent: car considered asleep")
            self._asleep = True
        elif value in NEUTRAL_STATES:
            # Keep the previous decision; a parked poll does not wake the logger.
            return
        else:
            _LOGGER.debug("unhandled polling status %r", value)
