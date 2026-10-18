#!/usr/bin/env python3
"""End-to-end check of the provider half of the engine, with no XPENG account.

Runs the same sequence xpengmate performs on sign-in and on every poll:

    POST /oauth2/v3/token  -> GET /api/1/products -> GET /api/1/vehicles/{id}
    -> GET /api/1/vehicles/{id}/vehicle_data -> websocket /streaming/

and asserts the TeslaMate rules the vendored mapping exists to satisfy: integer
ids, all five state blocks, millisecond timestamps, an integer `expires_in`, and
a 13-field stream value.

    python3 -m unittest engine.tests.test_offline_provider -v
"""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover - optional dependency for this test
    aiohttp = None
    web = None

from xpeng_engine.state import EngineState
from xpeng_engine.teslamate_provider import ProviderOptions, TeslaMateProvider

TOKEN = "offline-provider-test-token"
VIN = "L1NNSGHA3SB800783"

#: A parked G6 snapshot, the shape a merged MQTT payload has.
PARKED_RAW = {
    "instant": {
        "charge": {
            "battery_soc": 64,
            "max_range_charge": 90,
            "charging_state": 15,
            "charge_gun_status": 0,
            "charge_connector_status": 7,
            "slow_charge_port_status": 2,
        },
        "odometer": {"total_mileage": 12345.6, "avalible_driving_distance": 412.0},
        "drive": {"shift_state": 3},
        "ext": {"vehicleMode": 0, "soldier_camera_status": 0},
        "door": {"driver_door_lock_st": 2, "driver_door_ajar_st": 0},
        "tpms": {
            "tpms_pressure_fl": 265.0,
            "tpms_pressure_fr": 264.0,
            "tpms_pressure_rl": 266.0,
            "tpms_pressure_rr": 267.0,
        },
        "hvac": {"hvac_inner_temp_float": 21.5, "hvac_temp_float": 22.0},
    },
    "interval": {
        "drive": {"latitude": -33.8688, "longitude": 151.2093, "speed": 0, "angle": 90},
        "charge": {"power": 0, "voltage": 0, "electricity": 0},
    },
}


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class OfflineProviderTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = EngineState(VIN, usable_battery_kwh=87.5, name="XPeng G6")
        self.state.set_raw_snapshot(PARKED_RAW)
        self.state.set_xos_version("5.9.5")
        self.state.update_poll_status(mqtt_connected=True)
        self.provider = TeslaMateProvider(
            self.state,
            ProviderOptions(token=TOKEN, port=0, bind="127.0.0.1"),
        )
        await self.provider.async_start()
        self.base = self.provider.url
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.provider.async_stop()
        await asyncio.sleep(0)

    async def _get(self, session: "aiohttp.ClientSession", path: str) -> dict:
        async with session.get(f"{self.base}{path}", headers=self.headers) as response:
            self.assertEqual(response.status, 200, await response.text())
            return await response.json()

    async def test_contract(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base}/oauth2/v3/token",
                headers=self.headers,
                json={"grant_type": "refresh_token", "refresh_token": None},
            ) as response:
                self.assertEqual(response.status, 200)
                token = await response.json()
            self.assertEqual(token["access_token"], TOKEN)
            self.assertIsInstance(token["expires_in"], int)

            products = await self._get(session, "/api/1/products")
            vehicle = products["response"][0]
            self.assertIsInstance(vehicle["id"], int)
            self.assertIsInstance(vehicle["vehicle_id"], int)
            self.assertEqual(vehicle["vin"], VIN)

            data = await self._get(
                session,
                f"/api/1/vehicles/{vehicle['id']}/vehicle_data"
                "?endpoints=charge_state;climate_state;drive_state;vehicle_config;vehicle_state",
            )
            body = data["response"]
            for block in (
                "charge_state",
                "climate_state",
                "drive_state",
                "vehicle_config",
                "vehicle_state",
            ):
                self.assertIn(block, body)
                self.assertIsNotNone(body[block], block)
            self.assertIsInstance(body["drive_state"]["timestamp"], int)
            self.assertEqual(body["charge_state"]["battery_level"], 64)
            # Tesla units: 412 km of range arrives as miles.
            self.assertAlmostEqual(
                body["charge_state"]["battery_range"], 412.0 / 1.609344, places=1
            )
            self.assertEqual(body["vehicle_state"]["car_version"], "5.9.5")

            async with session.ws_connect(
                f"{self.base}/streaming/", headers=self.headers
            ) as websocket:
                await websocket.send_json(
                    {
                        "msg_type": "data:subscribe_oauth",
                        "token": TOKEN,
                        "tag": str(vehicle["vehicle_id"]),
                        "value": "speed,odometer,soc,elevation,est_heading,est_lat,"
                        "est_lng,power,shift_state,range,est_range,heading",
                    }
                )
                message = await asyncio.wait_for(websocket.receive_json(), timeout=5)
                self.assertEqual(message["msg_type"], "data:update")
                fields = message["value"].split(",")
                self.assertEqual(len(fields), 13, message["value"])

    async def test_auth_is_required(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base}/api/1/products") as response:
                self.assertEqual(response.status, 401)
            async with session.get(f"{self.base}/healthz") as response:
                self.assertEqual(response.status, 200)
                health = await response.json()
            self.assertTrue(health.get("ok"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
