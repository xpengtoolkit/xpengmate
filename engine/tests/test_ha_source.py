#!/usr/bin/env python3
"""The Home Assistant source, against a real captured entity snapshot.

The values below are a live snapshot from the production XPENG integration
(parked, unplugged, locked) so the test fails if the integration's entity
semantics ever change under us - the inversion of the lock sensor and the text
shift/charging states are the parts that are easy to get wrong.

    python3 -m unittest engine.tests.test_ha_source -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xpeng_engine.ha_source import HaSource, build_raw
from xpeng_engine.state import EngineState

VIN = "L1NNSGHA3SB800783"

#: Live entity states captured from the reference car on 2026-10-19.
PARKED = {
    "battery": "58",
    "range": "322",
    "odometer": "62142",
    "speed": "0.0",
    "shift_state": "P",
    "latitude": "-32.2241096496582",
    "longitude": "152.540939331055",
    "heading": "61.5",
    "cabin_temperature": "22",
    "set_temperature": "21.5",
    "charge_power": "0",
    "charge_voltage": "0",
    "charge_current": "0",
    "charging_state": "Disconnected",
    "max_charge": "90",
    "tyre_pressure_front_left": "275",
    "tyre_pressure_front_right": "275",
    "tyre_pressure_rear_left": "286",
    "tyre_pressure_rear_right": "286",
    "last_data_update": "2026-10-18T23:12:14+00:00",
    "xos_version": "XOS 5.9.5.8909",
    "charging": "off",
    "plugged_in": "off",
    "lock": "off",  # HA lock sensor: ON = unlocked, so off = locked
    "driver_door": "off",
    "passenger_door": "off",
    "rear_left_door": "off",
    "rear_right_door": "off",
    "bonnet": "off",
    "trunk": "off",
    "sentinel_mode": "off",
    "x_guard_camera": "off",
    "mqtt_connected": "on",
    "climate": "off",
}


class BuildRawTest(unittest.TestCase):
    def test_parked_snapshot(self) -> None:
        raw, when, connected = build_raw(PARKED)
        self.assertTrue(connected)
        self.assertIsNotNone(when)
        self.assertEqual(when.isoformat(), "2026-10-18T23:12:14+00:00")
        self.assertEqual(raw["instant"]["charge"]["battery_soc"], 58)
        self.assertEqual(raw["instant"]["charge"]["charging_state"], 0)
        self.assertEqual(raw["instant"]["charge"]["max_range_charge"], 90)
        self.assertEqual(raw["instant"]["drive"]["shift_state"], 3)  # P
        self.assertEqual(raw["instant"]["ext"]["vehicleMode"], 0)
        # 1 = locked in XPENG's encoding, and the HA sensor was off (locked).
        self.assertEqual(raw["instant"]["door"]["driver_door_lock_st"], 1)
        self.assertEqual(raw["instant"]["odometer"]["total_mileage"], 62142)
        self.assertEqual(raw["instant"]["tpms"]["tpms_pressure_fl"], 275)
        self.assertEqual(raw["interval"]["drive"]["latitude"], -32.2241096496582)
        self.assertEqual(raw["interval"]["drive"]["angle"], 61.5)

    def test_unlocked_and_driving(self) -> None:
        values = dict(PARKED)
        values.update(
            {
                "lock": "on",  # unlocked
                "speed": "48.5",
                "shift_state": "D",
                "driver_door": "on",
            }
        )
        raw, _, _ = build_raw(values)
        self.assertEqual(raw["instant"]["door"]["driver_door_lock_st"], 0)
        self.assertEqual(raw["instant"]["door"]["driver_door_ajar_st"], 1)
        self.assertEqual(raw["instant"]["drive"]["shift_state"], 1)  # D
        self.assertEqual(raw["instant"]["ext"]["vehicleMode"], 2)  # driving

    def test_charging(self) -> None:
        values = dict(PARKED)
        values.update(
            {
                "plugged_in": "on",
                "charging": "on",
                "charging_state": "Charging",
                "charge_power": "6.9",
                "charge_voltage": "553.0",
                "charge_current": "11.5",
            }
        )
        raw, _, _ = build_raw(values)
        charge = raw["instant"]["charge"]
        self.assertEqual(charge["charging_state"], 2)
        self.assertEqual(charge["charge_gun_status"], 1)
        self.assertEqual(charge["slow_charge_port_status"], 2)
        self.assertEqual(raw["interval"]["charge"]["power"], 6.9)

    def test_entity_prefix_filtering(self) -> None:
        source = HaSource("http://ha", "token", "xpeng_800783", lambda *a: None)
        self.assertEqual(
            source.apply_state("sensor.xpeng_800783_battery", "58"), True
        )
        self.assertEqual(
            source.apply_state("sensor.xpeng_g6_battery", "99"), False
        )
        self.assertEqual(
            source.apply_state("binary_sensor.xpeng_800783_charging", "off"), True
        )
        self.assertEqual(source.apply_state("sensor.other_thing", "1"), False)
        self.assertEqual(source.values["battery"], "58")


class StateTest(unittest.TestCase):
    def test_snapshot_round_trip(self) -> None:
        raw, when, _ = build_raw(PARKED)
        state = EngineState(VIN, usable_battery_kwh=87.5)
        state.update_poll_status(mqtt_connected=True)
        state.set_raw_snapshot(raw, when=when)
        state.set_xos_version("5.9.5.8909")
        snapshot = state.snapshot()
        self.assertEqual(snapshot.raw["instant"]["charge"]["battery_soc"], 58)
        self.assertEqual(snapshot.car_version, "5.9.5.8909")
        self.assertGreater(snapshot.data_age_seconds, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
