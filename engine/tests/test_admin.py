#!/usr/bin/env python3
"""The settings API the XpengMate web UI talks to.

It writes real files into temporary directories, so these tests also pin the
things that would be security bugs: file modes, refusing malformed PEM/JSON, and
never echoing a secret back.

    python3 -m unittest engine.tests.test_admin -v
"""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None

from xpeng_engine import runtime
from xpeng_engine.admin import AdminServer
from xpeng_engine.config import Config

ADMIN_TOKEN = "admin-test-token"
CA = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"
KEY = "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
TENANT = {"deviceId": "4dc51b29c3dee8b8ea6f7648ce407ffa", "keys": {"0": "AAAA"}, "version": 2}
TOKENS = {
    "accessToken": "sealed-access",
    "refreshToken": "sealed-refresh",
    "uid": "10225403",
    "vin": "L1NNSGHA3SB800783",
    "account_email": "test@example.com",
}


@unittest.skipIf(aiohttp is None, "aiohttp is not installed")
class AdminApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="xpengmate-admin-")
        self.config = Config(
            vin="L1NNSGHA3SB800783",
            cert_dir=os.path.join(self.root, "certs"),
            tenant_file=os.path.join(self.root, "secrets", "tenant.json"),
            token_file=os.path.join(self.root, "secrets", "tokens.json"),
            runtime_file=os.path.join(self.root, "config", "engine.json"),
            provider_token="local-provider-token",
            admin_token=ADMIN_TOKEN,
            source="ha",
            ha_url="http://ha",
            ha_token="ha-token",
            ha_entity_prefix="xpeng_800783",
        )
        self.server = AdminServer(
            config=self.config,
            status_provider=lambda: {"mode": "ha", "soc": 59},
            restart=lambda: None,
            port=0,
            bind="127.0.0.1",
            token=ADMIN_TOKEN,
        )
        await self.server.async_start()
        port = self.server._site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        self.headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    async def asyncTearDown(self) -> None:
        await self.server.async_stop()

    async def _get(self, session, path):
        async with session.get(f"{self.base}{path}", headers=self.headers) as response:
            return response.status, await response.json()

    async def test_requires_admin_token(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{self.base}/status") as response:
                self.assertEqual(response.status, 401)

    async def test_status_never_returns_a_secret(self) -> None:
        async with aiohttp.ClientSession() as session:
            status, payload = await self._get(session, "/status")
        self.assertEqual(status, 200)
        self.assertEqual(payload["source"], "ha")
        self.assertNotIn("ha_token", payload["config"])
        self.assertTrue(payload["config"]["ha_token_set"])
        self.assertEqual(payload["soc"], 59)

    async def test_put_config_writes_the_runtime_file(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{self.base}/config",
                headers=self.headers,
                json={
                    "source": "provider",
                    "provider_url": "http://homeassistant.local:8200",
                    "provider_token": "upstream-token",
                },
            ) as response:
                self.assertEqual(response.status, 200)
                self.assertTrue((await response.json())["restarting"])
        stored = runtime.read_runtime(self.config.runtime_file)
        self.assertEqual(stored["source"], "provider")
        self.assertEqual(stored["provider_url"], "http://homeassistant.local:8200")
        self.assertEqual(
            stat.S_IMODE(os.stat(self.config.runtime_file).st_mode), 0o600
        )

    async def test_put_config_rejects_unknown_settings(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                f"{self.base}/config",
                headers=self.headers,
                json={"source": "ha", "not_a_setting": 1},
            ) as response:
                self.assertEqual(response.status, 400)

    async def test_credentials_are_written_with_mode_600(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base}/credentials",
                headers=self.headers,
                json={
                    "ca_chain": CA,
                    "client_cert": CA,
                    "client_key": KEY,
                    "tenant": TENANT,
                    "tokens": TOKENS,
                },
            ) as response:
                self.assertEqual(response.status, 200, await response.text())
                payload = await response.json()
        self.assertEqual(payload["installed"], ["ca_chain.crt", "tenant", "tokens"])
        for name in ("ca-chain.crt", "client-cert-0.pem", "client-key.pem"):
            path = Path(self.config.cert_dir) / name
            self.assertTrue(path.is_file())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        tenant = json.loads(Path(self.config.tenant_file).read_text())
        self.assertEqual(tenant["deviceId"], TENANT["deviceId"])
        wrapper = json.loads(Path(self.config.token_file).read_text())
        self.assertEqual(wrapper["json"]["accessToken"], "sealed-access")
        self.assertIn("jsonText", wrapper)
        stored = runtime.read_runtime(self.config.runtime_file)
        self.assertEqual(stored["source"], "xpeng")
        self.assertEqual(stored["vin"], "L1NNSGHA3SB800783")

    async def test_credentials_reject_a_bad_pem(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base}/credentials",
                headers=self.headers,
                json={
                    "ca_chain": "not a pem",
                    "client_cert": CA,
                    "client_key": KEY,
                    "tenant": TENANT,
                    "tokens": TOKENS,
                },
            ) as response:
                self.assertEqual(response.status, 400)
                self.assertIn("PEM", (await response.json())["error"])

    async def test_test_endpoint_reports_native_credentials(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base}/test", headers=self.headers, json={"source": "xpeng"}
            ) as response:
                payload = await response.json()
        self.assertFalse(payload["ok"])
        self.assertIn("credentials", payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
