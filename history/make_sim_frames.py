#!/usr/bin/env python3
"""Build a replay file for the engine's simulate mode from real XPENG history.

The engine's `XPENG_SIMULATE` mode replays a JSON list of telemetry frames
through the same state store and provider the live mode uses. Recording a real
drive and a real charge from EVConduit makes it possible to prove the whole
live path - provider API -> TeslaMate car logger -> Postgres - without
connecting to XPENG's cloud and without spending an account session.

    python3 history/make_sim_frames.py --out /data/sim/frames.json
    (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / XPENG_VIN from the environment)

The frames are real positions, real speeds, real SoC and real charge power; only
the timeline is compressed (the engine's XPENG_SIMULATE_SPEED does that) and the
range figure is derived from SoC, because the source rows carry no per-sample
range.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

from evconduit import Evconduit

_LOGGER = logging.getLogger("xpengmate.sim")

#: A G6's usable pack is 87.5 kWh; the rated range shown by the car is ~460 km.
RATED_RANGE_KM = 460.0
PARKED_SHIFT = 4
DRIVE_SHIFT = 1
CHARGING_STATE = 2
CONNECTED_STATE = 1
DISCONNECTED_STATE = 15


def epoch(value: str) -> float:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    return dt.datetime.fromisoformat(text).timestamp()


def interpolate(start: float, end: float, fraction: float) -> float:
    return start + (end - start) * max(0.0, min(1.0, fraction))


def tpms() -> dict:
    return {
        "tpms_pressure_fl": 268.0,
        "tpms_pressure_fr": 267.0,
        "tpms_pressure_rl": 270.0,
        "tpms_pressure_rr": 271.0,
    }


def frame(
    when: float,
    *,
    soc: float,
    odometer: float,
    shift: int,
    vehicle_mode: int,
    speed: float = 0.0,
    latitude: float,
    longitude: float,
    heading: float = 0.0,
    charging_state: int = DISCONNECTED_STATE,
    charge_power: float = 0.0,
    charge_voltage: float = 0.0,
    charge_current: float = 0.0,
) -> dict:
    return {
        "time": when,
        "raw": {
            "instant": {
                "charge": {
                    "battery_soc": int(round(soc)),
                    "charging_state": charging_state,
                    "max_range_charge": 90,
                    "charge_gun_status": 1 if charging_state in (CHARGING_STATE, CONNECTED_STATE) else 0,
                    "charge_connector_status": 7 if charging_state in (CHARGING_STATE, CONNECTED_STATE) else 0,
                    "slow_charge_port_status": 2 if charging_state in (CHARGING_STATE, CONNECTED_STATE) else 0,
                },
                "odometer": {
                    "total_mileage": round(odometer, 1),
                    "avalible_driving_distance": round(soc / 100.0 * RATED_RANGE_KM, 1),
                },
                "drive": {"shift_state": shift},
                "ext": {"vehicleMode": vehicle_mode, "soldier_camera_status": 0},
                "door": {"driver_door_lock_st": 2, "driver_door_ajar_st": 0},
                "tpms": tpms(),
                "hvac": {"hvac_inner_temp_float": 21.5, "hvac_temp_float": 22.0},
            },
            "interval": {
                "drive": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "speed": round(speed, 1),
                    "angle": heading,
                },
                "charge": {
                    "power": round(charge_power, 2),
                    "voltage": round(charge_voltage, 1),
                    "electricity": round(charge_current, 1),
                },
            },
        },
    }


def build(api: Evconduit, vin: str) -> list[dict]:
    # GPS history starts later than the export-derived trip history, so the
    # longest trip is not necessarily the one with a track. Pick the longest one
    # that actually has points.
    candidates = sorted(
        (t for t in api.trips(vin) if (t.get("distance_km") or 0) >= 10),
        key=lambda t: t.get("distance_km") or 0,
        reverse=True,
    )
    if not candidates:
        raise SystemExit("no trip long enough to replay")
    all_points = [p for p in api.positions(vin) if p.get("latitude") is not None]
    trip = None
    points: list[dict] = []
    for candidate in candidates:
        start = epoch(candidate["started_at"])
        end = epoch(candidate["ended_at"])
        window = [
            p
            for p in all_points
            if p.get("recorded_at") and start - 60 <= epoch(p["recorded_at"]) <= end + 60
        ]
        if len(window) >= 5:
            trip, points = candidate, window
            break
    if trip is None:
        raise SystemExit("no trip has GPS points to replay")
    start = epoch(trip["started_at"])
    end = epoch(trip["ended_at"])
    odometer_start = float(trip.get("odometer_start") or 50000)
    odometer_end = float(trip.get("odometer_end") or odometer_start + 10)
    soc_start = float(trip.get("soc_start") or 70)
    soc_end = float(trip.get("soc_end") or soc_start - 5)

    frames: list[dict] = []
    first = points[0]
    # Parked before the drive, so the logger sees the transition into driving.
    for offset in (-120, -60, -5):
        frames.append(
            frame(
                start + offset,
                soc=soc_start,
                odometer=odometer_start,
                shift=PARKED_SHIFT,
                vehicle_mode=0,
                latitude=float(first["latitude"]),
                longitude=float(first["longitude"]),
            )
        )

    for point in points:
        when = epoch(point["recorded_at"])
        fraction = (when - start) / max(1.0, end - start)
        frames.append(
            frame(
                when,
                soc=interpolate(soc_start, soc_end, fraction),
                odometer=interpolate(odometer_start, odometer_end, fraction),
                shift=DRIVE_SHIFT,
                vehicle_mode=2,
                speed=float(point.get("speed_kmh") or 0),
                latitude=float(point["latitude"]),
                longitude=float(point["longitude"]),
                heading=float(point.get("heading") or 0),
            )
        )

    last = points[-1]
    # Parked after the drive: this is what closes it.
    for offset in (30, 90, 180):
        frames.append(
            frame(
                end + offset,
                soc=soc_end,
                odometer=odometer_end,
                shift=PARKED_SHIFT,
                vehicle_mode=0,
                latitude=float(last["latitude"]),
                longitude=float(last["longitude"]),
            )
        )

    sessions = [s for s in api.charging_sessions(vin) if isinstance(s.get("curve"), dict)]
    if sessions:
        session = max(sessions, key=lambda s: len(s["curve"].get("t") or []))
        charge_start = epoch(session["started_at"])
        curve = session["curve"]
        seconds = curve.get("t") or []
        soc = curve.get("soc") or []
        power = curve.get("input_kw") or []
        volt = curve.get("volt") or []
        amp = curve.get("amp") or []
        for index, t_value in enumerate(seconds):
            when = charge_start + float(t_value)
            frames.append(
                frame(
                    when,
                    soc=float(soc[index]) if index < len(soc) else soc_end,
                    odometer=odometer_end,
                    shift=PARKED_SHIFT,
                    vehicle_mode=0,
                    charging_state=CHARGING_STATE,
                    charge_power=float(power[index]) if index < len(power) else 0.0,
                    charge_voltage=float(volt[index]) if index < len(volt) else 0.0,
                    charge_current=float(amp[index]) if index < len(amp) else 0.0,
                    latitude=float(last["latitude"]),
                    longitude=float(last["longitude"]),
                )
            )
        # Unplug: this closes the charging process.
        for offset in (60, 120, 180):
            frames.append(
                frame(
                    charge_start + float(seconds[-1]) + offset,
                    soc=float(soc[-1]) if soc else soc_end,
                    odometer=odometer_end,
                    shift=PARKED_SHIFT,
                    vehicle_mode=0,
                    charging_state=DISCONNECTED_STATE,
                    latitude=float(last["latitude"]),
                    longitude=float(last["longitude"]),
                )
            )

    frames.sort(key=lambda item: item["time"])
    unique: list[dict] = []
    for item in frames:
        if unique and item["time"] <= unique[-1]["time"]:
            continue
        unique.append(item)
    _LOGGER.info(
        "built %d frames: trip %.1f km, %d charge samples",
        len(unique),
        float(trip.get("distance_km") or 0),
        len(seconds) if sessions else 0,
    )
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--vin", default=os.environ.get("XPENG_VIN", ""))
    parser.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL", ""))
    parser.add_argument(
        "--service-key", default=os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")
    if not args.vin:
        print("XPENG_VIN is required", file=sys.stderr)
        return 2
    api = Evconduit(args.supabase_url, args.service_key)
    frames = build(api, args.vin)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump({"frames": frames}, handle)
    size = os.path.getsize(args.out)
    print(f"wrote {args.out} ({size} bytes, {len(frames)} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
