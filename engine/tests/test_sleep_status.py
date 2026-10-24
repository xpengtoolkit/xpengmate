#!/usr/bin/env python3
"""Sleep detection: polling status plus the telemetry that can only move awake.

The polling-status entity goes Silent/Parked for a whole drive when the
integration's live socket is quiet (XPENG allows one telemetry session, and the
driving car may hold it). The passive cloud polls still refresh the odometer and
battery, so those are the second arousal signal. These tests pin that down with a
fake Home Assistant session; no aiohttp needed.

    python3 -m unittest engine.tests.test_sleep_status -v
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xpeng_engine.sleep_status import SleepStatus, sibling_entity

HA_URL = "http://homeassistant.test:8123"
TOKEN = "test-token"
POLLING = "sensor.xpeng_800783_polling_status"
ODOMETER = "sensor.xpeng_800783_odometer"
BATTERY = "sensor.xpeng_800783_battery"


class _Response:
    def __init__(self, state: str, status: int = 200) -> None:
        self._state = state
        self.status = status

    async def json(self) -> dict[str, Any]:
        return {"state": self._state}

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _Session:
    """Stand-in for aiohttp.ClientSession serving one state per entity."""

    def __init__(self, states: dict[str, str]) -> None:
        self.states = states
        self.requested: list[str] = []

    def get(self, url: str, headers: Any = None) -> _Response:
        entity_id = url.rsplit("/", 1)[-1]
        self.requested.append(entity_id)
        return _Response(self.states.get(entity_id, "unavailable"))


def make_status(states: dict[str, str], **kwargs: Any) -> SleepStatus:
    """A SleepStatus with a fake Home Assistant and no TTL caching."""
    status = SleepStatus(HA_URL, TOKEN, POLLING, ttl=0.0, **kwargs)
    status._session = _Session(states)  # type: ignore[assignment]
    return status


class SiblingEntityTest(unittest.TestCase):
    def test_polling_status_yields_odometer_and_battery(self) -> None:
        self.assertEqual(sibling_entity(POLLING, "odometer"), ODOMETER)
        self.assertEqual(sibling_entity(POLLING, "battery"), BATTERY)

    def test_other_entity_has_no_sibling(self) -> None:
        self.assertEqual(sibling_entity("binary_sensor.car_silent", "battery"), "")

    def test_defaults_are_derived_from_the_polling_status(self) -> None:
        status = SleepStatus(HA_URL, TOKEN, POLLING)
        self.assertEqual(status.odometer_entity, ODOMETER)
        self.assertEqual(status.battery_entity, BATTERY)

    def test_explicit_entities_win(self) -> None:
        status = SleepStatus(
            HA_URL,
            TOKEN,
            POLLING,
            odometer_entity="sensor.custom_odo",
            battery_entity="sensor.custom_soc",
        )
        self.assertEqual(status.odometer_entity, "sensor.custom_odo")
        self.assertEqual(status.battery_entity, "sensor.custom_soc")


class SleepStatusTest(unittest.IsolatedAsyncioTestCase):
    async def test_start_is_unknown(self) -> None:
        self.assertIsNone(make_status({POLLING: "Parked"}).is_asleep)

    async def test_silent_without_telemetry_is_asleep(self) -> None:
        status = make_status(
            {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        )
        await status.async_get_status()
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)
        self.assertEqual(status.raw_status, "silent")

    async def test_odometer_step_wakes(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

        states[ODOMETER] = "62418"
        await status.async_get_status()
        self.assertIs(status.is_asleep, False)

    async def test_battery_step_wakes(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

        states[BATTERY] = "74"
        await status.async_get_status()
        self.assertIs(status.is_asleep, False)

    async def test_silent_does_not_cancel_a_recent_wake(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()
        states[ODOMETER] = "62430"
        await status.async_get_status()
        self.assertIs(status.is_asleep, False)
        # The next poll is still Silent with a stable odometer: stay awake.
        await status.async_get_status()
        self.assertIs(status.is_asleep, False)

    async def test_returns_to_asleep_after_the_hold(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()
        states[ODOMETER] = "62430"
        await status.async_get_status()
        self.assertIs(status.is_asleep, False)

        status._awake_until = time.monotonic() - 1
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

    async def test_active_wakes_without_telemetry(self) -> None:
        status = make_status({POLLING: "Active"})
        await status.async_get_status()
        self.assertIs(status.is_asleep, False)

    async def test_parked_keeps_the_previous_decision(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

        states[POLLING] = "Parked"
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

    async def test_unavailable_telemetry_does_not_wake(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

        states[ODOMETER] = "unavailable"
        states[BATTERY] = "unknown"
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

    async def test_odometer_reset_does_not_wake(self) -> None:
        states = {POLLING: "Silent", ODOMETER: "62413", BATTERY: "79"}
        status = make_status(states)
        await status.async_get_status()

        states[ODOMETER] = "10"
        await status.async_get_status()
        self.assertIs(status.is_asleep, True)

    async def test_telemetry_is_read_from_the_configured_ha(self) -> None:
        status = make_status({POLLING: "Silent"})
        await status.async_get_status()
        session = status._session
        assert session is not None
        self.assertIn(ODOMETER, session.requested)  # type: ignore[attr-defined]
        self.assertIn(BATTERY, session.requested)  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
