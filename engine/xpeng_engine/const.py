"""The few constants the vendored modules expect from the integration's const.py.

The upstream ``custom_components/xpeng/const.py`` imports Home Assistant's
sensor/binary-sensor device classes because it doubles as the entity registry.
The vendored modules only need a handful of pure values, so this module provides
those and nothing else - which is what keeps the engine installable without
Home Assistant.
"""

from __future__ import annotations

import os

#: Where the three mTLS files live when no directory is configured. The service
#: always calls ``xpeng_api.set_cert_dir()`` with the resolved path, so this is
#: only the fallback the vendored module uses before that happens.
DEFAULT_CERT_DIR = os.environ.get("XPENG_CERT_DIR") or "/data/certs"

#: The XPENG broker the car's telemetry arrives from.
DEFAULT_SERVER = "iot-app-global.xiaopeng.com:8885"

#: The REST control plane (login, MQTT credential minting, OTA status).
DEFAULT_LOGIN_HOST = "https://apps-global.xiaopeng.com"
