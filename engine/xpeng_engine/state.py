"""The engine's state store, shaped like the Home Assistant coordinator.

``teslamate_mapping.snapshot_from_coordinator()`` reads a fixed set of attributes
off the integration's ``XPengDataCoordinator``. Reproducing that attribute
surface here - rather than forking the mapping - is deliberate: the mapping is
the part with the TeslaMate rules encoded in it (miles, millisecond timestamps,
the five state blocks) and it stays byte-for-byte upstream.

Threading: paho delivers telemetry on its network thread while the provider runs
on the asyncio loop. ``attach_loop_scheduler`` is called by the service with
``loop.call_soon_threadsafe`` so listeners always fire on the loop thread, which
is what ``TeslaMateProvider._on_telemetry`` expects.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from .teslamate_mapping import Telemetry, snapshot_from_coordinator

_LOGGER = logging.getLogger(__name__)


class EngineState:
    """Everything known about one car, in the attribute set the mapping reads.

    The attributes below are not arbitrary: each one is read by
    ``snapshot_from_coordinator`` or by the provider. They keep the coordinator's
    names on purpose so the vendored mapping needs no adapter.
    """

    def __init__(
        self,
        vin: str,
        usable_battery_kwh: float = 87.5,
        name: str | None = None,
    ) -> None:
        self.vin = vin
        self.usable_battery_kwh = float(usable_battery_kwh or 87.5)
        self.nick_name = name
        self.series_name: str | None = None
        self.vehicle_type_name: str | None = None
        self.xos_version: str | None = None
        self.weather: dict[str, Any] = {}
        self.vehicle_appearance: dict[str, Any] = {}
        self.data: dict[str, Any] = {}
        self.last_data_timestamp: datetime | None = None
        self.last_energy_data_timestamp: datetime | None = None
        self.poll_status: dict[str, Any] = {"mqtt_connected": False}
        self.credential_timestamp: datetime | None = None
        self.last_renewal: datetime | None = None
        self.state_is_restored = False

        self._lock = threading.RLock()
        self._listeners: list[Callable[[], None]] = []
        self._notify_scheduler: Callable[[Callable[[], None]], None] | None = None

    # -- listener plumbing ------------------------------------

    def attach_loop_scheduler(
        self, scheduler: Callable[[Callable[[], None]], None]
    ) -> None:
        """Install a thread-safe scheduler, normally ``loop.call_soon_threadsafe``."""
        self._notify_scheduler = scheduler

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(callback)

        def remove() -> None:
            with self._lock:
                if callback in self._listeners:
                    self._listeners.remove(callback)

        return remove

    def _notify(self) -> None:
        scheduler = self._notify_scheduler
        if scheduler is not None:
            scheduler(self._emit)
        else:
            self._emit()

    def _emit(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception:  # noqa: BLE001 - a listener must not stop the rest
                _LOGGER.warning("engine state listener failed", exc_info=True)

    # -- the SnapshotSource contract --------------------------

    def snapshots(self) -> Sequence[Telemetry]:
        return [self.snapshot()]

    def snapshot(self) -> Telemetry:
        with self._lock:
            return snapshot_from_coordinator(self)

    # -- writers ----------------------------------------------

    def set_vehicle_data(self, vehicle_data: dict[str, Any]) -> None:
        """Receive a decoded MQTT state from the paho network thread.

        ``vehicle_data`` is what ``XPengMQTTClient`` hands its state callback:
        ``{"vin", "msg_content", "has_fresh_energy_data"}``. The content is a
        full snapshot - the client merges partial frames into the last full one
        before calling us - so replacing ``self.data`` is correct and is what the
        coordinator does too.
        """
        content = (
            vehicle_data.get("msg_content")
            if isinstance(vehicle_data, dict)
            else None
        )
        if not isinstance(content, dict) or not content:
            return
        now = datetime.now(timezone.utc)
        with self._lock:
            self.data = content
            self.last_data_timestamp = now
            self.state_is_restored = False
            if vehicle_data.get("has_fresh_energy_data"):
                self.last_energy_data_timestamp = now
            self.poll_status["meaningful_silence_seconds"] = 0.0
        self._notify()

    def set_raw_snapshot(self, raw: dict[str, Any], when: datetime | None = None) -> None:
        """Install a state tree directly (HA source, simulator, REST fallback).

        ``when`` is the moment the *car* reported this data, which for the Home
        Assistant source is the integration's own ``last_data_update`` and can be
        minutes old on a sleeping car. Silence is therefore derived from that
        timestamp (``None`` makes the mapping compute it) rather than reset to
        zero - reporting a sleeping car as freshly heard is how it would never
        fall asleep in TeslaMate.
        """
        if not isinstance(raw, dict) or not raw:
            return
        moment = when or datetime.now(timezone.utc)
        with self._lock:
            self.data = raw
            self.last_data_timestamp = moment
            self.poll_status["meaningful_silence_seconds"] = (
                None if when is not None else 0.0
            )
        self._notify()

    def set_rest_data(self, data: dict[str, Any]) -> None:
        """Install REST-only supplements: appearance, name, series."""
        if not isinstance(data, dict):
            return
        with self._lock:
            appearance = data.get("appearance") or data.get("vehicleAppearance")
            if isinstance(appearance, dict) and appearance:
                self.vehicle_appearance = appearance
            for key in ("nickName", "nick_name", "vehicleName"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    self.nick_name = value.strip()
                    break
            for key in ("seriesName", "series_name"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    self.series_name = value.strip()
                    break
            for key in ("vehicleTypeName", "vehicle_type_name"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    self.vehicle_type_name = value.strip()
                    break

    def set_weather(self, weather: dict[str, Any]) -> None:
        if isinstance(weather, dict) and weather:
            with self._lock:
                self.weather = weather

    def set_xos_version(self, version: str | None) -> None:
        """Record the car's XOS/OTA version.

        TeslaMate writes a row to ``updates`` when ``vehicle_state.car_version``
        changes, so this single string is what turns XPENG OTA history into the
        TeslaMate Updates dashboard.
        """
        if not version:
            return
        version = str(version).strip()
        if not version or version == self.xos_version:
            return
        with self._lock:
            self.xos_version = version
        _LOGGER.info("XOS version is now %s", version)
        self._notify()

    def update_poll_status(self, **fields: Any) -> None:
        with self._lock:
            self.poll_status.update(fields)

    def set_credentials_timestamp(self) -> None:
        with self._lock:
            self.credential_timestamp = datetime.now(timezone.utc)

    def silence_seconds(self) -> float | None:
        with self._lock:
            last = self.last_data_timestamp
        if last is None:
            return None
        return max(0.0, time.time() - last.timestamp())
