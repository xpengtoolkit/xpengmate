"""Client for EVConduit's cursor-paged XPENG data API (`/api/v1/xpeng`).

This is the documented, API-key surface that replaces both the old "newest 200"
endpoints and reading EVConduit's database directly. It is read-only and pages
every collection with an opaque keyset cursor, so a full history can be pulled
without a 200-row ceiling and a rolling 30-day export cannot shift an offset
mid-sync.

Resource shapes, timestamps (UTC, ISO 8601 with Z) and error codes follow
`docs/xpeng-data-api.md` in the EVConduit repository.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

_LOGGER = logging.getLogger("xpengmate.history.xpeng_api")

DEFAULT_BASE = "https://backend.evconduit.com"
MAX_RETRIES = 4
USER_AGENT = "XpengMate/4.4 (+https://github.com/xpengtoolkit/xpengmate)"


class ApiError(RuntimeError):
    """A non-retryable failure from the data API."""


class XpengDataApi:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        retry_after_default: float = 5.0,
    ) -> None:
        base = (base_url or DEFAULT_BASE).rstrip("/")
        if not base.endswith("/api/v1/xpeng"):
            base = base + "/api/v1/xpeng"
        self.base = base
        self._key = api_key
        self._timeout = timeout
        self._retry_after_default = retry_after_default
        self.requests = 0
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None

    # -- transport -------------------------------------------

    def _request(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{self.base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            # Cloudflare in front of the API blocks Python's default
            # urllib User-Agent with error 1010, so identify ourselves.
            "User-Agent": USER_AGENT,
        }
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES):
            self.requests += 1
            request = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(request, timeout=self._timeout) as response:
                    self._read_rate_headers(response.headers)
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = _safe_body(exc)
                if exc.code == 429:
                    wait = _retry_after(exc) or self._retry_after_default
                    _LOGGER.info("rate limited; retrying in %.0fs", wait)
                    time.sleep(wait)
                    last_error = exc
                    continue
                if exc.code in (502, 503, 504):
                    time.sleep(1.5 * (attempt + 1))
                    last_error = exc
                    continue
                if exc.code == 401:
                    raise ApiError("EVConduit rejected the API key (401)") from exc
                raise ApiError(f"HTTP {exc.code} from {path}: {body[:300]}") from exc
            except Exception as exc:  # noqa: BLE001 - network hiccup, retry
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        raise ApiError(f"EVConduit API unavailable after {MAX_RETRIES} attempts: {last_error}")

    def _read_rate_headers(self, headers: Any) -> None:
        try:
            remaining = headers.get("X-RateLimit-Remaining")
            reset = headers.get("X-RateLimit-Reset")
            if remaining is not None:
                self.rate_remaining = int(remaining)
            if reset is not None:
                self.rate_reset = int(reset)
        except (TypeError, ValueError):
            pass

    # -- pagination ------------------------------------------

    def pages(
        self,
        resource: str,
        *,
        limit: int | None = None,
        vin: str | None = None,
        since: str | None = None,
        until: str | None = None,
        updated_since: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"cursor": cursor}
            if limit:
                params["limit"] = limit
            if vin:
                params["vin"] = vin
            if since:
                params["since"] = since
            if until:
                params["until"] = until
            if updated_since:
                params["updated_since"] = updated_since
            payload = self._request(f"/{resource}", params)
            data = payload.get("data") or []
            for item in data:
                if isinstance(item, dict):
                    yield item
            pagination = payload.get("pagination") or {}
            if not pagination.get("has_more"):
                break
            cursor = pagination.get("next_cursor")
            if not cursor:
                break
            # Stay well under the documented per-key allowance.
            if self.rate_remaining is not None and self.rate_remaining <= 2:
                time.sleep(self._retry_after_default)

    def collect(self, resource: str, **kwargs: Any) -> list[dict[str, Any]]:
        items = list(self.pages(resource, **kwargs))
        _LOGGER.info("EVConduit %s: %d rows", resource, len(items))
        return items

    # -- resources -------------------------------------------

    def trips(self, vin: str | None = None) -> list[dict[str, Any]]:
        return self.collect("trips", vin=vin)

    def charging_sessions(self, vin: str | None = None) -> list[dict[str, Any]]:
        return self.collect("charging-sessions", vin=vin)

    def positions(self, vin: str | None = None) -> list[dict[str, Any]]:
        return self.collect("positions", vin=vin)

    def tyre_pressures(self, vin: str | None = None) -> list[dict[str, Any]]:
        return self.collect("tyre-pressures", vin=vin)

    def state_spans(self, vin: str | None = None) -> list[dict[str, Any]]:
        return self.collect("state-spans", vin=vin)

    def charge_curves(self, vin: str | None = None) -> list[dict[str, Any]]:
        return self.collect("charge-curve-samples", vin=vin)

    def export_files(self, link_ttl_seconds: int = 900) -> list[dict[str, Any]]:
        return self.collect("export-files", limit=200, link_ttl_seconds=link_ttl_seconds)


def _safe_body(error: urllib.error.HTTPError) -> str:
    try:
        return error.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _retry_after(error: urllib.error.HTTPError) -> float | None:
    value = error.headers.get("Retry-After") if error.headers else None
    if not value:
        return None
    try:
        return max(1.0, float(value))
    except ValueError:
        return None


def build_curve(curve: dict[str, Any]) -> dict[str, list[Any]] | None:
    """Convert the API's charge-curve sample objects into Store arrays."""
    samples = curve.get("samples") or []
    if len(samples) < 2:
        return None
    times: list[float] = []
    power: list[float | None] = []
    soc: list[float | None] = []
    volt: list[float | None] = []
    amp: list[float | None] = []
    for sample in samples:
        try:
            times.append(float(sample.get("t_seconds")))
        except (TypeError, ValueError):
            continue
        power.append(_float(sample.get("power_kw")))
        soc.append(_float(sample.get("soc_pct")))
        volt.append(_float(sample.get("pack_volt_v")))
        amp.append(_float(sample.get("pack_amp_a")))
    if len(times) < 2:
        return None
    return {"t": times, "input_kw": power, "soc": soc, "volt": volt, "amp": amp}


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
