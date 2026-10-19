"""The engine's settings API: read/modify the data source and its credentials.

The XpengMate web UI is the only intended caller. It listens on its own port on
the compose network (never exposed publicly), requires the provider token, and
never returns a secret - only whether one is configured and enough identity
(account, VIN, device-id tail) to recognise it.

Applying a change restarts the engine process; compose's `restart: always`
brings it back on the new configuration. A few seconds of the provider being
unavailable is preferable to every settings write needing a rewrite of a running
MQTT session in place.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None
    web = None

from . import runtime

_LOGGER = logging.getLogger(__name__)


class AdminServer:
    def __init__(
        self,
        *,
        config: Any,
        status_provider: Callable[[], dict[str, Any]] | None = None,
        restart: Callable[[], None] | None = None,
        port: int | None = None,
        bind: str | None = None,
        token: str | None = None,
    ) -> None:
        self.config = config
        self._status_provider = status_provider or (lambda: {})
        self._restart = restart or self._default_restart
        self._port = port if port is not None else config.admin_port
        self._bind = bind if bind is not None else config.admin_bind
        self._token = token if token is not None else config.admin_token
        self._runner: Any = None
        self._site: Any = None
        self._session: "aiohttp.ClientSession | None" = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _default_restart() -> None:
        asyncio.get_running_loop().call_later(0.6, os._exit, 0)

    # -- lifecycle --------------------------------------------

    async def async_start(self) -> None:
        if web is None:
            raise RuntimeError("aiohttp is not available")
        self._loop = asyncio.get_running_loop()
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        )
        app = web.Application(middlewares=[self._middleware])
        app.router.add_get("/status", self._handle_status)
        app.router.add_get("/config", self._handle_get_config)
        app.router.add_put("/config", self._handle_put_config)
        app.router.add_post("/credentials", self._handle_credentials)
        app.router.add_post("/test", self._handle_test)
        app.router.add_post("/restart", self._handle_restart)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._bind, self._port)
        await self._site.start()
        _LOGGER.info("Settings API listening on %s:%s", self._bind, self._port)

    async def async_stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    @web.middleware
    async def _middleware(self, request: "web.Request", handler: Callable) -> Any:
        if self._token:
            header = request.headers.get("Authorization", "")
            scheme, _, value = header.partition(" ")
            if not (scheme.lower() == "bearer" and value.strip() == self._token):
                return web.json_response(
                    {"error": "invalid_token", "error_description": "admin token required"},
                    status=401,
                )
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("admin request failed", exc_info=True)
            return web.json_response({"error": str(exc)}, status=500)

    # -- handlers ---------------------------------------------

    async def _handle_status(self, request: "web.Request") -> "web.Response":
        payload: dict[str, Any] = {
            "ok": True,
            "source": self.config.source,
            "provider_port": self.config.provider_port,
            "admin_port": self._port,
            "credentials": runtime.credentials_summary(self.config),
            "config": runtime.sanitized(self.config),
        }
        payload.update(self._status_provider() or {})
        if self.config.source == "provider" and self.config.upstream_url:
            payload["upstream"] = await self._probe_upstream(
                self.config.upstream_url, self.config.upstream_token
            )
        return web.json_response(payload)

    async def _handle_get_config(self, request: "web.Request") -> "web.Response":
        return web.json_response(runtime.sanitized(self.config))

    async def _handle_put_config(self, request: "web.Request") -> "web.Response":
        try:
            update = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(update, dict):
            return web.json_response({"error": "body must be an object"}, status=400)
        allowed = set(runtime.EDITABLE.values())
        unknown = set(update) - allowed
        if unknown:
            return web.json_response(
                {"error": f"unknown settings: {', '.join(sorted(unknown))}"}, status=400
            )
        if update.get("source") and update["source"] not in ("xpeng", "provider", "ha"):
            return web.json_response({"error": "invalid source"}, status=400)
        problem = self._validate_effective(update)
        if problem:
            return web.json_response({"error": problem}, status=400)
        runtime.write_runtime(self.config.runtime_file, update)
        _LOGGER.info("settings updated: %s", sorted(update))
        asyncio.get_running_loop().call_later(0.4, self._restart)
        return web.json_response({"ok": True, "restarting": True})

    async def _handle_credentials(self, request: "web.Request") -> "web.Response":
        try:
            payload = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        if not isinstance(payload, dict):
            return web.json_response({"error": "body must be an object"}, status=400)
        try:
            written = self._install_credentials(payload)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        runtime.write_runtime(
            self.config.runtime_file,
            {
                "source": "xpeng",
                "vin": written.get("vin") or self.config.vin,
            },
        )
        _LOGGER.info("XPENG credentials installed: %s", ", ".join(written["installed"]))
        asyncio.get_running_loop().call_later(0.4, self._restart)
        return web.json_response(
            {
                "ok": True,
                "installed": written["installed"],
                "vin": written.get("vin"),
                "restarting": True,
            }
        )

    async def _handle_test(self, request: "web.Request") -> "web.Response":
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        source = str(payload.get("source") or self.config.source)
        if source == "provider":
            url = str(payload.get("provider_url") or self.config.upstream_url)
            token = str(payload.get("provider_token") or self.config.upstream_token)
            if not url or not token:
                return web.json_response(
                    {"ok": False, "error": "provider URL and token are required"},
                    status=400,
                )
            probe = await self._probe_provider(url, token)
            return web.json_response(probe)
        if source == "ha":
            url = str(payload.get("ha_url") or self.config.ha_url)
            token = str(payload.get("ha_token") or self.config.ha_token)
            prefix = str(payload.get("ha_entity_prefix") or self.config.ha_entity_prefix)
            if not url or not token:
                return web.json_response(
                    {"ok": False, "error": "HA URL and token are required"}, status=400
                )
            probe = await self._probe_ha(url, token, prefix)
            return web.json_response(probe)
        summary = runtime.credentials_summary(self.config)
        ok = all(item.get("present") for item in summary["certs"].values()) and summary[
            "tenant"
        ].get("present") and summary["tokens"].get("present")
        return web.json_response({"ok": bool(ok), "source": "xpeng", "credentials": summary})

    async def _handle_restart(self, request: "web.Request") -> "web.Response":
        asyncio.get_running_loop().call_later(0.4, self._restart)
        return web.json_response({"ok": True, "restarting": True})

    def _validate_effective(self, update: dict[str, Any]) -> str | None:
        """Reject a change the engine could not start with, before writing it.

        The form sends only what changed, so validation merges the update over
        the runtime file and the environment defaults - the settings that will
        actually be in force after the restart.
        """
        stored = runtime.read_runtime(self.config.runtime_file)
        effective = {**stored, **{k: v for k, v in update.items() if v not in (None, "")}}
        source = str(effective.get("source") or self.config.source)

        def value(key: str, fallback: str = "") -> str:
            return str(effective.get(key) or fallback or "").strip()

        if source == "provider":
            missing = []
            if not value("provider_url", self.config.upstream_url):
                missing.append("provider URL")
            if not value("provider_token", self.config.upstream_token):
                missing.append("provider token")
            if missing:
                return (
                    "The provider source needs a " + " and a ".join(missing) +
                    ". Enter them and save again."
                )
        elif source == "ha":
            missing = []
            if not value("ha_url", self.config.ha_url):
                missing.append("Home Assistant URL")
            if not value("ha_token", self.config.ha_token):
                missing.append("Home Assistant token")
            if not value("ha_entity_prefix", self.config.ha_entity_prefix):
                missing.append("entity prefix")
            if missing:
                return (
                    "The Home Assistant entity source needs a " + ", ".join(missing) +
                    ". Enter them and save again."
                )
        elif source == "xpeng":
            summary = runtime.credentials_summary(self.config)
            certs_ok = all(item.get("present") for item in summary["certs"].values())
            if not (certs_ok and summary["tenant"].get("present") and summary["tokens"].get("present")):
                return (
                    "The XPENG cloud source needs the certificates, tenant and token "
                    "files installed first."
                )
        return None

    # -- credentials ------------------------------------------

    def _install_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = ("ca_chain", "client_cert", "client_key", "tenant", "tokens")
        missing = [key for key in required if not payload.get(key)]
        if missing:
            raise ValueError(f"missing credential fields: {', '.join(missing)}")

        ca_chain = str(payload["ca_chain"])
        client_cert = str(payload["client_cert"])
        client_key = str(payload["client_key"])
        if "BEGIN CERTIFICATE" not in ca_chain:
            raise ValueError("ca_chain does not look like a PEM certificate")
        if "BEGIN CERTIFICATE" not in client_cert:
            raise ValueError("client_cert does not look like a PEM certificate")
        if "PRIVATE KEY" not in client_key:
            raise ValueError("client_key does not look like a PEM private key")

        tenant = _parse_json(payload["tenant"], "tenant")
        tokens = _parse_json(payload["tokens"], "tokens")
        if isinstance(tokens.get("json"), dict):
            tokens = tokens["json"]
        if not tenant.get("deviceId") or not tenant.get("keys"):
            raise ValueError("tenant JSON needs deviceId and keys")
        if not tokens.get("accessToken") or not tokens.get("refreshToken"):
            raise ValueError("tokens JSON needs accessToken and refreshToken")

        cert_dir = Path(self.config.cert_dir)
        cert_dir.mkdir(parents=True, exist_ok=True)
        for name, content in (
            ("ca-chain.crt", ca_chain),
            ("client-cert-0.pem", client_cert),
            ("client-key.pem", client_key),
        ):
            _write_secret(cert_dir / name, content)

        secrets_dir = Path(os.path.dirname(self.config.tenant_file) or ".")
        secrets_dir.mkdir(parents=True, exist_ok=True)
        _write_secret(
            Path(self.config.tenant_file),
            json.dumps(tenant, indent=2, sort_keys=True),
        )
        wrapper = {
            "json": tokens,
            "jsonText": json.dumps(tokens, separators=(",", ":")),
        }
        _write_secret(Path(self.config.token_file), json.dumps(wrapper, indent=2))

        vin = str(payload.get("vin") or tokens.get("vin") or "")
        return {"installed": ["ca_chain.crt", "tenant", "tokens"], "vin": vin}

    # -- probes -----------------------------------------------

    async def _probe_upstream(self, url: str, token: str) -> dict[str, Any]:
        return await self._probe_provider(url, token)

    async def _probe_provider(self, url: str, token: str) -> dict[str, Any]:
        url = url.rstrip("/")
        result: dict[str, Any] = {"url": url, "ok": False}
        try:
            assert self._session is not None
            async with self._session.get(f"{url}/healthz") as response:
                result["health_status"] = response.status
                try:
                    result["health"] = await response.json()
                except Exception:  # noqa: BLE001
                    result["health"] = (await response.text())[:200]
        except Exception as exc:  # noqa: BLE001
            result["health_error"] = str(exc)
            return result
        try:
            headers = {"Authorization": f"Bearer {token}"}
            async with self._session.get(f"{url}/api/1/products", headers=headers) as response:
                result["products_status"] = response.status
                if response.status == 200:
                    body = await response.json()
                    cars = body.get("response") or []
                    result["cars"] = [
                        {"id": car.get("id"), "vin": car.get("vin"), "state": car.get("state")}
                        for car in cars
                        if isinstance(car, dict)
                    ]
                    result["ok"] = bool(result["health"].get("ok")) and bool(cars)
                else:
                    result["body"] = (await response.text())[:300]
        except Exception as exc:  # noqa: BLE001
            result["products_error"] = str(exc)
        return result

    async def _probe_ha(self, url: str, token: str, prefix: str) -> dict[str, Any]:
        url = url.rstrip("/")
        result: dict[str, Any] = {"url": url, "ok": False, "prefix": prefix}
        try:
            assert self._session is not None
            headers = {"Authorization": f"Bearer {token}"}
            async with self._session.get(f"{url}/api/states", headers=headers) as response:
                result["states_status"] = response.status
                if response.status == 200:
                    states = await response.json()
                    matched = [
                        state
                        for state in states
                        if str(state.get("entity_id", "")).startswith(("sensor.", "binary_sensor."))
                        and prefix in str(state.get("entity_id", ""))
                    ]
                    result["entities"] = len(matched)
                    result["ok"] = bool(matched)
                else:
                    result["body"] = (await response.text())[:300]
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
        return result


def _parse_json(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except ValueError as exc:
        raise ValueError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} JSON must be an object")
    return parsed


def _write_secret(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
