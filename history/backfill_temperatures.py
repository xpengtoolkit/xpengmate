"""Fill historical outside temperatures from Home Assistant statistics.

XPENG's exports carry no ambient temperature, so imported positions and the
drives/charges built from them have ``outside_temp`` empty and the Temperatures
dashboard has nothing to plot. Home Assistant keeps long-term statistics for the
outside-temperature sensor indefinitely (hourly mean/min/max), which is enough to
give every historical position the temperature of its hour.

Inside temperature has no historical source; it is only logged live, so it is
left NULL rather than guessed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import sys
from typing import Any

import aiohttp
import psycopg

_LOGGER = logging.getLogger("xpengmate.backfill.temperatures")

DEFAULT_ENTITY = "sensor.xpeng_800783_weather_at_vehicle"
MATCH_SECONDS = 5400  # half an hour either side of the hourly bucket


async def fetch_hourly_temperature(
    ha_url: str, ha_token: str, entity_id: str, start: dt.datetime, end: dt.datetime
) -> dict[int, float]:
    """Return {bucket_start_epoch: mean_celsius} from HA long-term statistics."""
    ws_url = ha_url.replace("https://", "wss://").replace("http://", "ws://")
    ws_url = ws_url.rstrip("/") + "/api/websocket"
    buckets: dict[int, float] = {}
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(ws_url, heartbeat=30, max_msg_size=0) as ws:
            await ws.receive()  # auth_required
            await ws.send_json({"type": "auth", "access_token": ha_token})
            auth = json.loads((await ws.receive()).data)
            if auth.get("type") != "auth_ok":
                raise RuntimeError(f"Home Assistant auth failed: {auth}")
            await ws.send_json(
                {
                    "id": 1,
                    "type": "recorder/statistics_during_period",
                    "start_time": start.isoformat(),
                    "end_time": end.isoformat(),
                    "statistic_ids": [entity_id],
                    "period": "hour",
                    "types": ["mean"],
                }
            )
            while True:
                frame = await ws.receive()
                if frame.type != aiohttp.WSMsgType.TEXT:
                    raise RuntimeError(f"Home Assistant websocket closed: {frame.type}")
                message = json.loads(frame.data)
                if message.get("id") != 1:
                    continue
                if not message.get("success"):
                    raise RuntimeError(f"statistics request failed: {message}")
                result = message.get("result") or {}
                for stat_id, rows in result.items():
                    for row in rows or []:
                        try:
                            start_ms = int(row["start"])
                            mean = float(row["mean"])
                        except (KeyError, TypeError, ValueError):
                            continue
                        buckets[start_ms // 1000] = round(mean, 1)
                break
    return buckets


def fill_positions(conn: psycopg.Connection, car_id: int, buckets: dict[int, float]) -> int:
    if not buckets:
        return 0
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS xpengmate_outside_temp")
        cur.execute(
            "CREATE TEMP TABLE xpengmate_outside_temp (bucket bigint PRIMARY KEY, celsius numeric)"
        )
        cur.executemany(
            "INSERT INTO xpengmate_outside_temp VALUES (%s,%s)",
            [(bucket, value) for bucket, value in buckets.items()],
        )
        cur.execute(
            """
            UPDATE positions p SET outside_temp = (
                SELECT t.celsius FROM xpengmate_outside_temp t
                WHERE abs(t.bucket - floor(extract(epoch FROM p.date))) <= %s
                ORDER BY abs(t.bucket - floor(extract(epoch FROM p.date)))
                LIMIT 1
            )
            WHERE p.car_id = %s
              AND p.outside_temp IS NULL
              AND EXISTS (
                SELECT 1 FROM xpengmate_outside_temp t
                WHERE abs(t.bucket - floor(extract(epoch FROM p.date))) <= %s
              )
            """,
            (MATCH_SECONDS, car_id, MATCH_SECONDS),
        )
        filled = cur.rowcount
    conn.commit()
    return filled


def update_averages(conn: psycopg.Connection, car_id: int) -> tuple[int, int]:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE drives d SET outside_temp_avg = avg_temp.avg
            FROM (
                SELECT p.drive_id AS id, round(avg(p.outside_temp), 1) AS avg
                FROM positions p
                WHERE p.car_id = %s AND p.drive_id IS NOT NULL AND p.outside_temp IS NOT NULL
                GROUP BY p.drive_id
            ) AS avg_temp
            WHERE d.id = avg_temp.id AND d.car_id = %s
            """,
            (car_id, car_id),
        )
        drives = cur.rowcount
        cur.execute(
            """
            UPDATE charging_processes c SET outside_temp_avg = avg_temp.avg
            FROM (
                SELECT cp.id,
                       round(avg(p.outside_temp), 1) AS avg
                FROM charging_processes cp
                JOIN positions p
                  ON p.car_id = cp.car_id
                 AND p.date BETWEEN cp.start_date AND COALESCE(cp.end_date, cp.start_date)
                WHERE cp.car_id = %s AND p.outside_temp IS NOT NULL
                GROUP BY cp.id
            ) AS avg_temp
            WHERE c.id = avg_temp.id AND c.car_id = %s
            """,
            (car_id, car_id),
        )
        charges = cur.rowcount
        # Drives/charges without a position inside them (pre-logger history)
        # still get an average from the hourly statistics over their window.
        cur.execute(
            """
            UPDATE drives d SET outside_temp_avg = (
                SELECT t.celsius FROM xpengmate_outside_temp t
                WHERE abs(t.bucket - floor(extract(epoch FROM (
                        d.start_date + (d.end_date - d.start_date) / 2)))) <= 3600
                ORDER BY abs(t.bucket - floor(extract(epoch FROM (
                        d.start_date + (d.end_date - d.start_date) / 2))))
                LIMIT 1
            )
            WHERE d.car_id = %s
              AND d.outside_temp_avg IS NULL
              AND EXISTS (
                SELECT 1 FROM xpengmate_outside_temp t
                WHERE abs(t.bucket - floor(extract(epoch FROM (
                        d.start_date + (d.end_date - d.start_date) / 2)))) <= 3600
              )
            """,
            (car_id,),
        )
        drives += cur.rowcount
        cur.execute(
            """
            UPDATE charging_processes c SET outside_temp_avg = (
                SELECT t.celsius FROM xpengmate_outside_temp t
                WHERE abs(t.bucket - floor(extract(epoch FROM (
                        c.start_date + (c.end_date - c.start_date) / 2)))) <= 3600
                ORDER BY abs(t.bucket - floor(extract(epoch FROM (
                        c.start_date + (c.end_date - c.start_date) / 2))))
                LIMIT 1
            )
            WHERE c.car_id = %s
              AND c.outside_temp_avg IS NULL
              AND EXISTS (
                SELECT 1 FROM xpengmate_outside_temp t
                WHERE abs(t.bucket - floor(extract(epoch FROM (
                        c.start_date + (c.end_date - c.start_date) / 2)))) <= 3600
              )
            """,
            (car_id,),
        )
        charges += cur.rowcount
    conn.commit()
    return drives, charges


def run() -> int:
    dsn = os.environ.get("XPENGMATE_DATABASE_URL", "")
    vin = os.environ.get("XPENG_VIN", "")
    ha_url = os.environ.get("XPENG_HA_URL", "")
    ha_token = os.environ.get("XPENG_HA_TOKEN", "")
    entity = os.environ.get("XPENG_OUTSIDE_TEMP_ENTITY", DEFAULT_ENTITY)
    if not dsn or not vin:
        print("XPENGMATE_DATABASE_URL and XPENG_VIN are required", file=sys.stderr)
        return 2
    if not ha_url or not ha_token:
        print("XPENG_HA_URL and XPENG_HA_TOKEN are required", file=sys.stderr)
        return 2

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM cars WHERE vin = %s", (vin,))
            row = cur.fetchone()
            if not row:
                print(f"car {vin} is not in the database", file=sys.stderr)
                return 1
            car_id = int(row[0])
            cur.execute(
                "SELECT min(date) FROM positions WHERE car_id = %s", (car_id,)
            )
            first = cur.fetchone()[0]
            cur.execute(
                "SELECT min(start_date) FROM drives WHERE car_id = %s", (car_id,)
            )
            drive_first = cur.fetchone()[0]
            cur.execute(
                "SELECT min(start_date) FROM charging_processes WHERE car_id = %s",
                (car_id,),
            )
            charge_first = cur.fetchone()[0]
            for candidate in (drive_first, charge_first):
                if candidate and (first is None or candidate < first):
                    first = candidate
        start = (first or dt.datetime.utcnow()) - dt.timedelta(days=1)
        start = start.replace(tzinfo=dt.timezone.utc)
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
        _LOGGER.info("fetching hourly statistics %s..%s", start.date(), end.date())
        buckets = asyncio.run(fetch_hourly_temperature(ha_url, ha_token, entity, start, end))
        _LOGGER.info("got %d hourly temperature buckets", len(buckets))
        filled = fill_positions(conn, car_id, buckets)
        drives, charges = update_averages(conn, car_id)
    _LOGGER.info(
        "filled outside_temp on %d positions; averages on %d drives and %d charges",
        filled,
        drives,
        charges,
    )
    print(json.dumps({"positions_filled": filled, "drives": drives, "charges": charges}))
    return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
