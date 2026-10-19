"""Runtime configuration written by the XpengMate settings page.

The engine's defaults come from the environment, which is what a compose file
is good at. The things a user should be able to change without SSH - which data
source to use, the Home Assistant provider's URL and token, the XPENG
credentials - live in a small JSON file under the mounted data volume instead.
The file wins over the environment; a restart applies it.

Secrets are never echoed by the admin API: the file is mode 0600 and the status
endpoint reports only whether a secret is present.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
from typing import Any

from .config import Config, load_config

_LOGGER = logging.getLogger(__name__)

#: Fields the settings page may override, mapped to their JSON names.
EDITABLE = {
    "source": "source",
    "vin": "vin",
    "usable_battery_kwh": "usable_battery_kwh",
    "upstream_url": "provider_url",
    "upstream_token": "provider_token",
    "ha_url": "ha_url",
    "ha_token": "ha_token",
    "ha_entity_prefix": "ha_entity_prefix",
    "outside_temp_entity": "outside_temp_entity",
}

SECRET_FIELDS = ("upstream_token", "ha_token")


def read_runtime(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        _LOGGER.warning("runtime config %s is unreadable; ignoring it", path)
        return {}
    return data if isinstance(data, dict) else {}


def write_runtime(path: str, update: dict[str, Any]) -> dict[str, Any]:
    """Merge `update` into the runtime file atomically, mode 0600."""
    current = read_runtime(path)
    allowed = set(EDITABLE.values())
    for key, value in update.items():
        if key not in allowed:
            continue
        if value is None:
            continue
        current[key] = value
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, temporary = _mkstemp(directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, indent=2, sort_keys=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return current


def _mkstemp(directory: str) -> tuple[int, str]:
    import tempfile

    return tempfile.mkstemp(prefix=".engine-", dir=directory)


def load_runtime_config(path: str) -> Config:
    """Environment defaults, overridden by the runtime file."""
    base = load_config()
    data = read_runtime(path)
    overrides: dict[str, Any] = {}
    for field, key in EDITABLE.items():
        if key not in data:
            continue
        value = data[key]
        if field == "usable_battery_kwh":
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
        if isinstance(value, str):
            value = value.strip()
        overrides[field] = value
    return dataclasses.replace(base, **overrides)


def sanitized(config: Config) -> dict[str, Any]:
    """The config as the settings page may see it: no secrets."""
    payload: dict[str, Any] = {}
    for field, key in EDITABLE.items():
        if field in SECRET_FIELDS:
            payload[key + "_set"] = bool(getattr(config, field))
        else:
            payload[key] = getattr(config, field)
    payload.update(
        {
            "provider_port": config.provider_port,
            "admin_port": config.admin_port,
            "cert_dir": config.cert_dir,
            "tenant_file": config.tenant_file,
            "token_file": config.token_file,
        }
    )
    return payload


def credentials_summary(config: Config) -> dict[str, Any]:
    """What credentials are installed, and enough identity to recognise them."""
    summary: dict[str, Any] = {
        "certs": {},
        "tenant": {"present": False},
        "tokens": {"present": False},
    }
    for name in ("ca-chain.crt", "client-cert-0.pem", "client-key.pem"):
        path = Path(config.cert_dir) / name
        try:
            stat = path.stat()
            summary["certs"][name] = {"present": True, "bytes": stat.st_size}
        except OSError:
            summary["certs"][name] = {"present": False}
    try:
        tenant = json.loads(Path(config.tenant_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        tenant = None
    if isinstance(tenant, dict):
        device_id = str(tenant.get("deviceId") or "")
        summary["tenant"] = {
            "present": True,
            "device_id_tail": device_id[-6:] if device_id else "",
            "key_count": len(tenant.get("keys") or {}),
            "fetched_at": tenant.get("fetchedAt"),
        }
    try:
        wrapper = json.loads(Path(config.token_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        wrapper = None
    tokens = None
    if isinstance(wrapper, dict):
        tokens = wrapper.get("json") if isinstance(wrapper.get("json"), dict) else wrapper
    if isinstance(tokens, dict):
        summary["tokens"] = {
            "present": bool(tokens.get("accessToken")),
            "account": tokens.get("account_email") or tokens.get("email") or "",
            "vin": tokens.get("vin") or "",
            "uid": tokens.get("uid") or "",
            "has_refresh": bool(tokens.get("refreshToken")),
        }
    return summary
