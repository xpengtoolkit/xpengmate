"""Engine configuration, entirely from environment variables.

The engine is a container: one car, one config, no UI. Everything that differs
between installs is an environment variable, and everything that has a sane
default does.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None else value.strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name).lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return default


class ConfigError(RuntimeError):
    """A configuration problem that must stop the engine rather than be guessed."""


@dataclass(frozen=True)
class Config:
    # -- what to log ------------------------------------------------
    vin: str = ""
    usable_battery_kwh: float = 87.5

    # -- where the secrets are --------------------------------------
    cert_dir: str = "/data/certs"
    tenant_file: str = "/data/secrets/xpeng_tenant.json"
    token_file: str = "/data/secrets/xpeng_tokens.json"
    state_dir: str = "/data/state"

    # -- XPENG cloud ------------------------------------------------
    mqtt_server: str = "iot-app-global.xiaopeng.com:8885"
    scan_interval: float = 60.0
    parked_scan_interval: float = 900.0
    rest_interval: float = 300.0
    ota_interval: float = 3600.0
    watchdog_interval: float = 15.0
    silence_timeout: float = 300.0
    token_refresh_margin: float = 900.0

    # -- the Tesla-compatible server TeslaMate/xpengmate talks to ----
    provider_bind: str = "0.0.0.0"
    provider_port: int = 8201
    provider_token: str = ""

    # -- boot policy ------------------------------------------------
    #: Serve the Tesla-compatible API even when the XPENG credentials are not
    #: installed yet. The car reports ``offline``; nothing is invented. This is
    #: how the stack is brought up before the mTLS material is mounted.
    allow_offline: bool = False

    # -- simulation (no XPENG connection; replays recorded telemetry) --
    simulate: bool = False
    simulate_file: str = ""
    simulate_speed: float = 60.0
    simulate_loop: bool = False

    # -- observability ----------------------------------------------
    status_file: str = "/data/state/status.json"
    log_level: str = "INFO"

    def validate(self) -> None:
        problems: list[str] = []
        if self.allow_offline:
            return
        if not self.simulate:
            if not self.vin:
                problems.append("XPENG_VIN is required in live mode")
            for label, path in (
                ("XPENG_CERT_DIR", self.cert_dir),
                ("XPENG_TENANT_FILE", self.tenant_file),
                ("XPENG_TOKEN_FILE", self.token_file),
            ):
                if not os.path.isfile(path) and not os.path.isdir(path):
                    problems.append(f"{label} does not exist: {path}")
            if len(self.provider_token) < 8:
                problems.append(
                    "XPENG_PROVIDER_TOKEN must be set (at least 8 characters); "
                    "xpengmate sends it as TOKEN and every provider route needs it"
                )
        if self.simulate and not self.simulate_file:
            problems.append("XPENG_SIMULATE_FILE is required in simulate mode")
        if problems:
            raise ConfigError("; ".join(problems))


def load_config() -> Config:
    return Config(
        vin=_env("XPENG_VIN"),
        usable_battery_kwh=_env_float("XPENG_USABLE_BATTERY_KWH", 87.5),
        cert_dir=_env("XPENG_CERT_DIR", "/data/certs"),
        tenant_file=_env("XPENG_TENANT_FILE", "/data/secrets/xpeng_tenant.json"),
        token_file=_env("XPENG_TOKEN_FILE", "/data/secrets/xpeng_tokens.json"),
        state_dir=_env("XPENG_STATE_DIR", "/data/state"),
        mqtt_server=_env("XPENG_MQTT_SERVER", "iot-app-global.xiaopeng.com:8885"),
        scan_interval=_env_float("XPENG_SCAN_INTERVAL", 60.0),
        parked_scan_interval=_env_float("XPENG_PARKED_SCAN_INTERVAL", 900.0),
        rest_interval=_env_float("XPENG_REST_SCAN_INTERVAL", 300.0),
        ota_interval=_env_float("XPENG_OTA_SCAN_INTERVAL", 3600.0),
        watchdog_interval=_env_float("XPENG_WATCHDOG_INTERVAL", 15.0),
        silence_timeout=_env_float("XPENG_SILENCE_TIMEOUT", 300.0),
        token_refresh_margin=_env_float("XPENG_TOKEN_REFRESH_MARGIN", 900.0),
        provider_bind=_env("XPENG_PROVIDER_BIND", "0.0.0.0"),
        provider_port=_env_int("XPENG_PROVIDER_PORT", 8201),
        provider_token=_env("XPENG_PROVIDER_TOKEN"),
        allow_offline=_env_bool("XPENG_ALLOW_OFFLINE", False),
        simulate=_env_bool("XPENG_SIMULATE", False),
        simulate_file=_env("XPENG_SIMULATE_FILE"),
        simulate_speed=_env_float("XPENG_SIMULATE_SPEED", 60.0),
        simulate_loop=_env_bool("XPENG_SIMULATE_LOOP", False),
        status_file=_env("XPENG_STATUS_FILE", "/data/state/status.json"),
        log_level=_env("XPENG_LOG_LEVEL", "INFO").upper(),
    )
