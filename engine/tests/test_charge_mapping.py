#!/usr/bin/env python3
"""Charge telemetry has to arrive in the shapes TeslaMate can insert.

TeslaMate's `charges` schema casts charger_actual_current, charger_phases,
charger_pilot_current, charger_power and charger_voltage to :integer, and Ecto's
cast_integer/1 rejects a float (:error -> "is invalid"). The XPENG integration
reports fractional values (6.9 kW, 233.6 V), so every sample of a live charge
failed to insert and the session finished with "Charging / Stopped / 0.00 kWh".
normalise_charge_state/1 rounds them; it runs on the relay's vehicle_data and in
the native mapping.

    python3 -m unittest engine.tests.test_charge_mapping -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xpeng_engine.teslamate_mapping import (
    CHARGE_INTEGER_FIELDS,
    Telemetry,
    build_charge_state,
    normalise_charge_state,
)

VIN = "L1NNSGHA3SB800783"

#: How the integration reported a live AC charge on 2026-10-24.
CHARGING_RAW = {
    "instant": {
        "charge": {
            "battery_soc": 58,
            "charging_state": 2,
            "charge_gun_status": 1,
            "max_range_charge": 90,
            "time_to_complete_charge_min": 120,
            "electric_current_limit": 32,
        },
        "odometer": {"avalible_driving_distance": 322},
    },
    "interval": {
        "charge": {"power": 6.9, "voltage": 233.6, "electricity": 9.4},
    },
}


class NormaliseChargeStateTest(unittest.TestCase):
    def test_rounds_the_integer_fields(self) -> None:
        charge = {
            "charger_actual_current": 9.4,
            "charger_phases": 1.0,
            "charger_pilot_current": 32.0,
            "charger_power": 6.9,
            "charger_voltage": 233.6,
        }
        self.assertTrue(normalise_charge_state(charge))
        expected = {
            "charger_actual_current": 9,
            "charger_phases": 1,
            "charger_pilot_current": 32,
            "charger_power": 7,
            "charger_voltage": 234,
        }
        self.assertEqual(charge, expected)
        for field, value in expected.items():
            self.assertIsInstance(charge[field], int, field)
            self.assertNotIsInstance(charge[field], bool, field)

    def test_whole_floats_are_rewritten_as_ints(self) -> None:
        # 0.0 == 0 numerically, so an equality check would miss these; Ecto does
        # not, which is how "charger_power 0.0 is invalid" rejected samples at
        # the start and end of every charge.
        charge = {"charger_power": 0.0, "charger_voltage": 7.0, "charger_phases": 1.0}
        self.assertTrue(normalise_charge_state(charge))
        for field in ("charger_power", "charger_voltage", "charger_phases"):
            self.assertIsInstance(charge[field], int, field)

    def test_integers_are_left_alone(self) -> None:
        charge = {field: 1 for field in CHARGE_INTEGER_FIELDS}
        self.assertFalse(normalise_charge_state(charge))

    def test_decimal_and_unrelated_fields_are_preserved(self) -> None:
        charge = {
            "charger_power": 6.9,
            "charge_energy_added": 12.53,
            "battery_range": 196.3,
            "charging_state": "Charging",
            "timestamp": 1792843040658,
        }
        normalise_charge_state(charge)
        self.assertEqual(charge["charge_energy_added"], 12.53)
        self.assertEqual(charge["battery_range"], 196.3)
        self.assertEqual(charge["charging_state"], "Charging")
        self.assertEqual(charge["timestamp"], 1792843040658)

    def test_unparseable_values_are_dropped(self) -> None:
        charge = {
            "charger_power": "unknown",
            "charger_voltage": None,
            "charger_actual_current": True,
        }
        self.assertTrue(normalise_charge_state(charge))
        self.assertNotIn("charger_power", charge)
        # nil is a value TeslaMate accepts, so it stays; a boolean is not.
        self.assertIsNone(charge["charger_voltage"])
        self.assertNotIn("charger_actual_current", charge)

    def test_zero_or_negative_phases_are_dropped(self) -> None:
        # validate_number(:charger_phases, greater_than: 0) would reject the
        # whole sample otherwise.
        for value in (0, 0.0, -1, "0"):
            with self.subTest(value=value):
                charge = {"charger_phases": value}
                self.assertTrue(normalise_charge_state(charge))
                self.assertNotIn("charger_phases", charge)

    def test_string_numbers_are_cast(self) -> None:
        charge = {"charger_power": "6.9", "charger_voltage": " 233 "}
        normalise_charge_state(charge)
        self.assertEqual(charge["charger_power"], 7)
        self.assertEqual(charge["charger_voltage"], 233)

    def test_empty_charge_state_is_unchanged(self) -> None:
        self.assertFalse(normalise_charge_state({}))


class BuildChargeStateTest(unittest.TestCase):
    def test_native_mapping_emits_integer_charge_fields(self) -> None:
        snapshot = Telemetry(vin=VIN, raw=CHARGING_RAW)
        charge = build_charge_state(snapshot)
        for field in CHARGE_INTEGER_FIELDS:
            value = charge.get(field)
            if value is not None:
                self.assertIsInstance(value, int, f"{field} must be an int")
        self.assertEqual(charge["charger_power"], 7)
        self.assertEqual(charge["charger_voltage"], 234)
        self.assertEqual(charge["charger_actual_current"], 9)
        self.assertEqual(charge["battery_level"], 58)
        # The values TeslaMate stores as :decimal keep their precision.
        self.assertNotIsInstance(charge["charge_energy_added"], int)

    def test_parked_zero_power_is_an_int(self) -> None:
        raw = {
            "instant": {"charge": {"battery_soc": 58, "charging_state": 0}},
            "interval": {"charge": {"power": 0}},
        }
        charge = build_charge_state(Telemetry(vin=VIN, raw=raw))
        self.assertIsInstance(charge["charger_power"], int)
        self.assertEqual(charge["charger_power"], 0)


if __name__ == "__main__":
    unittest.main()
