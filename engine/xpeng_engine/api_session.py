"""The authenticated XPENG side: tenant + tokens + one lock around the REST calls.

Everything here is blocking; the service calls it from a worker thread. Threads
rather than asyncio on purpose: the vendored client is synchronous
(urllib + cryptography), and the one operation that must be serialised - minting
an MQTT credential or refreshing a token - is naturally a lock.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

from . import xpeng_api
from .xpeng_errors import LoginExpiredError, is_token_expired_response

_LOGGER = logging.getLogger(__name__)

#: Refresh the access token this long before it lapses. XPENG's is ~2 h.
_REFRESH_MARGIN_SECONDS = 15 * 60


class ApiSession:
    """One XPENG account session for one car.

    The tenant file holds the key pool sealed against this install's device id;
    the token file holds the account's access/refresh pair. Both are read once
    and kept in memory; a refresh rewrites the token file atomically so a
    restart does not need a new login.
    """

    def __init__(self, cert_dir: str, tenant_file: str, token_file: str, vin: str) -> None:
        self.vin = vin
        self.cert_dir = cert_dir
        self.tenant_file = tenant_file
        self.token_file = token_file
        self._lock = threading.RLock()
        self._tenant = xpeng_api.load_tenant_cache(tenant_file)
        self._tokens = xpeng_api.load_login_tokens(token_file)
        self._tokens_minted_at = time.time()
        try:
            self._tokens_minted_at = os.path.getmtime(token_file)
        except OSError:
            pass
        self._device_id = self._resolve_device_id()
        xpeng_api.set_cert_dir(cert_dir)
        xpeng_api.set_configured_device_id(self._device_id)
        self.vehicle_type_code = ""
        self.vehicle: dict[str, Any] = {}

    # -- identity ---------------------------------------------

    @property
    def device_id(self) -> str:
        """This install's 32-hex device identity (signs REST, names MQTT topics)."""
        return self._device_id

    def _resolve_device_id(self) -> str:
        device_id = str(self._tenant.get("deviceId") or "").strip()
        if device_id:
            return device_id
        device_id = xpeng_api.mint_device_id()
        self._tenant["deviceId"] = device_id
        self._save_tenant()
        _LOGGER.info("Minted a new device id for this install")
        return device_id

    def _save_tenant(self) -> None:
        xpeng_api.save_tenant_cache(self.tenant_file, self._tenant)

    # -- token lifecycle --------------------------------------

    def access_token_expires_at(self) -> float:
        try:
            expires_in = float(self._tokens.get("expiresIn") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0
        return self._tokens_minted_at + expires_in

    def ensure_tokens_fresh(self) -> None:
        """Refresh proactively when the access token is close to expiring."""
        if time.time() < self.access_token_expires_at() - _REFRESH_MARGIN_SECONDS:
            return
        try:
            self.refresh_tokens()
        except LoginExpiredError:
            raise
        except Exception:  # noqa: BLE001 - the next API call will retry
            _LOGGER.warning("Proactive token refresh failed", exc_info=True)

    def refresh_tokens(self) -> dict[str, Any]:
        with self._lock:
            refreshed = xpeng_api.refresh_login_tokens(None, self._tenant, self._tokens)
            if not refreshed or not refreshed.get("accessToken"):
                raise ConnectionError("token refresh returned no access token")
            self._tokens = refreshed
            self._tokens_minted_at = time.time()
            self._save_tokens()
            _LOGGER.info("XPENG access token refreshed")
            return self._tokens

    def _save_tokens(self) -> None:
        wrapper = {
            "json": self._tokens,
            "jsonText": json.dumps(self._tokens, separators=(",", ":")),
        }
        directory = os.path.dirname(self.token_file) or "."
        fd, tmp = tempfile.mkstemp(prefix=".tokens-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(wrapper, handle, indent=2, ensure_ascii=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.token_file)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- MQTT credentials -------------------------------------

    def mint_mqtt_credentials(self) -> dict[str, Any] | None:
        """Mint one short-lived broker credential.

        Called by the MQTT client from a connect path, on demand. A 12010101 in
        the response means the login token lapsed; refreshing and retrying once
        here is what keeps a long-running engine connected without a restart.
        """
        with self._lock:
            try:
                account = xpeng_api.create_mqtt_account(None, self._tenant, self._tokens)
            except LoginExpiredError:
                _LOGGER.info("Login token expired while minting MQTT credentials; refreshing")
                self.refresh_tokens()
                account = xpeng_api.create_mqtt_account(None, self._tenant, self._tokens)
            expires_ms = account.get("expireTime")
            expires_at = 0.0
            if expires_ms:
                try:
                    expires_at = float(expires_ms) / 1000.0
                except (TypeError, ValueError):
                    expires_at = 0.0
            if not expires_at:
                expires_at = time.time() + float(account.get("expireInSec") or 90)
            return {
                "client_id": account.get("clientId", ""),
                "username": account.get("username", ""),
                "password": account.get("password", ""),
                "server": account.get("serverSsl") or "iot-app-global.xiaopeng.com:8885",
                "expires_at": expires_at,
            }

    # -- REST calls -------------------------------------------

    def _call(self, function, *args) -> Any:
        """Run one authenticated REST call, recovering once from a lapsed login."""
        with self._lock:
            try:
                return function(None, self._tenant, self._tokens, *args)
            except LoginExpiredError:
                _LOGGER.info("Login token expired on %s; refreshing", function.__name__)
                self.refresh_tokens()
                return function(None, self._tenant, self._tokens, *args)

    def vehicles(self) -> list[dict[str, Any]]:
        data = self._call(xpeng_api.get_vehicles)
        if isinstance(data, list):
            return [v for v in data if isinstance(v, dict)]
        if isinstance(data, dict):
            for key in ("vehicles", "list"):
                if isinstance(data.get(key), list):
                    return [v for v in data[key] if isinstance(v, dict)]
        return []

    def refresh_vehicle_info(self) -> dict[str, Any]:
        """Fetch the vehicle list and keep this VIN's row."""
        for vehicle in self.vehicles():
            if vehicle.get("vin") == self.vin:
                self.vehicle = vehicle
                self.vehicle_type_code = str(vehicle.get("vehicleTypeCode") or "")
                return vehicle
        _LOGGER.warning("VIN %s is not in the account's vehicle list", self.vin)
        return {}

    def ota_status(self) -> dict[str, Any]:
        if not self.vehicle_type_code:
            self.refresh_vehicle_info()
        data = self._call(xpeng_api.get_ota_status, self.vin)
        return data if isinstance(data, dict) else {}

    def xos_version(self) -> str:
        data = self.ota_status()
        for key in ("osVersion", "version", "currentVersion", "xosVersion"):
            value = data.get(key)
            if value:
                return str(value)
        return ""

    def weather(self, latitude: float, longitude: float) -> dict[str, Any]:
        try:
            data = self._call(xpeng_api.get_weather, latitude, longitude)
        except Exception:  # noqa: BLE001 - weather is a supplement, never fatal
            _LOGGER.debug("weather lookup failed", exc_info=True)
            return {}
        return data if isinstance(data, dict) else {}

    def context(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """(tenant, tokens) for diagnostics; never log these."""
        return self._tenant, self._tokens


def is_expired(payload: Any) -> bool:
    return is_token_expired_response(payload)
