"""The history service: configuration, a sync button, and data freshness.

The importer used to be a one-shot container you ran by hand. The XpengMate
settings page needs to configure the EVConduit source, trigger a sync, and see
how old the data is, so the importer now sits behind a small local HTTP API. It
runs only on the compose network, behind a bearer token, and never exposes the
EVConduit credentials it stores (mode 0600).

Two sources, deliberately different in what they can reach:

* ``supabase`` - EVConduit's own database through PostgREST with a service-role
  key. Complete history, including the GPS logger's positions.
* ``api`` - EVConduit's public user API with an xpengcardata API key. No
  pagination exists on that API, so it sees the newest ~200 trips and charges
  and their per-trip GPS tracks - enough for a recent window, not a backfill.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None
    web = None

_LOGGER = logging.getLogger("xpengmate.history.server")

CONFIG_PATH = os.environ.get("HISTORY_CONFIG", "/data/config/history.json")
LAST_RUN_PATH = os.environ.get("HISTORY_LAST_RUN", "/data/last_run.json")
LOG_PATH = os.environ.get("HISTORY_LOG", "/data/sync.log")
EXPORT_LOG = os.environ.get("HISTORY_EXPORT_LOG", "/data/exports.log")
EXPORT_LAST_RUN = os.environ.get("HISTORY_EXPORT_LAST_RUN", "/data/export_last_run.json")
EXPORT_UPLOAD_DIR = os.environ.get("HISTORY_EXPORT_DIR", "/data/exports/uploaded")
DATABASE_URL = os.environ.get("XPENGMATE_DATABASE_URL", "")
TOKEN = os.environ.get("XPENGMATE_HISTORY_TOKEN", "")

_TRUE = {"1", "true", "yes", "on"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def config_public(config: dict[str, Any]) -> dict[str, Any]:
    """Config as the UI may see it: secrets reduced to a boolean."""
    public = {
        key: value
        for key, value in config.items()
        if key not in ("service_key", "api_key")
    }
    public["service_key_set"] = bool(config.get("service_key"))
    public["api_key_set"] = bool(config.get("api_key"))
    public["configured"] = bool(
        (config.get("mode") == "supabase" and config.get("supabase_url") and config.get("service_key"))
        or (config.get("mode") == "api" and config.get("api_url") and config.get("api_key"))
    )
    return public


# -- freshness -------------------------------------------------


def database_freshness(vin: str) -> dict[str, Any]:
    """Newest and oldest row per table, and the row counts, for one car."""
    if not DATABASE_URL:
        return {"error": "database URL is not configured"}
    result: dict[str, Any] = {"tables": {}, "counts": {}}
    queries = {
        "drives": "select count(*), min(start_date), max(start_date) from drives d join cars c on c.id = d.car_id where c.vin = %s",
        "charges": "select count(*), min(start_date), max(start_date) from charging_processes cp join cars c on c.id = cp.car_id where c.vin = %s",
        "positions": "select count(*), min(date), max(date) from positions p join cars c on c.id = p.car_id where c.vin = %s",
        "states": "select count(*), min(start_date), max(start_date) from states s join cars c on c.id = s.car_id where c.vin = %s",
        "charge_samples": "select count(*), min(c2.date), max(c2.date) from charges c2 join charging_processes cp on cp.id = c2.charging_process_id join cars c on c.id = cp.car_id where c.vin = %s",
    }
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            with connection.cursor() as cursor:
                for table, sql in queries.items():
                    cursor.execute(sql, (vin,))
                    count, oldest, newest = cursor.fetchone()
                    result["counts"][table] = int(count or 0)
                    result["tables"][table] = {
                        "oldest": oldest.isoformat() if oldest else None,
                        "newest": newest.isoformat() if newest else None,
                        "age_hours": round(
                            (datetime.now(timezone.utc).replace(tzinfo=None) - newest).total_seconds()
                            / 3600,
                            1,
                        )
                        if newest
                        else None,
                    }
                cursor.execute(
                    "select count(*) from xpengmate_import"
                )
                result["tracked_rows"] = int(cursor.fetchone()[0] or 0)
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
    return result


async def source_freshness(session: "aiohttp.ClientSession", config: dict[str, Any], vin: str) -> dict[str, Any]:
    """How current the source itself is, independent of what has been imported."""
    try:
        if config.get("mode") == "supabase" and config.get("supabase_url"):
            url = config["supabase_url"].rstrip("/") + "/rest/v1/xpeng_trips"
            headers = {
                "apikey": config.get("service_key", ""),
                "Authorization": f"Bearer {config.get('service_key', '')}",
            }
            params = {
                "select": "started_at",
                "vin": f"eq.{vin}",
                "order": "started_at.desc",
                "limit": "1",
            }
            async with session.get(url, headers=headers, params=params, timeout=10) as response:
                rows = await response.json() if response.status == 200 else []
            newest = rows[0].get("started_at") if rows else None
            return {"mode": "supabase", "newest_event": newest}
        if config.get("mode") == "api" and config.get("api_url"):
            # The data API pages oldest-first; one 1000-row page covers this
            # account (354 trips), so the last row is the newest event.
            base = config["api_url"].rstrip("/")
            if not base.endswith("/api/v1/xpeng"):
                base = base + "/api/v1/xpeng"
            url = base + "/trips"
            headers = {
                "Authorization": f"Bearer {config.get('api_key', '')}",
                "Accept": "application/json",
                "User-Agent": "XpengMate/4.4 (+https://github.com/xpengtoolkit/xpengmate)",
            }
            params = {"limit": "1000", "vin": vin}
            async with session.get(
                url, headers=headers, params=params, timeout=20
            ) as response:
                payload = await response.json() if response.status == 200 else {}
            trips = payload.get("data") or []
            newest = trips[-1].get("started_at") if trips else None
            return {"mode": "api", "newest_event": newest, "visible_trips": len(trips)}
        return {"mode": config.get("mode") or "unconfigured"}
    except Exception as exc:  # noqa: BLE001
        return {"mode": config.get("mode") or "unconfigured", "error": str(exc)}


def _lag_days(newest: str | None) -> float | None:
    if not newest:
        return None
    try:
        text = newest[:-1] + "+00:00" if newest.endswith("Z") else newest
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 86400, 2)


class HistoryService:
    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[Any] | None = None
        self._export_task: asyncio.Task[Any] | None = None
        self._config = read_json(CONFIG_PATH)
        self._session: "aiohttp.ClientSession | None" = None

    @property
    def configured(self) -> bool:
        return config_public(self._config)["configured"]

    # -- lifecycle --------------------------------------------

    async def start(self) -> None:
        self._session = aiohttp.ClientSession()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self._export_task is not None:
            self._export_task.cancel()
        if self._session is not None:
            await self._session.close()

    # -- handlers ---------------------------------------------

    async def handle_status(self, request: "web.Request") -> "web.Response":
        vin = str(self._config.get("vin") or "")
        last_run = read_json(LAST_RUN_PATH)
        payload: dict[str, Any] = {
            "ok": True,
            "configured": self.configured,
            "config": config_public(self._config),
            "running": self._task is not None and not self._task.done(),
            "last_run": last_run,
        }
        payload["database"] = await asyncio.to_thread(database_freshness, vin)
        if self._session is not None and self.configured:
            payload["source"] = await source_freshness(self._session, self._config, vin)
            newest_db = (
                payload["database"].get("tables", {}).get("drives", {}).get("newest")
            )
            payload["lag_days"] = {
                "database_vs_now": _lag_days(newest_db),
                "source_vs_now": _lag_days((payload.get("source") or {}).get("newest_event")),
            }
        if last_run.get("started_at"):
            payload["last_run"]["started_at"] = last_run["started_at"]
        return web.json_response(payload)

    async def handle_get_config(self, request: "web.Request") -> "web.Response":
        return web.json_response(config_public(self._config))

    async def handle_put_config(self, request: "web.Request") -> "web.Response":
        try:
            update = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(update, dict):
            return web.json_response({"error": "body must be an object"}, status=400)
        mode = update.get("mode")
        if mode not in (None, "supabase", "api"):
            return web.json_response({"error": "mode must be supabase or api"}, status=400)
        allowed = {
            "mode",
            "supabase_url",
            "service_key",
            "api_url",
            "api_key",
            "vin",
            "export_zip",
            "usable_battery_kwh",
            "car_name",
        }
        unknown = set(update) - allowed
        if unknown:
            return web.json_response(
                {"error": f"unknown settings: {', '.join(sorted(unknown))}"}, status=400
            )
        for key, value in update.items():
            if value in (None, ""):
                continue
            self._config[key] = value
        write_json(CONFIG_PATH, self._config)
        _LOGGER.info("history configuration updated: %s", sorted(update))
        return web.json_response({"ok": True, "config": config_public(self._config)})

    async def handle_sync(self, request: "web.Request") -> "web.Response":
        if not self.configured:
            return web.json_response(
                {"error": "history source is not configured"}, status=409
            )
        if self._task is not None and not self._task.done():
            return web.json_response({"error": "a sync is already running"}, status=409)
        self._task = asyncio.create_task(self._run_import())
        return web.json_response({"ok": True, "started": True})

    # -- export zips ------------------------------------------

    async def handle_exports_status(self, request: "web.Request") -> "web.Response":
        imports: list[dict[str, Any]] = []
        error = None
        if DATABASE_URL:
            try:
                imports = await asyncio.to_thread(self._list_imports)
            except Exception as exc:  # noqa: BLE001
                error = str(exc)
        payload: dict[str, Any] = {
            "ok": True,
            "running": self._export_task is not None and not self._export_task.done(),
            "last_run": read_json(EXPORT_LAST_RUN),
            "imports": imports,
            "upload_dir": EXPORT_UPLOAD_DIR,
        }
        if error:
            payload["error"] = error
        return web.json_response(payload)

    def _list_imports(self) -> list[dict[str, Any]]:
        with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT filename, vin, timer_start, timer_end, samples, drives,
                           charges, tyre_hours, imported_at
                    FROM xpengmate_export_imports
                    ORDER BY imported_at DESC
                    LIMIT 100
                    """
                )
                rows = cursor.fetchall()
        return [
            {
                "filename": row[0],
                "vin": row[1],
                "timer_start": row[2],
                "timer_end": row[3],
                "samples": row[4],
                "drives": row[5],
                "charges": row[6],
                "tyre_hours": row[7],
                "imported_at": row[8].isoformat() if row[8] else None,
            }
            for row in rows
        ]

    async def handle_export_upload(self, request: "web.Request") -> "web.Response":
        if not self._config.get("vin"):
            return web.json_response(
                {"error": "set the VIN in the history source first"}, status=409
            )
        if self._export_task is not None and not self._export_task.done():
            return web.json_response({"error": "an import is already running"}, status=409)
        os.makedirs(EXPORT_UPLOAD_DIR, exist_ok=True)
        saved: list[str] = []
        try:
            reader = await request.multipart()
            while True:
                part = await reader.next()
                if part is None:
                    break
                if not part.filename:
                    continue
                filename = os.path.basename(part.filename)
                if not filename.lower().endswith(".zip"):
                    return web.json_response(
                        {"error": f"{filename} is not a .zip export"}, status=400
                    )
                target = os.path.join(EXPORT_UPLOAD_DIR, filename)
                size = 0
                with open(target, "wb") as handle:
                    while True:
                        chunk = await part.read_chunk(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        handle.write(chunk)
                _LOGGER.info("stored export %s (%d bytes)", filename, size)
                saved.append(target)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("export upload failed", exc_info=True)
            return web.json_response({"error": str(exc)}, status=400)
        if not saved:
            return web.json_response({"error": "no files in the upload"}, status=400)

        self._export_task = asyncio.create_task(self._run_export_import(saved))
        return web.json_response(
            {"ok": True, "started": True, "files": [os.path.basename(p) for p in saved]}
        )

    async def _run_export_import(self, paths: list[str]) -> None:
        started = _now()
        write_json(EXPORT_LAST_RUN, {"started_at": started, "ok": None, "files": paths})
        environment = dict(os.environ)
        environment.update(
            {
                "XPENGMATE_DATABASE_URL": DATABASE_URL,
                "XPENG_VIN": str(self._config.get("vin") or ""),
                "XPENG_USABLE_BATTERY_KWH": str(
                    self._config.get("usable_battery_kwh") or "87.5"
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        log = open(EXPORT_LOG, "a", encoding="utf-8", buffering=1)
        log.write(f"\n=== export import started {started} ===\n")
        summary: dict[str, Any] = {}
        ok = False
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "export_import.py",
                *paths,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            stdout, _ = await process.communicate()
            text = stdout.decode("utf-8", errors="replace")
            log.write(text)
            ok = process.returncode == 0
            for line in text.splitlines():
                if line.startswith("EXPORT_IMPORT_SUMMARY "):
                    try:
                        summary = json.loads(line[len("EXPORT_IMPORT_SUMMARY "):])
                    except ValueError:
                        summary = {}
        except Exception as exc:  # noqa: BLE001
            log.write(f"export import failed to start: {exc}\n")
        finally:
            log.close()
        write_json(
            EXPORT_LAST_RUN,
            {
                "started_at": started,
                "finished_at": _now(),
                "ok": ok,
                "files": [os.path.basename(p) for p in paths],
                "summary": summary,
                "log_tail": _tail(EXPORT_LOG, 6000),
            },
        )
        _LOGGER.info("export import finished ok=%s", ok)

    # -- the importer -----------------------------------------

    def _pending_export_zips(self) -> list[str]:
        """Export zips in the upload directory that were never imported."""
        if not DATABASE_URL or not os.path.isdir(EXPORT_UPLOAD_DIR):
            return []
        try:
            with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT filename FROM xpengmate_export_imports")
                    known = {row[0] for row in cursor.fetchall()}
        except Exception:  # noqa: BLE001 - missing table means nothing imported yet
            known = set()
        pending = []
        for name in sorted(os.listdir(EXPORT_UPLOAD_DIR)):
            if not name.lower().endswith(".zip") or name in known:
                continue
            pending.append(os.path.join(EXPORT_UPLOAD_DIR, name))
        return pending

    async def _run_export_import(
        self, paths: list[str], environment: dict[str, str], log: Any
    ) -> None:
        log.write(f"=== export import: {len(paths)} pending zip(s) ===\n")
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "export_import.py",
                *paths,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            code = await process.wait()
            log.write(f"=== export import finished ({code}) ===\n")
            if code != 0:
                _LOGGER.warning("export import after sync failed with %s", code)
        except Exception as exc:  # noqa: BLE001
            log.write(f"export import failed to start: {exc}\n")

    async def _run_import(self) -> None:
        mode = self._config.get("mode") or "supabase"
        environment = dict(os.environ)
        environment.update(
            {
                "XPENGMATE_DATABASE_URL": DATABASE_URL,
                "XPENG_VIN": str(self._config.get("vin") or ""),
                "XPENG_USABLE_BATTERY_KWH": str(
                    self._config.get("usable_battery_kwh") or "87.5"
                ),
                "XPENG_CAR_NAME": str(self._config.get("car_name") or "XPeng"),
                "XPENG_EXPORT_ZIP": str(self._config.get("export_zip") or ""),
                "PYTHONUNBUFFERED": "1",
            }
        )
        if mode == "supabase":
            environment.update(
                {
                    "SUPABASE_URL": str(self._config.get("supabase_url") or ""),
                    "SUPABASE_SERVICE_ROLE_KEY": str(self._config.get("service_key") or ""),
                }
            )
            command = [sys.executable, "import_history.py"]
        else:
            environment.update(
                {
                    "EVCONDUIT_API_URL": str(self._config.get("api_url") or ""),
                    "EVCONDUIT_API_KEY": str(self._config.get("api_key") or ""),
                }
            )
            command = [sys.executable, "api_import.py"]
        started = _now()
        write_json(LAST_RUN_PATH, {"started_at": started, "ok": None, "mode": mode})
        log = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
        log.write(f"\n=== sync started {started} ({mode}) ===\n")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=os.path.dirname(os.path.abspath(__file__)),
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            code = await process.wait()
            ok = code == 0
            if ok:
                # Any export zip dropped in the upload directory (including a
                # manual download) is imported here, so its state spans, tyres
                # and SoC/range enrichment are applied even when EVConduit
                # already covers the drives and charges.
                pending = await asyncio.to_thread(self._pending_export_zips)
                if pending:
                    await self._run_export_import(pending, environment, log)
                # Re-imported rows start without TeslaMate's derived columns
                # (charger phases, rated ranges, clamps), so the sync is only
                # complete once the backfill has run over the fresh data.
                log.write("=== backfill after sync ===\n")
                backfill = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "backfill_teslamate.py",
                    "--no-geocode",
                    cwd=os.path.dirname(os.path.abspath(__file__)),
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                backfill_code = await backfill.wait()
                log.write(f"=== backfill finished ({backfill_code}) ===\n")
                if backfill_code != 0:
                    _LOGGER.warning("backfill after sync failed with %s", backfill_code)
        except Exception as exc:  # noqa: BLE001
            log.write(f"sync failed to start: {exc}\n")
            ok = False
        finally:
            log.close()
        finished = _now()
        write_json(
            LAST_RUN_PATH,
            {
                "started_at": started,
                "finished_at": finished,
                "ok": ok,
                "mode": mode,
                "log_tail": _tail(LOG_PATH, 4000),
            },
        )
        _LOGGER.info("history sync finished ok=%s", ok)


def _tail(path: str, limit: int) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            data = handle.read()
    except OSError:
        return ""
    return data[-limit:]


async def async_main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    service = HistoryService()
    await service.start()
    app = web.Application(client_max_size=512 * 1024 * 1024)
    app.router.add_get("/status", service.handle_status)
    app.router.add_get("/exports", service.handle_exports_status)
    app.router.add_post("/exports", service.handle_export_upload)
    app.router.add_get("/config", service.handle_get_config)
    app.router.add_put("/config", service.handle_put_config)
    app.router.add_post("/sync", service.handle_sync)

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> Any:
        if TOKEN:
            header = request.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            if not (scheme.lower() == "bearer" and value.strip() == TOKEN):
                return web.json_response({"error": "invalid_token"}, status=401)
        return await handler(request)

    app.middlewares.append(auth)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    port = int(os.environ.get("HISTORY_PORT", "8100"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    _LOGGER.info("history service listening on %s", port)
    await asyncio.Event().wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
