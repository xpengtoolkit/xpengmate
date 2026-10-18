"""A Tesla-compatible API provider for TeslaMate, served by this integration.

TeslaMate 4.x can read its vehicle data from a third party instead of Tesla.
Pointed at this server, it logs an XPENG exactly as if it were a Tesla: drives,
charging sessions, positions, battery history and the trip dashboards all come
from the integration's live MQTT state.

The contract, as the TeslaMate code actually consumes it:

    POST {auth_path}/token            OAuth refresh_token grant -> access_token
    GET  /api/1/products              [vehicle, ...]             (TeslaMate's list)
    GET  /api/1/vehicles/:id          vehicle (+ state)
    GET  /api/1/vehicles/:id/vehicle_data
                                      vehicle + the five state sections
    GET  /streaming/                  websocket, data:subscribe_oauth /
                                      data:update frames
    GET  /healthz                     diagnostics (no auth)

TeslaMate is put in "fleet/provider" mode with ``TOKEN`` (sent as
``Authorization: Bearer`` on every request) and ``TESLA_API_HOST``,
``TESLA_AUTH_HOST``/``TESLA_AUTH_PATH`` and ``TESLA_WSS_HOST`` pointed here.

The response bodies come from ``teslamate_mapping.py``; this module is only the
transport: token check, request routing, the streaming fan-out, and the small
amount of per-car bookkeeping the mapping cannot do on its own (monotonic
timestamps, last-known position, charge-session energy).

Design notes:

* One provider serves one config entry, i.e. one VIN. Multi-car accounts need
  one provider (and one TeslaMate instance) per car; see docs/TESLAMATE.md.
* It is a second aiohttp server inside the Home Assistant process, on its own
  port, not Home Assistant's own HTTP server: the paths are fixed by TeslaMate
  (``/api/1/...``) and would collide with Home Assistant's ``/api`` routes.
  Home Assistant integrations that run their own server (emulated_hue,
  homekit) are the precedent.
* Read-only. TeslaMate never sends commands in this version, and no command
  route is registered, so pointing it here cannot unlock or move the car.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping, Protocol, Sequence

from .teslamate_mapping import (
    Telemetry,
    as_float,
    as_int,
    build_vehicle,
    charge_cable_attached,
    charge_section,
    build_vehicle_data,
    dig,
    frame,
    is_charging,
    km_to_miles,
    shift_state,
    snapshot_from_coordinator,
    stable_ids,
    stream_value,
    tesla_vehicle_state,
)

try:  # aiohttp ships with Home Assistant; the plain unit tests run without it.
    from aiohttp import WSMsgType, web
except ImportError:  # pragma: no cover - only the no-aiohttp test environment
    web = None  # type: ignore[assignment]
    WSMsgType = None  # type: ignore[assignment]

_LOGGER = logging.getLogger(__name__)

#: Sent as the token endpoint's ``access_token``. The value is opaque to
#: TeslaMate: with ``TOKEN`` set its FleetAuth middleware overrides the header
#: with the provider token anyway, and with it unset TokenAuth sends this value,
#: which is the same secret. One token, no refresh dance to maintain.
TOKEN_EXPIRES_IN_SECONDS = 3600

#: Streaming heartbeat. TeslaMate closes the stream after 30 seconds of silence
#: (``TeslaApi.Stream``'s timer), so stay comfortably inside that.
HEARTBEAT_SECONDS = 15.0

#: Charging sessions are smoothed for this long. Measured on a G6: the car's
#: ``charging_state`` code alternates between 2 (Charging) and 15 mid-session
#: while power keeps flowing; ending a TeslaMate session on that blip would
#: split one charge into two.
CHARGING_GRACE_SECONDS = 120.0

# ── source protocol (Home Assistant in production, a list in tests) ─────────


class SnapshotSource(Protocol):
    """Where the provider gets telemetry from."""

    def snapshots(self) -> Sequence[Telemetry]:
        """The current snapshot per car."""

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call ``callback`` when new telemetry arrives; return a remover."""


class StaticSource:
    """A source over a list of snapshots, for tests."""

    def __init__(self, snapshots: Sequence[Telemetry]) -> None:
        self._snapshots = list(snapshots)
        self._listeners: list[Callable[[], None]] = []

    def snapshots(self) -> Sequence[Telemetry]:
        return self._snapshots

    def update(self, snapshots: Sequence[Telemetry]) -> None:
        self._snapshots = list(snapshots)
        for listener in list(self._listeners):
            listener()

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove


# ── per-car bookkeeping ─────────────────────────────────────


@dataclass
class _CarState:
    """State the mapping module cannot hold because it is not pure."""

    ids: tuple[int, int]
    last_timestamp_ms: int = 0
    last_latitude: float | None = None
    last_longitude: float | None = None
    charging: bool = False
    last_charge_evidence: float = 0.0
    charge_start_soc: int | None = None
    charge_start_range_miles: float | None = None
    energy_added_kwh: float | None = None
    range_added_miles: float | None = None
    seen_charge_session: bool = False

    @classmethod
    def for_vin(cls, vin: str) -> "_CarState":
        return cls(ids=stable_ids(vin))


@dataclass(frozen=True)
class ProviderOptions:
    """What the options flow stores."""

    token: str
    port: int
    bind: str = "0.0.0.0"
    auth_path: str = "/oauth2/v3"
    heartbeat_seconds: float = HEARTBEAT_SECONDS


class TeslaMateProvider:
    """The Tesla-compatible server. One instance per config entry."""

    def __init__(self, source: SnapshotSource, options: ProviderOptions) -> None:
        self._source = source
        self._options = options
        self._cars: dict[str, _CarState] = {}
        self._clients: dict[Any, str] = {}
        self._task: asyncio.Task[None] | None = None
        self._remove_listener: Callable[[], None] | None = None
        self._runner: Any = None
        self._site: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._requests = 0
        self._stream_frames = 0
        self._started_at = 0.0
        self._bound_port: int = 0

    # ── lifecycle ───────────────────────────────────────────

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self._options.bind in ("0.0.0.0", "::") else self._options.bind
        return f"http://{host}:{self.bound_port}"

    @property
    def bound_port(self) -> int:
        """The port actually bound (options.port, unless that was 0)."""
        return self._bound_port or self._options.port

    @property
    def stream_url(self) -> str:
        """What ``TESLA_WSS_HOST`` should be set to."""
        return "ws://" + self.url.split("://", 1)[1]

    async def async_start(self) -> None:
        """Bind the port and start heartbeat + change fan-out."""
        if web is None:
            raise RuntimeError("aiohttp is not available")
        self._loop = asyncio.get_running_loop()
        app = web.Application(middlewares=[self._make_auth_middleware()])
        app.router.add_get("/healthz", self._handle_health)
        app.router.add_post(
            f"{self._options.auth_path}/token", self._handle_token
        )
        app.router.add_get("/api/1/products", self._handle_products)
        app.router.add_get("/api/1/vehicles/{vehicle_id}", self._handle_vehicle)
        app.router.add_get(
            "/api/1/vehicles/{vehicle_id}/vehicle_data", self._handle_vehicle_data
        )
        app.router.add_get("/streaming/", self._handle_stream)
        app.router.add_get("/streaming", self._handle_stream)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(
            self._runner, self._options.bind, self._options.port
        )
        await self._site.start()
        self._bound_port = _site_port(self._site, self._options.port)
        if self._options.bind in ("0.0.0.0", "", "::"):
            # Best effort: on a dual-stack host also answer on IPv6, so a
            # TeslaMate container resolving the HA hostname to an AAAA record
            # can reach us. Failure (no IPv6) must not stop the IPv4 listener.
            try:
                ipv6 = web.TCPSite(self._runner, "::", self._bound_port)
                await ipv6.start()
            except OSError:
                _LOGGER.debug("no IPv6 listener on port %s", self._bound_port)
        self._started_at = time.time()
        self._remove_listener = self._source.add_listener(self._on_telemetry)
        self._task = asyncio.create_task(self._heartbeat_loop())
        _LOGGER.info(
            "TeslaMate provider listening on port %s of %s (token %s); "
            "TeslaMate uses TESLA_API_HOST=http://<this-host>:%s",
            self.bound_port,
            self._options.bind,
            "required" if self._options.token else "NOT configured - open access",
            self.bound_port,
        )

    async def async_stop(self) -> None:
        """Stop accepting, close streams and release the port."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._remove_listener is not None:
            try:
                self._remove_listener()
            except Exception:  # noqa: BLE001 - teardown must not raise
                _LOGGER.debug("removing provider listener failed", exc_info=True)
            self._remove_listener = None
        for ws in list(self._clients):
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        _LOGGER.info("TeslaMate provider stopped")

    # ── auth ────────────────────────────────────────────────

    def _make_auth_middleware(self) -> Callable[[Any, Any], Any]:
        """Build the token middleware.

        A closure rather than a decorated method: ``web.middleware`` wraps a
        two-argument function, and a bound method would receive ``self`` as the
        request. The provider token is ``TOKEN`` in TeslaMate, which sends it as
        ``Authorization: Bearer`` on every call including the token endpoint.
        """

        @web.middleware
        async def auth_middleware(request: Any, handler: Callable[[Any], Any]) -> Any:
            if self._authorized(request):
                return await handler(request)
            return web.json_response(
                {
                    "error": "invalid_token",
                    "error_description": "Provider token missing or wrong",
                },
                status=401,
            )

        return auth_middleware

    def _authorized(self, request: Any) -> bool:
        token = self._options.token
        if not token:
            return True
        if request.path in ("/healthz", "/streaming", "/streaming/"):
            # The streaming websocket cannot carry an Authorization header:
            # TeslaMate's WebSockex connects bare and authenticates inside the
            # ``data:subscribe_oauth`` frame instead. That check lives in
            # ``_on_stream_message``.
            return True
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip() == token:
            return True
        return request.query.get("token") == token

    def _subscribe_token_ok(self, token: Any) -> bool:
        if not self._options.token:
            return True
        return isinstance(token, str) and token.strip() == self._options.token

    # ── snapshot preparation ────────────────────────────────

    def _prepared(self) -> list[Telemetry]:
        return [self._prepare(snapshot) for snapshot in self._source.snapshots()]

    def _prepare(self, snapshot: Telemetry) -> Telemetry:
        """Apply the monotonic timestamp, last-known position and session state."""
        car = self._cars.setdefault(snapshot.vin, _CarState.for_vin(snapshot.vin))
        now = snapshot.now_ms or int(time.time() * 1000)

        # Strictly increasing: TeslaMate discards a fetch whose drive_state
        # timestamp goes backwards, and a live stream frame must never appear
        # older than the last poll's.
        timestamp = max(now, car.last_timestamp_ms + 1, snapshot.data_ms or 0)
        car.last_timestamp_ms = timestamp

        raw = snapshot.raw or {}
        latitude = as_float(dig(raw, "interval", "drive", "latitude"))
        longitude = as_float(dig(raw, "interval", "drive", "longitude"))
        if latitude is not None and longitude is not None:
            car.last_latitude, car.last_longitude = latitude, longitude

        charging = self._effective_charging(snapshot, car)

        return replace(
            snapshot,
            data_ms=timestamp,
            fallback_latitude=car.last_latitude,
            fallback_longitude=car.last_longitude,
            force_charging=True if charging else None,
            data_age_seconds=(
                snapshot.data_age_seconds
                if snapshot.data_age_seconds is not None
                else (
                    max(0.0, (now - snapshot.data_ms) / 1000.0)
                    if snapshot.data_ms
                    else None
                )
            ),
        )

    def _effective_charging(self, snapshot: Telemetry, car: _CarState) -> bool:
        """Charging, with a grace period so a state-code blip does not end it.

        Also feeds the session energy estimate: XPENG reports charger power but
        not energy added for the current session, so the provider integrates it
        from the battery percentage against the configured usable capacity.
        """
        now = snapshot.now_ms / 1000.0
        raw = snapshot.raw or {}
        evidence = is_charging(raw)
        if evidence:
            car.last_charge_evidence = now
        # The grace period only bridges a *state-code* blip: the cable has to
        # still be in the port. Without that condition an unplugged car would
        # keep reporting Charging for two minutes, delaying the next drive.
        cable = charge_cable_attached(charge_section(raw))
        charging = evidence or (
            car.charging
            and cable
            and (now - car.last_charge_evidence) < CHARGING_GRACE_SECONDS
        )
        if not charging:
            # Don't let a completed session's numbers leak into the next poll's
            # "not charging" summary; they are only read while charging anyway.
            car.charging = False
            return False

        soc = as_int(dig(raw, "instant", "charge", "battery_soc"))
        range_km = as_float(
            dig(raw, "instant", "odometer", "avalible_driving_distance")
        )
        range_miles = km_to_miles(range_km)
        if not car.charging:
            car.charging = True
            car.seen_charge_session = True
            car.charge_start_soc = soc
            car.charge_start_range_miles = range_miles
            car.energy_added_kwh = 0.0
            car.range_added_miles = 0.0
        if car.charge_start_soc is None and soc is not None:
            # The first frame of the session may predate the SOC reading.
            car.charge_start_soc = soc
        if car.charge_start_range_miles is None and range_miles is not None:
            car.charge_start_range_miles = range_miles
        if soc is not None and car.charge_start_soc is not None:
            delta = (soc - car.charge_start_soc) / 100.0 * snapshot.usable_kwh
            car.energy_added_kwh = round(max(0.0, delta), 2)
        if range_miles is not None and car.charge_start_range_miles is not None:
            car.range_added_miles = round(
                max(0.0, range_miles - car.charge_start_range_miles), 1
            )
        return True

    def _session_values(
        self, snapshot: Telemetry
    ) -> tuple[float | None, float | None]:
        car = self._cars.get(snapshot.vin)
        if car is None or not car.seen_charge_session:
            return None, None
        return car.energy_added_kwh, car.range_added_miles

    def _state_for(self, snapshot: Telemetry) -> str:
        if not snapshot.has_data:
            # Nothing has ever arrived for this VIN. TeslaMate should not wake the
            # car (there is nothing to wake with) or write a first position at the
            # fall-back (0, 0); offline is the honest answer until telemetry comes.
            return "offline"
        return tesla_vehicle_state(
            data_age_seconds=snapshot.data_age_seconds,
            mqtt_connected=snapshot.mqtt_connected,
            charging=bool(snapshot.force_charging),
            driving=shift_state(snapshot.raw) in ("D", "R", "N"),
        )

    def _find(self, requested: str) -> tuple[Telemetry, str] | None:
        try:
            wanted = int(requested)
        except (TypeError, ValueError):
            return None
        for snapshot in self._prepared():
            car = self._cars[snapshot.vin]
            if wanted in (car.ids[0], car.ids[1]):
                return snapshot, self._state_for(snapshot)
        return None

    # ── HTTP handlers ───────────────────────────────────────

    async def _handle_health(self, request: Any) -> Any:
        self._requests += 1
        snapshots = self._prepared()
        primary = snapshots[0] if snapshots else None
        return web.json_response(
            {
                "ok": True,
                "provider": "xpengtk-teslamate",
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "token_required": bool(self._options.token),
                "port": self.bound_port,
                "cars": len(snapshots),
                "vin_tail": primary.vin[-6:] if primary else None,
                "state": self._state_for(primary) if primary else None,
                "data_age_seconds": primary.data_age_seconds if primary else None,
                "stream_clients": len(self._clients),
                "stream_frames": self._stream_frames,
                "requests": self._requests,
            }
        )

    async def _handle_token(self, request: Any) -> Any:
        """OAuth's refresh_token grant, as TeslaMate calls it.

        TeslaMate posts a form/JSON body but the provider token is the only
        credential that matters: in fleet mode its FleetAuth middleware sends
        ``Authorization: Bearer <TOKEN>`` on this request too, so no refresh
        token is ever required from us.
        """
        self._requests += 1
        token = self._options.token or "xpengtk"
        return web.json_response(
            {
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": TOKEN_EXPIRES_IN_SECONDS,
                "refresh_token": token,
                "created_at": int(time.time()),
                "scope": "openid email offline_access",
            }
        )

    async def _handle_products(self, request: Any) -> Any:
        self._requests += 1
        vehicles = [
            build_vehicle(snapshot, state=self._state_for(snapshot),
                          ids=self._cars[snapshot.vin].ids)
            for snapshot in self._prepared()
        ]
        return web.json_response({"response": vehicles, "count": len(vehicles)})

    async def _handle_vehicle(self, request: Any) -> Any:
        self._requests += 1
        found = self._find(request.match_info["vehicle_id"])
        if found is None:
            return _not_found()
        snapshot, state = found
        vehicle = build_vehicle(
            snapshot, state=state, ids=self._cars[snapshot.vin].ids
        )
        return web.json_response({"response": vehicle})

    async def _handle_vehicle_data(self, request: Any) -> Any:
        self._requests += 1
        found = self._find(request.match_info["vehicle_id"])
        if found is None:
            return _not_found()
        snapshot, state = found
        energy, range_added = self._session_values(snapshot)
        vehicle = build_vehicle_data(
            snapshot,
            state=state,
            ids=self._cars[snapshot.vin].ids,
            energy_added_kwh=energy,
            range_added_miles=range_added,
        )
        return web.json_response({"response": vehicle})

    # ── streaming ───────────────────────────────────────────

    async def _handle_stream(self, request: Any) -> Any:
        ws = web.WebSocketResponse(heartbeat=25)
        await ws.prepare(request)
        try:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await self._on_stream_message(ws, message.data)
                elif message.type == WSMsgType.ERROR:
                    _LOGGER.debug("stream socket error: %s", ws.exception())
                    break
        finally:
            self._clients.pop(ws, None)
        return ws

    async def _on_stream_message(self, ws: Any, data: str) -> None:
        try:
            payload = json.loads(data)
        except (TypeError, ValueError):
            _LOGGER.debug("ignoring non-JSON streaming frame")
            return
        if not isinstance(payload, dict):
            return
        kind = payload.get("msg_type")
        tag = payload.get("tag")
        tag_text = str(tag) if tag is not None else ""
        if kind == "data:subscribe_oauth":
            if not self._subscribe_token_ok(payload.get("token")):
                await ws.send_json(
                    frame(
                        "data:error",
                        tag=tag_text,
                        error_type="client_error",
                        value="Can't validate token",
                    )
                )
                return
            self._clients[ws] = tag_text
            await self._send_update(ws, tag_text)
        elif kind == "data:unsubscribe":
            self._clients.pop(ws, None)
        else:
            _LOGGER.debug("ignoring streaming frame type %r", kind)

    async def _send_update(self, ws: Any, tag: str) -> None:
        snapshots = self._prepared()
        if not snapshots:
            return
        snapshot = snapshots[0]
        if self._state_for(snapshot) != "online":
            # A sleeping car is quiet, exactly like a sleeping Tesla: TeslaMate
            # relies on the silence to keep it in its asleep state.
            return
        await ws.send_json(
            frame("data:update", tag=tag,
                  value=stream_value(snapshot))
        )
        self._stream_frames += 1

    def _on_telemetry(self) -> None:
        """Called from the coordinator's listener; schedule a broadcast."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.create_task(self._broadcast())

    async def _broadcast(self) -> None:
        clients = list(self._clients.items())
        if not clients:
            return
        snapshots = self._prepared()
        if not snapshots:
            return
        snapshot = snapshots[0]
        if self._state_for(snapshot) != "online":
            return
        value = stream_value(snapshot)
        for ws, tag in clients:
            if getattr(ws, "closed", True):
                self._clients.pop(ws, None)
                continue
            try:
                await ws.send_json(frame("data:update", tag=tag, value=value))
                self._stream_frames += 1
            except Exception:  # noqa: BLE001 - a dead socket is not an error here
                self._clients.pop(ws, None)

    async def _heartbeat_loop(self) -> None:
        interval = max(1.0, float(self._options.heartbeat_seconds))
        while True:
            try:
                await asyncio.sleep(interval)
                if self._clients:
                    await self._broadcast()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a heartbeat must never kill the task
                _LOGGER.debug("stream heartbeat failed", exc_info=True)


def _site_port(site: Any, fallback: int) -> int:
    """The port a ``TCPSite`` actually bound (tests bind port 0)."""
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None) or []
    for sock in sockets:
        try:
            return int(sock.getsockname()[1])
        except (IndexError, OSError, TypeError, ValueError):
            continue
    return fallback


def _not_found() -> Any:
    return web.json_response(
        {"error": "not_found", "error_description": "Unknown vehicle"},
        status=404,
    )


# ── Home Assistant wiring ───────────────────────────────────


async def async_start_teslamate_provider(
    coordinator: Any, options: ProviderOptions
) -> TeslaMateProvider | None:
    """Start the provider for one config entry, or log and return None.

    A failure to bind (a port already in use, most likely a second entry with
    the same setting) must not take the whole integration down: the vehicle
    itself works fine without TeslaMate.
    """

    class _CoordinatorSource:
        def snapshots(self) -> Sequence[Telemetry]:
            return [snapshot_from_coordinator(coordinator)]

        def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
            return coordinator.async_add_listener(callback)

    provider = TeslaMateProvider(_CoordinatorSource(), options)
    try:
        await provider.async_start()
    except Exception:  # noqa: BLE001 - see docstring
        _LOGGER.error(
            "Could not start the TeslaMate provider on port %s",
            options.port,
            exc_info=True,
        )
        return None
    return provider
