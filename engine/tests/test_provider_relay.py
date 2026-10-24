#!/usr/bin/env python3
"""The relay that serves xpengtoolkit 0.6.16's provider to xpengmate.

A fake upstream provider stands in for the Home Assistant integration: it has
the four read routes and a streaming socket, and it authenticates with its own
token. The relay must forward all of it, keep xpengmate's local token private to
the upstream, and answer TeslaMate's sign-in itself.

    python3 -m unittest engine.tests.test_provider_relay -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None
    web = None

from xpeng_engine.proxy import ProviderProxy

LOCAL_TOKEN = "local-xpengmate-token"
UPSTREAM_TOKEN = "upstream-provider-token"
VIN = "L1NNSGHA3SB800783"


def build_vehicle() -> dict:
    return {
        "id": 1664585520,
        "vehicle_id": 1405951807,
        "vin": VIN,
        "display_name": "XPENG 800783",
        "state": "online",
        "charge_state": {
            "battery_level": 59,
            "battery_range": 196.3,
            # Fractional values, as xpengtoolkit reports a live XPENG charge.
            "charge_energy_added": 12.53,
            "charger_actual_current": 9.4,
            "charger_phases": 1.0,
            "charger_power": 6.9,
            "charger_voltage": 233.6,
        },
        "climate_state": {},
        "drive_state": {"shift_state": "P"},
        "vehicle_config": {},
        "vehicle_state": {"odometer": 38613.25, "car_version": "5.9.5.8909"},
    }


async def make_upstream(session_frames: list[dict]) -> tuple[web.Application, str]:
    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "provider": "fake-xpengtk", "vin_tail": "800783"})

    async def products(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {UPSTREAM_TOKEN}":
            return web.json_response({"error": "bad token"}, status=401)
        vehicle = build_vehicle()
        return web.json_response({"response": [vehicle], "count": 1})

    async def vehicle(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {UPSTREAM_TOKEN}":
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"response": build_vehicle()})

    async def vehicle_data(request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {UPSTREAM_TOKEN}":
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"response": build_vehicle()})

    async def streaming(request: web.Request) -> web.Response:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                continue
            payload = json.loads(message.data)
            if payload.get("msg_type") == "data:subscribe_oauth":
                if payload.get("token") != UPSTREAM_TOKEN:
                    await ws.send_json(
                        {"msg_type": "data:error", "value": "upstream token rejected"}
                    )
                    continue
                session_frames.append(payload)
                await ws.send_json(
                    {"msg_type": "data:update", "tag": payload.get("tag"), "value": "1,2"}
                )
        return ws

    app = web.Application()
    app.router.add_get("/healthz", health)
    app.router.add_get("/api/1/products", products)
    app.router.add_get("/api/1/vehicles/{id}", vehicle)
    app.router.add_get("/api/1/vehicles/{id}/vehicle_data", vehicle_data)
    app.router.add_get("/streaming/", streaming)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    app["runner"] = runner
    return app, f"http://127.0.0.1:{port}"


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class ProviderRelayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.frames: list[dict] = []
        self.upstream_app, self.upstream_url = await make_upstream(self.frames)
        self.proxy = ProviderProxy(
            upstream_url=self.upstream_url,
            upstream_token=UPSTREAM_TOKEN,
            local_token=LOCAL_TOKEN,
            port=0,
            bind="127.0.0.1",
        )
        await self.proxy.async_start()
        port = self.proxy._site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self) -> None:
        await self.proxy.async_stop()
        await self.upstream_app["runner"].cleanup()

    async def test_health_is_aggregated(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base}/healthz") as response:
                self.assertEqual(response.status, 200)
                health = await response.json()
        self.assertTrue(health["ok"])
        self.assertEqual(health["source"], "provider")
        self.assertTrue(health["upstream"]["ok"])

    async def test_token_endpoint_is_local(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base}/oauth2/v3/token",
                json={"grant_type": "refresh_token", "refresh_token": None},
            ) as response:
                self.assertEqual(response.status, 200)
                payload = await response.json()
        self.assertEqual(payload["access_token"], LOCAL_TOKEN)
        self.assertIsInstance(payload["expires_in"], int)

    async def test_reads_are_proxied_and_authenticated(self) -> None:
        headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base}/api/1/products") as response:
                self.assertEqual(response.status, 401)
            async with session.get(f"{self.base}/api/1/products", headers=headers) as response:
                self.assertEqual(response.status, 200)
                products = await response.json()
            self.assertEqual(products["response"][0]["vin"], VIN)

            async with session.get(
                f"{self.base}/api/1/vehicles/{products['response'][0]['id']}/vehicle_data",
                headers=headers,
            ) as response:
                data = await response.json()
            self.assertEqual(data["response"]["charge_state"]["battery_level"], 59)
            self.assertEqual(data["response"]["vehicle_state"]["car_version"], "5.9.5.8909")

    async def test_charge_integer_fields_are_normalised(self) -> None:
        """TeslaMate casts these to :integer and rejects floats ("is invalid")."""
        headers = {"Authorization": f"Bearer {LOCAL_TOKEN}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self.base}/api/1/products", headers=headers
            ) as response:
                products = await response.json()
            async with session.get(
                f"{self.base}/api/1/vehicles/{products['response'][0]['id']}/vehicle_data",
                headers=headers,
            ) as response:
                data = await response.json()
        charge = data["response"]["charge_state"]
        self.assertEqual(charge["charger_power"], 7)
        self.assertEqual(charge["charger_voltage"], 234)
        self.assertEqual(charge["charger_actual_current"], 9)
        self.assertEqual(charge["charger_phases"], 1)
        # Decimals TeslaMate stores as :decimal keep their precision.
        self.assertEqual(charge["charge_energy_added"], 12.53)
        self.assertEqual(charge["battery_range"], 196.3)

    async def test_streaming_rewrites_the_token(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"{self.base}/streaming/") as websocket:
                await websocket.send_json(
                    {
                        "msg_type": "data:subscribe_oauth",
                        "token": LOCAL_TOKEN,
                        "tag": "1405951807",
                        "value": "speed,odometer,soc",
                    }
                )
                message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        self.assertEqual(message["msg_type"], "data:update")
        self.assertEqual(message["tag"], "1405951807")
        # The upstream only accepts its own token; the relay must have swapped it.
        self.assertEqual(self.frames[0]["token"], UPSTREAM_TOKEN)

    async def test_streaming_rejects_a_bad_local_token(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(f"{self.base}/streaming/") as websocket:
                await websocket.send_json(
                    {"msg_type": "data:subscribe_oauth", "token": "wrong", "tag": "1"}
                )
                message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
        self.assertEqual(message["msg_type"], "data:error")
        self.assertEqual(self.frames, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
