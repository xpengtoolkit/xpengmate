"""Relay mode: serve xpengmate from an upstream Tesla-compatible provider.

xpengtoolkit 0.6.16 can act as TeslaMate's API provider itself (see its
docs/TESLAMATE.md). That is the preferred Home Assistant source: it serves
Tesla's shapes directly, from the integration's live MQTT state, with the
integration's own auth and renewal - so the engine has nothing to translate and
xpengmate keeps one stable endpoint to point at.

This module is a transparent relay: the HTTP reads and the streaming websocket
are forwarded to the upstream provider with the upstream token, while xpengmate
still authenticates locally with its own TOKEN. The token endpoint is answered
locally because TeslaMate's sign-in only needs a 200 with an access token; it
never uses that token for HTTP.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None
    web = None

_LOGGER = logging.getLogger(__name__)

TOKEN_EXPIRES_IN_SECONDS = 3600


class ProviderProxy:
    """A Tesla-compatible façade over another Tesla-compatible provider."""

    def __init__(
        self,
        *,
        upstream_url: str,
        upstream_token: str,
        local_token: str,
        port: int,
        bind: str = "0.0.0.0",
        auth_path: str = "/oauth2/v3",
        timeout: float = 15.0,
        outside_temp: Any = None,
    ) -> None:
        self.upstream_url = upstream_url.rstrip("/")
        self._upstream_token = upstream_token
        self._local_token = local_token
        self._port = port
        self._bind = bind
        self._auth_path = auth_path
        self._timeout = timeout
        #: Optional OutsideTemperature reader: when set, its value replaces the
        #: upstream provider's climate_state.outside_temp on every read.
        self._outside_temp = outside_temp
        self._runner: Any = None
        self._site: Any = None
        self._session: "aiohttp.ClientSession | None" = None
        self._started_at = 0.0
        self.requests = 0
        self.frames = 0
        self._last_error: str | None = None
        self._last_outside_temp: float | None = None

    # -- lifecycle --------------------------------------------

    async def async_start(self) -> None:
        if web is None:
            raise RuntimeError("aiohttp is not available")
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout)
        )
        app = web.Application()
        app.router.add_get("/healthz", self._handle_health)
        app.router.add_post(f"{self._auth_path}/token", self._handle_token)
        app.router.add_get("/api/1/products", self._forward_products)
        app.router.add_get("/api/1/vehicles/{vehicle_id}", self._forward_vehicle)
        app.router.add_get(
            "/api/1/vehicles/{vehicle_id}/vehicle_data", self._forward_vehicle_data
        )
        app.router.add_get("/streaming/", self._handle_stream)
        app.router.add_get("/streaming", self._handle_stream)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._bind, self._port)
        await self._site.start()
        self._started_at = time.time()
        _LOGGER.info(
            "Provider relay listening on %s:%s -> %s",
            self._bind,
            self._port,
            self.upstream_url,
        )

    async def async_stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    # -- auth ------------------------------------------------

    def _authorized(self, request: "web.Request") -> bool:
        token = self._local_token
        if not token:
            return True
        if request.path in ("/healthz", "/streaming", "/streaming/"):
            return True
        header = request.headers.get("Authorization", "")
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip() == token:
            return True
        return request.query.get("token") == token

    def _subscribe_token_ok(self, token: Any) -> bool:
        if not self._local_token:
            return True
        return isinstance(token, str) and token.strip() == self._local_token

    # -- health ----------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        upstream: dict[str, Any] = {"url": self.upstream_url, "ok": False}
        started = time.monotonic()
        try:
            assert self._session is not None
            async with self._session.get(f"{self.upstream_url}/healthz") as response:
                upstream["status"] = response.status
                upstream["ok"] = response.status == 200
                try:
                    upstream["body"] = await response.json()
                except Exception:  # noqa: BLE001
                    upstream["body"] = (await response.text())[:400]
        except Exception as exc:  # noqa: BLE001
            upstream["error"] = str(exc)
        upstream["latency_ms"] = round((time.monotonic() - started) * 1000, 1)
        return web.json_response(
            {
                "ok": upstream["ok"],
                "provider": "xpengmate-relay",
                "source": "provider",
                "upstream": upstream,
                "outside_temp": {
                    "entity": getattr(self._outside_temp, "entity_id", None),
                    "celsius": getattr(self._outside_temp, "celsius", None),
                    "error": getattr(self._outside_temp, "last_error", None),
                },
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "requests": self.requests,
                "stream_frames": self.frames,
                "last_error": self._last_error,
            }
        )

    async def _handle_token(self, request: "web.Request") -> "web.Response":
        token = self._local_token or "relay"
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

    # -- passthrough -----------------------------------------

    async def _forward(self, path: str, query: str = "") -> "web.Response":
        self.requests += 1
        url = f"{self.upstream_url}{path}"
        if query:
            url = f"{url}?{query}"
        headers = {"Authorization": f"Bearer {self._upstream_token}"}
        try:
            assert self._session is not None
            async with self._session.get(url, headers=headers) as response:
                body = await response.read()
                return web.Response(
                    status=response.status,
                    body=body,
                    content_type=response.content_type,
                )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            _LOGGER.warning("upstream %s failed: %s", path, exc)
            return web.json_response(
                {"error": "upstream_unavailable", "error_description": str(exc)},
                status=502,
            )

    async def _forward_products(self, request: "web.Request") -> "web.Response":
        if not self._authorized(request):
            return self._unauthorized()
        return await self._forward("/api/1/products", request.query_string)

    async def _forward_vehicle(self, request: "web.Request") -> "web.Response":
        if not self._authorized(request):
            return self._unauthorized()
        vehicle_id = request.match_info["vehicle_id"]
        return await self._forward(f"/api/1/vehicles/{vehicle_id}", request.query_string)

    async def _forward_vehicle_data(self, request: "web.Request") -> "web.Response":
        if not self._authorized(request):
            return self._unauthorized()
        vehicle_id = request.match_info["vehicle_id"]
        path = f"/api/1/vehicles/{vehicle_id}/vehicle_data"
        self.requests += 1
        url = f"{self.upstream_url}{path}"
        if request.query_string:
            url = f"{url}?{request.query_string}"
        headers = {"Authorization": f"Bearer {self._upstream_token}"}
        try:
            assert self._session is not None
            async with self._session.get(url, headers=headers) as response:
                body = await response.read()
                if response.status != 200:
                    return web.Response(
                        status=response.status,
                        body=body,
                        content_type=response.content_type,
                    )
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            _LOGGER.warning("upstream %s failed: %s", path, exc)
            return web.json_response(
                {"error": "upstream_unavailable", "error_description": str(exc)},
                status=502,
            )
        body = await self._with_outside_temp(body)
        return web.Response(status=200, body=body, content_type="application/json")

    async def _with_outside_temp(self, body: bytes) -> bytes:
        """Replace climate_state.outside_temp with the Home Assistant sensor.

        TeslaMate stores this value on every position, so the drive and charge
        temperature averages follow the sensor the user chose.
        """
        if self._outside_temp is None:
            return body
        try:
            value = await self._outside_temp.async_get()
        except Exception:  # noqa: BLE001
            return body
        if value is None:
            return body
        try:
            payload = json.loads(body)
            vehicle = payload["response"] if isinstance(payload.get("response"), dict) else payload
            climate = vehicle.get("climate_state")
            if not isinstance(climate, dict):
                return body
            climate["outside_temp"] = value
            self._last_outside_temp = value
            return json.dumps(payload).encode()
        except Exception:  # noqa: BLE001
            return body

    def _unauthorized(self) -> "web.Response":
        return web.json_response(
            {"error": "invalid_token", "error_description": "Provider token missing or wrong"},
            status=401,
        )

    # -- streaming -------------------------------------------

    async def _handle_stream(self, request: "web.Request") -> "web.Response":
        ws_client = web.WebSocketResponse(heartbeat=25)
        await ws_client.prepare(request)
        ws_base = self.upstream_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        try:
            assert self._session is not None
            async with self._session.ws_connect(
                f"{ws_base}/streaming/", heartbeat=25
            ) as ws_upstream:
                tasks = [
                    asyncio.create_task(self._pump_client(ws_client, ws_upstream)),
                    asyncio.create_task(self._pump_upstream(ws_upstream, ws_client)),
                ]
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            _LOGGER.warning("streaming relay failed: %s", exc)
            try:
                await ws_client.close(
                    message=b"upstream streaming unavailable", code=1011
                )
            except Exception:  # noqa: BLE001
                pass
        return ws_client

    async def _pump_client(self, ws_client: Any, ws_upstream: Any) -> None:
        async for message in ws_client:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                payload = json.loads(message.data)
            except ValueError:
                continue
            if payload.get("msg_type") == "data:subscribe_oauth":
                if not self._subscribe_token_ok(payload.get("token")):
                    await ws_client.send_json(
                        {
                            "msg_type": "data:error",
                            "tag": payload.get("tag"),
                            "error_type": "client_error",
                            "value": "Can't validate token",
                        }
                    )
                    continue
                payload["token"] = self._upstream_token
                await ws_upstream.send_json(payload)
                continue
            await ws_upstream.send_str(message.data)

    async def _pump_upstream(self, ws_upstream: Any, ws_client: Any) -> None:
        async for message in ws_upstream:
            if message.type == aiohttp.WSMsgType.TEXT:
                self.frames += 1
                await ws_client.send_str(message.data)
            elif message.type in (
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSING,
                aiohttp.WSMsgType.CLOSED,
            ):
                break
