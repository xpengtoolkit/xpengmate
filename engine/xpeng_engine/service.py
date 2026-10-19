"""The engine process: connect XPENG, keep state current, serve TeslaMate.

Two modes share the same state store and the same Tesla-compatible server:

* ``live`` - authenticate to XPENG, hold the mTLS MQTT subscription open, poll
  the REST supplements (XOS version, weather, vehicle identity) on a timer, and
  refresh the login token before it lapses.
* ``simulate`` - replay recorded telemetry from a file, no XPENG connection at
  all. This is how the full path (provider -> xpengmate -> Postgres) is tested
  without spending the one session an XPENG account is allowed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .admin import AdminServer
from .api_session import ApiSession
from .ha_source import HaSource
from .config import Config, ConfigError, load_config
from .proxy import ProviderProxy
from .runtime import load_runtime_config
from .mqtt_client import XPengMQTTClient
from .state import EngineState
from .teslamate_mapping import is_charging, is_driving
from .teslamate_provider import ProviderOptions, TeslaMateProvider

_LOGGER = logging.getLogger(__name__)

#: How often the status file is rewritten. Cheap, and it is the only way to see
#: inside the container without attaching a logger.
STATUS_INTERVAL = 10.0


def _uid_from_client_id(client_id: str) -> str:
    parts = (client_id or "").split(":")
    return parts[2] if len(parts) >= 3 else ""


def _write_json_atomic(path: str, payload: Any) -> None:
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        _LOGGER.debug("could not write %s", path, exc_info=True)


class Engine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.state = EngineState(
            vin=config.vin or "SIMULATED",
            usable_battery_kwh=config.usable_battery_kwh,
        )
        self.api: ApiSession | None = None
        self.mqtt: XPengMQTTClient | None = None
        self.provider: TeslaMateProvider | None = None
        self.relay: ProviderProxy | None = None
        self.admin: AdminServer | None = None
        self._stopping = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started_at = time.time()
        self._poll_kind = ""
        self._last_poll_at: float | None = None
        self._last_rest_at: float | None = None
        self._last_error: str | None = None

    # -- lifecycle --------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.state.attach_loop_scheduler(self._loop.call_soon_threadsafe)

        self.admin = AdminServer(
            config=self.config, status_provider=self._admin_status
        )
        await self.admin.async_start()

        if self.config.source == "provider":
            # Relay mode: the upstream (xpengtoolkit 0.6.16 provider) already
            # speaks Tesla's shapes, so the engine forwards rather than rebuilds.
            self.relay = ProviderProxy(
                upstream_url=self.config.upstream_url,
                upstream_token=self.config.upstream_token,
                local_token=self.config.provider_token,
                port=self.config.provider_port,
                bind=self.config.provider_bind,
            )
            await self.relay.async_start()
            _LOGGER.info(
                "Relaying %s through the local provider port %s",
                self.config.upstream_url,
                self.config.provider_port,
            )
            tasks = [asyncio.create_task(self._status_loop(), name="status")]
            await self._wait_for_stop(tasks)
            if self.relay is not None:
                await self.relay.async_stop()
            if self.admin is not None:
                await self.admin.async_stop()
            return

        options = ProviderOptions(
            token=self.config.provider_token,
            port=self.config.provider_port,
            bind=self.config.provider_bind,
        )
        self.provider = TeslaMateProvider(self.state, options)
        await self.provider.async_start()
        _LOGGER.info(
            "Tesla-compatible provider listening on %s", self.provider.url
        )

        tasks = [
            asyncio.create_task(self._status_loop(), name="status"),
        ]

        if self.config.simulate:
            tasks.append(asyncio.create_task(self._simulate_loop(), name="simulate"))
        elif self.config.source == "ha":
            tasks.append(asyncio.create_task(self._ha_loop(), name="ha"))
        else:
            try:
                await self._start_live()
            except Exception as exc:  # noqa: BLE001
                if not self.config.allow_offline:
                    raise
                _LOGGER.error(
                    "XPENG connection not started (%s); serving the provider only. "
                    "The car will report offline until the credentials are mounted.",
                    exc,
                )
                self._last_error = f"offline: {exc}"
                self.state.update_poll_status(mqtt_connected=False)
                tasks.extend(
                    [
                        asyncio.create_task(self._rest_loop(), name="rest"),
                    ]
                )
                await self._wait_for_stop(tasks)
                return
            tasks.extend(
                [
                    asyncio.create_task(self._watchdog_loop(), name="watchdog"),
                    asyncio.create_task(self._poll_loop(), name="poll"),
                    asyncio.create_task(self._rest_loop(), name="rest"),
                    asyncio.create_task(self._renew_loop(), name="renew"),
                ]
            )

        await self._wait_for_stop(tasks)

    async def _wait_for_stop(self, tasks: list[asyncio.Task[Any]]) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                self._loop.add_signal_handler(sig, self._stopping.set)
            except NotImplementedError:  # pragma: no cover - non-main thread
                pass

        await self._stopping.wait()
        _LOGGER.info("Shutting down")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._stop_live()
        if self.provider is not None:
            await self.provider.async_stop()
        if self.admin is not None:
            await self.admin.async_stop()

    # -- live XPENG session -----------------------------------

    async def _start_live(self) -> None:
        self.api = await asyncio.to_thread(
            ApiSession,
            self.config.cert_dir,
            self.config.tenant_file,
            self.config.token_file,
            self.config.vin,
        )
        await asyncio.to_thread(self._seed_rest_once)
        await asyncio.to_thread(self._refresh_xos)
        credentials = await asyncio.to_thread(self.api.mint_mqtt_credentials)
        if not credentials or not credentials.get("password"):
            raise ConfigError("could not mint an MQTT credential from XPENG")

        self.mqtt = XPengMQTTClient(
            client_id=credentials["client_id"],
            password=credentials["password"],
            server=credentials["server"],
            user_id=_uid_from_client_id(credentials["client_id"]),
            device_id=self.api.device_id,
            cert_dir=self.config.cert_dir,
            expires_at=credentials["expires_at"],
        )
        self.mqtt.set_credentials_provider(self._mint_credentials)
        self.mqtt.set_state_callback(self._on_vehicle_state)
        self.mqtt.set_auth_failure_callback(self._on_auth_failure)
        _LOGGER.info(
            "Connecting to XPENG MQTT as uid %s",
            _uid_from_client_id(credentials["client_id"]),
        )
        await asyncio.to_thread(self.mqtt.connect)

    async def _stop_live(self) -> None:
        if self.mqtt is not None:
            try:
                await asyncio.to_thread(self.mqtt.disconnect)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                _LOGGER.debug("MQTT disconnect failed", exc_info=True)
            self.mqtt = None

    def _mint_credentials(self) -> dict[str, Any] | None:
        assert self.api is not None
        try:
            minted = self.api.mint_mqtt_credentials()
            if minted:
                self.state.set_credentials_timestamp()
            return minted
        except Exception as exc:  # noqa: BLE001 - the client falls back
            self._last_error = f"credential mint failed: {exc}"
            _LOGGER.warning("MQTT credential mint failed", exc_info=True)
            return None

    def _on_vehicle_state(self, vehicle_data: dict[str, Any]) -> None:
        self.state.set_vehicle_data(vehicle_data)

    def _on_auth_failure(self) -> None:
        _LOGGER.warning("XPENG MQTT refused the credential; refreshing the token")
        if self.api is not None:
            try:
                self.api.refresh_tokens()
            except Exception:  # noqa: BLE001
                _LOGGER.warning("token refresh after auth failure failed", exc_info=True)

    # -- REST supplements -------------------------------------

    def _seed_rest_once(self) -> None:
        assert self.api is not None
        try:
            vehicle = self.api.refresh_vehicle_info()
            if vehicle:
                self.state.set_rest_data(vehicle)
                self.state.nick_name = (
                    vehicle.get("nickName")
                    or vehicle.get("seriesName")
                    or self.state.nick_name
                )
                if vehicle.get("seriesName"):
                    self.state.series_name = str(vehicle["seriesName"])
        except Exception:  # noqa: BLE001 - supplements are best effort
            _LOGGER.warning("vehicle list lookup failed", exc_info=True)

    def _refresh_xos(self) -> None:
        assert self.api is not None
        try:
            version = self.api.xos_version()
            if version:
                self.state.set_xos_version(version)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("XOS version lookup failed", exc_info=True)

    def _refresh_weather(self) -> None:
        assert self.api is not None and self.state.data
        from .teslamate_mapping import as_float, dig

        lat = as_float(dig(self.state.data, "interval", "drive", "latitude"))
        lon = as_float(dig(self.state.data, "interval", "drive", "longitude"))
        if lat is None or lon is None:
            return
        weather = self.api.weather(lat, lon)
        if weather:
            self.state.set_weather(weather)

    # -- loops ------------------------------------------------

    async def _watchdog_loop(self) -> None:
        interval = max(5.0, self.config.watchdog_interval)
        while True:
            await asyncio.sleep(interval)
            client = self.mqtt
            if client is None:
                continue
            connected = client.connected
            silence = self.state.silence_seconds()
            self.state.update_poll_status(
                mqtt_connected=connected,
                meaningful_silence_seconds=silence,
            )
            if not connected:
                continue
            if silence is not None and silence > self.config.silence_timeout:
                if client.last_meaningful_state_ts == 0:
                    continue
                age = time.time() - client.last_meaningful_state_ts
                if age > self.config.silence_timeout:
                    _LOGGER.warning(
                        "No vehicle state for %.0fs; reconnecting the MQTT session", age
                    )
                    try:
                        await asyncio.to_thread(client.reconnect, "silence watchdog")
                    except Exception:  # noqa: BLE001
                        _LOGGER.warning("reconnect failed", exc_info=True)

    def _is_active(self) -> bool:
        raw = self.state.data
        try:
            return not self.state_is_parked(raw)
        except Exception:  # noqa: BLE001
            return False

    def state_is_parked(self, raw: dict[str, Any]) -> bool:
        return not (is_driving(raw) or is_charging(raw))

    async def _poll_loop(self) -> None:
        while True:
            active = self._is_active()
            interval = (
                self.config.scan_interval if active else self.config.parked_scan_interval
            )
            await asyncio.sleep(interval)
            client = self.mqtt
            if client is None or not client.connected:
                continue
            try:
                if active:
                    await asyncio.to_thread(
                        client.send_command, self.config.vin, 19, {"cmd_type": 2}, 2
                    )
                    self._poll_kind = "force_upload"
                else:
                    await asyncio.to_thread(client.send_query, self.config.vin)
                    self._poll_kind = "passive"
                self._last_poll_at = time.time()
            except Exception:  # noqa: BLE001 - a poll is never fatal
                _LOGGER.debug("poll failed", exc_info=True)
            self.state.update_poll_status(
                last_query_kind=self._poll_kind,
                last_query_seconds_ago=0,
                next_poll_seconds=interval,
            )

    async def _rest_loop(self) -> None:
        next_ota = 0.0
        while True:
            await asyncio.sleep(max(30.0, self.config.rest_interval))
            self._last_rest_at = time.time()
            if self.api is None:
                continue
            try:
                await asyncio.to_thread(self._seed_rest_once)
                await asyncio.to_thread(self._refresh_weather)
                if time.time() >= next_ota:
                    next_ota = time.time() + self.config.ota_interval
                    await asyncio.to_thread(self._refresh_xos)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("REST supplement loop failed", exc_info=True)

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(300.0)
            if self.api is None:
                continue
            try:
                await asyncio.to_thread(self.api.ensure_tokens_fresh)
            except Exception:  # noqa: BLE001
                self._last_error = "token refresh failed"
                _LOGGER.warning("proactive token refresh failed", exc_info=True)

    # -- Home Assistant integration as the live source ----------

    def _on_ha_update(
        self,
        raw: dict,
        when: datetime | None,
        connected: bool,
        xos_version: str | None,
    ) -> None:
        if not raw:
            return
        self.state.update_poll_status(mqtt_connected=connected)
        if when is not None:
            self.state.set_raw_snapshot(raw, when=when)
        else:
            self.state.set_raw_snapshot(raw)
        if xos_version:
            version = str(xos_version).strip()
            if version.upper().startswith("XOS "):
                version = version[4:].strip()
            self.state.set_xos_version(version)

    async def _ha_loop(self) -> None:
        source = HaSource(
            self.config.ha_url,
            self.config.ha_token,
            self.config.ha_entity_prefix,
            self._on_ha_update,
        )
        self._last_error = None
        _LOGGER.info(
            "Reading live telemetry from Home Assistant at %s (%s)",
            self.config.ha_url,
            self.config.ha_entity_prefix,
        )
        await source.run()

    # -- simulation -------------------------------------------

    async def _simulate_loop(self) -> None:
        # There is no broker in simulation, but the provider reads the flag to
        # decide whether the car is reachable at all.
        self.state.update_poll_status(mqtt_connected=True)
        path = self.config.simulate_file
        _LOGGER.info("Simulating telemetry from %s", path)
        delay = max(0.0, self.config.simulate_delay)
        if delay:
            _LOGGER.info("Replay starts in %.0fs (XPENG_SIMULATE_DELAY)", delay)
            await asyncio.sleep(delay)
        speed = max(0.01, self.config.simulate_speed)
        while True:
            frames = _load_frames(path)
            if not frames:
                _LOGGER.error("no frames in %s; stopping", path)
                self._stopping.set()
                return
            _LOGGER.info("Replaying %d frames at %.1fx", len(frames), speed)
            previous: float | None = None
            for frame in frames:
                stamp = float(frame.get("time") or 0)
                delay = 0.0
                if previous is not None and stamp > previous:
                    delay = (stamp - previous) / speed
                previous = stamp
                if delay:
                    await asyncio.sleep(min(delay, 30.0))
                raw = frame.get("raw") or {}
                if raw:
                    self.state.set_raw_snapshot(
                        raw, when=datetime.now(timezone.utc)
                    )
            if not self.config.simulate_loop:
                _LOGGER.info("Replay finished")
                self._stopping.set()
                return

    # -- observability ----------------------------------------

    def _admin_status(self) -> dict[str, Any]:
        """What the settings page needs to see, in one call."""
        status: dict[str, Any] = {
            "mode": (
                "simulate"
                if self.config.simulate
                else ("provider" if self.config.source == "provider" else self.config.source)
            ),
            "vin": self.config.vin,
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "mqtt_connected": bool(
                self.mqtt.connected if self.mqtt is not None else False
            ),
            "last_data_timestamp": (
                self.state.last_data_timestamp.isoformat()
                if self.state.last_data_timestamp
                else None
            ),
            "soc": self.state.data.get("instant", {})
            .get("charge", {})
            .get("battery_soc")
            if isinstance(self.state.data, dict)
            else None,
            "odometer_km": self.state.data.get("instant", {})
            .get("odometer", {})
            .get("total_mileage")
            if isinstance(self.state.data, dict)
            else None,
            "xos_version": self.state.xos_version,
            "last_error": self._last_error,
        }
        if self.relay is not None:
            status.update(
                {
                    "upstream_url": self.config.upstream_url,
                    "relay_requests": self.relay.requests,
                    "relay_stream_frames": self.relay.frames,
                }
            )
        return status

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep(STATUS_INTERVAL)
            snap = self.state.snapshot()
            if self.config.source == "provider":
                status = {
                    "mode": "provider",
                    "vin": self.config.vin,
                    "uptime_seconds": round(time.time() - self._started_at, 1),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "upstream_url": self.config.upstream_url,
                    "provider_port": self.config.provider_port,
                    "relay_requests": self.relay.requests if self.relay else 0,
                    "relay_stream_frames": self.relay.frames if self.relay else 0,
                    "last_error": self._last_error,
                }
                _write_json_atomic(self.config.status_file, status)
                continue
            status = {
                "mode": (
                    "simulate"
                    if self.config.simulate
                    else (
                        "ha"
                        if self.config.source == "ha"
                        else ("provider" if self.config.source == "provider" else "live")
                    )
                ),
                "vin": self.state.vin,
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "mqtt_connected": bool(
                    self.mqtt.connected
                    if self.mqtt is not None
                    else self.state.poll_status.get("mqtt_connected", False)
                ),
                "credentials_valid": bool(
                    self.mqtt is not None and not self.mqtt.credentials_expired
                ),
                "meaningful_silence_seconds": self.state.silence_seconds(),
                "last_data_timestamp": (
                    self.state.last_data_timestamp.isoformat()
                    if self.state.last_data_timestamp
                    else None
                ),
                "charging": is_charging(self.state.data),
                "driving": is_driving(self.state.data),
                "soc": snap.raw.get("instant", {}).get("charge", {}).get("battery_soc")
                if isinstance(snap.raw, dict)
                else None,
                "odometer_km": snap.raw.get("instant", {})
                .get("odometer", {})
                .get("total_mileage")
                if isinstance(snap.raw, dict)
                else None,
                "xos_version": self.state.xos_version,
                "last_poll_kind": self._poll_kind,
                "last_poll_seconds_ago": (
                    round(time.time() - self._last_poll_at, 1)
                    if self._last_poll_at
                    else None
                ),
                "last_rest_seconds_ago": (
                    round(time.time() - self._last_rest_at, 1)
                    if self._last_rest_at
                    else None
                ),
                "provider_port": self.provider.bound_port if self.provider else None,
                "last_error": self._last_error,
            }
            _write_json_atomic(self.config.status_file, status)


def _load_frames(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, dict) and isinstance(payload.get("frames"), list):
        return payload["frames"]
    if isinstance(payload, list):
        return payload
    return []


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("xpeng_engine.mqtt_client").setLevel(logging.INFO)


async def async_main() -> int:
    try:
        base = load_config()
        config = load_runtime_config(base.runtime_file)
        config.validate()
    except ConfigError as exc:
        print(f"configuration error: {exc}", flush=True)
        return 2
    _configure_logging(config.log_level)
    engine = Engine(config)
    await engine.run()
    return 0


def main() -> int:
    return asyncio.run(async_main())
