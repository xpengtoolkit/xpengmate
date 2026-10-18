"""Live telemetry read from an XPENG Home Assistant integration.

XPENG allows one live session per account, so a second client on the same
account evicts the first. When the car is already connected to the
xpengtoolkit integration on a Home Assistant host, the honest way to log it from
a second system is to read that host's states - not to open another session.

This source subscribes to Home Assistant's `state_changed` events for one
entity prefix, rebuilds the same XPENG telemetry tree the native MQTT path
produces (`instant`/`interval`), and hands it to the engine's state store. The
Tesla provider above it cannot tell the difference.

The mapping below is deliberately the integration's own semantics, which are not
always the obvious ones:

* `driver_door_lock_st` is **1 = locked, 0 = unlocked**, and HA's lock binary
  sensor is **on = unlocked**, so it has to be inverted twice.
* Shift states are 0 N, 1 D, 2 R, 3/4 P.
* Charging state codes are 0 disconnected, 1 connected, 2 charging, 3 complete,
  4 error, 8 idle, 15 unlabelled.
* The integration's passthrough sensors are text ("Charging", "P"), not codes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

try:  # aiohttp is an engine dependency; the import is kept local for tests
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

_LOGGER = logging.getLogger(__name__)

SHIFT_STATES = {"N": 0, "D": 1, "R": 2, "P": 3}
CHARGING_STATES = {
    "Disconnected": 0,
    "Connected": 1,
    "Charging": 2,
    "Complete": 3,
    "Error": 4,
    "Idle": 8,
    "Scheduled": 15,
}

#: Everything the telemetry tree needs. Absent entities simply stay absent.
SENSORS = {
    "battery",
    "range",
    "odometer",
    "speed",
    "shift_state",
    "latitude",
    "longitude",
    "heading",
    "cabin_temperature",
    "set_temperature",
    "charge_power",
    "charge_voltage",
    "charge_current",
    "charging_state",
    "max_charge",
    "tyre_pressure_front_left",
    "tyre_pressure_front_right",
    "tyre_pressure_rear_left",
    "tyre_pressure_rear_right",
    "last_data_update",
    "xos_version",
}
BINARY_SENSORS = {
    "charging",
    "plugged_in",
    "lock",
    "driver_door",
    "passenger_door",
    "rear_left_door",
    "rear_right_door",
    "bonnet",
    "trunk",
    "sentinel_mode",
    "x_guard_camera",
    "mqtt_connected",
    "climate",
}

DOMAINS = {"sensor", "binary_sensor", "device_tracker"}


def _number(values: dict[str, str], key: str) -> float | None:
    raw = values.get(key)
    if raw is None:
        return None
    try:
        return float(str(raw).split()[0])
    except (TypeError, ValueError):
        return None


def _on(values: dict[str, str], key: str) -> bool | None:
    raw = values.get(key)
    if raw is None:
        return None
    return str(raw).strip().lower() in ("on", "true", "1", "open", "home")


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text in ("unknown", "unavailable", "None", ""):
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_raw(values: dict[str, str]) -> tuple[dict[str, Any], datetime | None, bool]:
    """Build the XPENG telemetry tree from HA entity states.

    Returns ``(raw, data_timestamp, mqtt_connected)``. The timestamp is the
    integration's own ``last_data_update``: the provider uses it to tell a
    parked-but-current car from a sleeping one, so inventing "now" here would
    keep a sleeping car looking online forever.
    """
    soc = _number(values, "battery")
    range_km = _number(values, "range")
    odometer = _number(values, "odometer")
    speed = _number(values, "speed")
    latitude = _number(values, "latitude")
    longitude = _number(values, "longitude")
    heading = _number(values, "heading")
    cabin = _number(values, "cabin_temperature")
    set_temp = _number(values, "set_temperature")
    charge_power = _number(values, "charge_power")
    charge_voltage = _number(values, "charge_voltage")
    charge_current = _number(values, "charge_current")
    max_charge = _number(values, "max_charge")
    plugged = _on(values, "plugged_in")
    shift_text = str(values.get("shift_state") or "").strip().upper()
    shift_code = SHIFT_STATES.get(shift_text)
    charging_text = str(values.get("charging_state") or "").strip()
    charging_code = CHARGING_STATES.get(charging_text, 15 if charging_text else None)
    if plugged and charging_code in (None, 0):
        charging_code = 1
    charging = _on(values, "charging")
    if charging is None:
        charging = (charging_code == 2) or bool(charge_power and charge_power > 0.1)
    driving = bool(speed and speed > 0.5) or shift_text in ("D", "R", "N")

    instant: dict[str, Any] = {
        "charge": {
            "battery_soc": int(round(soc)) if soc is not None else None,
            "charging_state": charging_code,
            "max_range_charge": int(round(max_charge)) if max_charge is not None else None,
            "charge_gun_status": 1 if plugged else 0,
            "charge_connector_status": 7 if plugged else 0,
            "slow_charge_port_status": 2 if plugged else 0,
        },
        "odometer": {
            "total_mileage": odometer,
            "avalible_driving_distance": range_km,
        },
        "drive": {"shift_state": shift_code},
        "ext": {
            "vehicleMode": 2 if driving else 0,
            "soldier_camera_status": 1 if _on(values, "sentinel_mode") or _on(values, "x_guard_camera") else 0,
        },
        "door": {
            "driver_door_lock_st": 0 if _on(values, "lock") else 1,
            "driver_door_ajar_st": 1 if _on(values, "driver_door") else 0,
            "passenger_door_ajar_st": 1 if _on(values, "passenger_door") else 0,
            "rear_left_door_ajar_st": 1 if _on(values, "rear_left_door") else 0,
            "rear_right_door_ajar_st": 1 if _on(values, "rear_right_door") else 0,
            "bonnet_ajar_st": 1 if _on(values, "bonnet") else 0,
        },
        "tail": {"tail_status": 2 if _on(values, "trunk") else 1},
        "tpms": {
            "tpms_pressure_fl": _number(values, "tyre_pressure_front_left"),
            "tpms_pressure_fr": _number(values, "tyre_pressure_front_right"),
            "tpms_pressure_rl": _number(values, "tyre_pressure_rear_left"),
            "tpms_pressure_rr": _number(values, "tyre_pressure_rear_right"),
        },
        "hvac": {
            "hvac_inner_temp_float": cabin,
            "hvac_temp_float": set_temp,
            "hvac_on": 1 if _on(values, "climate") else 0,
        },
    }
    interval: dict[str, Any] = {
        "drive": {
            "latitude": latitude,
            "longitude": longitude,
            "speed": speed,
            "angle": heading,
        },
        "charge": {
            "power": charge_power,
            "voltage": charge_voltage,
            "electricity": charge_current,
        },
    }
    raw = {"instant": instant, "interval": interval}
    connected = _on(values, "mqtt_connected")
    return raw, parse_timestamp(values.get("last_data_update")), bool(connected) if connected is not None else True


class HaSource:
    """Subscribes to one XPENG integration's entities and publishes telemetry."""

    def __init__(
        self,
        base_url: str,
        token: str,
        entity_prefix: str,
        on_update: Callable[[dict[str, Any], datetime | None, bool, str | None], None],
        *,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.prefix = entity_prefix.strip()
        self.on_update = on_update
        self.timeout = timeout
        self.values: dict[str, str] = {}
        self._last_published: str | None = None
        self._session: Any = None

    # -- entity bookkeeping -----------------------------------

    def _suffix(self, entity_id: str) -> str | None:
        domain, _, object_id = entity_id.partition(".")
        if domain not in DOMAINS:
            return None
        prefix = f"{self.prefix}_"
        if not object_id.startswith(prefix):
            return None
        suffix = object_id[len(prefix):]
        if domain == "sensor" and suffix in SENSORS:
            return suffix
        if domain == "binary_sensor" and suffix in BINARY_SENSORS:
            return suffix
        if domain == "device_tracker" and suffix == "location":
            return "location"
        return None

    def apply_state(self, entity_id: str, state: str) -> bool:
        suffix = self._suffix(entity_id)
        if suffix is None:
            return False
        if suffix == "location":
            # device_tracker carries coordinates in attributes only.
            return False
        self.values[suffix] = state
        return True

    def apply_attributes(self, entity_id: str, attributes: dict[str, Any]) -> bool:
        if self._suffix(entity_id) != "location":
            return False
        latitude = attributes.get("latitude")
        longitude = attributes.get("longitude")
        if latitude is None or longitude is None:
            return False
        self.values["latitude"] = str(latitude)
        self.values["longitude"] = str(longitude)
        return True

    def publish(self, force: bool = False) -> None:
        raw, when, connected = build_raw(self.values)
        stamp = when.isoformat() if when else None
        if not force and stamp is not None and stamp == self._last_published:
            return
        self._last_published = stamp
        self.on_update(raw, when, connected, self.values.get("xos_version"))

    # -- transport --------------------------------------------

    async def _initial_states(self, session: "aiohttp.ClientSession") -> None:
        headers = {"Authorization": f"Bearer {self._token}"}
        async with session.get(
            f"{self.base_url}/api/states", headers=headers, timeout=self.timeout
        ) as response:
            response.raise_for_status()
            states = await response.json()
        count = 0
        for state in states or []:
            entity_id = state.get("entity_id", "")
            if self.apply_state(entity_id, str(state.get("state", ""))):
                count += 1
            if self.apply_attributes(entity_id, state.get("attributes") or {}):
                count += 1
        _LOGGER.info("HA source: %d relevant entities under %s", count, self.prefix)
        self.publish(force=True)

    async def run(self) -> None:
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for the HA source")
        backoff = 5
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    self._session = session
                    await self._initial_states(session)
                    backoff = 5
                    await self._listen(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect forever
                _LOGGER.warning("HA source disconnected (%s); retrying in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self._session = None

    async def _listen(self, session: "aiohttp.ClientSession") -> None:
        ws_url = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        async with session.ws_connect(f"{ws_url}/api/websocket", timeout=self.timeout) as ws:
            first = await ws.receive_json()
            if first.get("type") != "auth_required":
                _LOGGER.debug("unexpected HA websocket greeting: %s", first.get("type"))
            await ws.send_json({"type": "auth", "access_token": self._token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA authentication failed: {auth.get('type')}")
            await ws.send_json(
                {
                    "id": 1,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
            )
            _LOGGER.info("HA source: subscribed to state_changed")
            async for message in ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                payload = json.loads(message.data)
                if payload.get("type") != "event":
                    continue
                event = payload.get("event") or {}
                data = event.get("data") or {}
                entity_id = data.get("entity_id", "")
                new_state = data.get("new_state") or {}
                changed = self.apply_state(entity_id, str(new_state.get("state", "")))
                changed = (
                    self.apply_attributes(entity_id, new_state.get("attributes") or {})
                    or changed
                )
                if changed:
                    self.publish()
