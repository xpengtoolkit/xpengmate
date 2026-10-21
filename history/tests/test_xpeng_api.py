import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from xpeng_api import build_curve
from xpeng_api import XpengDataApi


class _Handler(BaseHTTPRequestHandler):
    pages = []
    calls = []
    fail_once = False

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        type(self).calls.append(self.path)
        if type(self).fail_once:
            type(self).fail_once = False
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(b'{"detail":"rate limited"}')
            return
        page = type(self).pages.pop(0)
        body = json.dumps(page).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-RateLimit-Remaining", "100")
        self.send_header("X-RateLimit-Reset", "0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class XpengDataApiTest(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        _Handler.calls = []
        _Handler.fail_once = False

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_follows_cursor_until_has_more_is_false(self):
        _Handler.pages = [
            {"data": [{"id": "1"}, {"id": "2"}], "pagination": {"has_more": True, "next_cursor": "abc"}},
            {"data": [{"id": "3"}], "pagination": {"has_more": False, "next_cursor": None}},
        ]
        client = XpengDataApi(self.base, "key")
        rows = client.collect("trips", vin="VIN1")
        self.assertEqual([row["id"] for row in rows], ["1", "2", "3"])
        self.assertIn("cursor=abc", _Handler.calls[1])
        self.assertIn("vin=VIN1", _Handler.calls[0])
        self.assertEqual(client.requests, 2)

    def test_retries_rate_limit(self):
        _Handler.fail_once = True
        _Handler.pages = [{"data": [{"id": "1"}], "pagination": {"has_more": False}}]
        client = XpengDataApi(self.base, "key", retry_after_default=0.1)
        rows = client.collect("trips")
        self.assertEqual(len(rows), 1)
        self.assertEqual(client.requests, 2)

    def test_build_curve_maps_sample_objects(self):
        curve = {
            "samples": [
                {"t_seconds": 0, "power_kw": 6.9, "soc_pct": 49, "pack_volt_v": 554, "pack_amp_a": 11.5},
                {"t_seconds": 60, "power_kw": 7.0, "soc_pct": 50, "pack_volt_v": 555, "pack_amp_a": 12.0},
                {"t_seconds": 120, "power_kw": None, "soc_pct": 51, "pack_volt_v": 555, "pack_amp_a": 12.0},
            ]
        }
        arrays = build_curve(curve)
        self.assertEqual(arrays["t"], [0.0, 60.0, 120.0])
        self.assertEqual(arrays["input_kw"], [6.9, 7.0, None])
        self.assertEqual(arrays["soc"], [49.0, 50.0, 51.0])


if __name__ == "__main__":
    unittest.main()
