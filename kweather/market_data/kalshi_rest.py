"""Kalshi Trading REST API client.

Includes a 10 req/s token bucket and exponential-backoff retries on 429/5xx.
Order placement is post-only by default. Cancel-then-replace is the only refresh path.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from kweather.auth import KalshiSigner
from kweather.config import Settings
from kweather.types import Bracket, Market

log = logging.getLogger(__name__)


class TokenBucket:
    def __init__(self, rate_per_sec: float, burst: int | None = None):
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(int(rate_per_sec), 1)
        self.tokens = float(self.capacity)
        self.last = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self, n: int = 1) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
                self.last = now
                if self.tokens >= n:
                    self.tokens -= n
                    return
                wait = (n - self.tokens) / self.rate
                await asyncio.sleep(wait)


@dataclass
class PlacementResult:
    client_order_id: str
    exchange_order_id: str | None
    accepted: bool
    raw: dict[str, Any]


# Kalshi weather event ticker patterns.
KXHIGH_RE = re.compile(r"^KXHIGH(?P<city>[A-Z]+)-(?P<date>\d{2}[A-Z]{3}\d{2})-(?P<bracket>[T0-9-]+)$")
KXLOW_RE = re.compile(r"^KXLOW(?P<city>[A-Z]+)-(?P<date>\d{2}[A-Z]{3}\d{2})-(?P<bracket>[T0-9-]+)$")


def _parse_kalshi_date(s: str) -> str:
    """Convert Kalshi's 25NOV05 → 2025-11-05."""
    months = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    yy, mon, dd = s[:2], s[2:5], s[5:7]
    year = 2000 + int(yy)
    month = months.get(mon, 1)
    return f"{year:04d}-{month:02d}-{int(dd):02d}"


def _parse_bracket(label: str) -> Bracket:
    """Decode a Kalshi bracket suffix.

    Examples:
        "T76"      -> single integer 76 → lo=76, hi=77
        "73-T-77"  -> range 73..77 inclusive → lo=73, hi=78
        "B72"      -> below 72 → lo=None, hi=72  (we treat 'B' as below; some versions use 'BL')
        "A77"      -> above 77 → lo=78, hi=None
    Falls back to a single-temp heuristic if the suffix is unrecognized.
    """
    s = label.upper().strip()
    if s.startswith("T") and s[1:].isdigit():
        v = int(s[1:])
        return Bracket(lo=v, hi=v + 1, label=label)
    m = re.match(r"^(\d+)-T-(\d+)$", s)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) + 1
        return Bracket(lo=lo, hi=hi, label=label)
    if s.startswith("B") and s[1:].isdigit():
        return Bracket(lo=None, hi=int(s[1:]), label=label)
    if s.startswith("A") and s[1:].isdigit():
        return Bracket(lo=int(s[1:]) + 1, hi=None, label=label)
    if s.endswith("OR-BELOW"):
        v = int(re.search(r"\d+", s).group(0))
        return Bracket(lo=None, hi=v + 1, label=label)
    if s.endswith("OR-ABOVE"):
        v = int(re.search(r"\d+", s).group(0))
        return Bracket(lo=v, hi=None, label=label)
    return Bracket(lo=None, hi=None, label=label)


def _city_to_station(city: str, stations: list) -> str | None:
    for s in stations:
        if s.kalshi_city.upper() == city.upper():
            return s.code
    return None


class KalshiREST:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.signer: KalshiSigner | None = None
        if settings.kalshi_key_id and settings.kalshi_private_key_path.exists():
            self.signer = KalshiSigner(settings.kalshi_key_id, settings.kalshi_private_key_path)
        self._client = httpx.AsyncClient(timeout=15.0, base_url=settings.kalshi_base_url)
        parsed = urlparse(settings.kalshi_base_url)
        self._path_prefix = parsed.path.rstrip("/")
        self._bucket = TokenBucket(rate_per_sec=8.0, burst=10)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _signing_path(self, path: str) -> str:
        return f"{self._path_prefix}{path}" if not path.startswith(self._path_prefix) else path

    async def _request(
        self, method: str, path: str, *, params: dict | None = None, json: dict | None = None
    ) -> httpx.Response:
        await self._bucket.take()
        headers: dict[str, str] = {}
        if self.signer is not None:
            headers = self.signer.headers(method, self._signing_path(path))
        for attempt in range(4):
            r = await self._client.request(method, path, params=params, json=json, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504):
                await asyncio.sleep(0.25 * (2**attempt))
                continue
            return r
        r.raise_for_status()
        return r

    async def get_balance(self) -> dict[str, Any]:
        r = await self._request("GET", "/portfolio/balance")
        r.raise_for_status()
        return r.json()

    async def list_markets(
        self, status: str = "open", series_ticker: str | None = None, limit: int = 1000
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": min(limit, 1000), "status": status}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            r = await self._request("GET", "/markets", params=params)
            r.raise_for_status()
            payload = r.json()
            out.extend(payload.get("markets", []))
            cursor = payload.get("cursor")
            if not cursor:
                break
            if len(out) >= limit:
                break
        return out

    async def list_weather_markets(self, stations: list) -> list[Market]:
        out: list[Market] = []
        for series in ("KXHIGH", "KXLOW"):
            try:
                rows = await self.list_markets(status="open", series_ticker=series)
            except Exception:
                log.exception("list_markets %s failed", series)
                continue
            for row in rows:
                ticker = row.get("ticker", "")
                m = KXHIGH_RE.match(ticker) or KXLOW_RE.match(ticker)
                if not m:
                    continue
                target = "high" if ticker.startswith("KXHIGH") else "low"
                station = _city_to_station(m.group("city"), stations)
                if station is None:
                    continue
                bracket = _parse_bracket(m.group("bracket"))
                out.append(
                    Market(
                        ticker=ticker,
                        event_ticker=row.get("event_ticker", ""),
                        station_code=station,
                        target=target,
                        target_date=_parse_kalshi_date(m.group("date")),
                        bracket=bracket,
                        open_ts=_parse_iso_or_now(row.get("open_time")),
                        close_ts=_parse_iso_or_now(row.get("close_time")),
                        last_price=row.get("last_price"),
                        yes_bid=row.get("yes_bid"),
                        yes_ask=row.get("yes_ask"),
                    )
                )
        return out

    async def get_orderbook(self, ticker: str, depth: int = 50) -> dict[str, Any]:
        r = await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})
        r.raise_for_status()
        return r.json()

    async def get_incentives(self) -> dict[str, Any]:
        try:
            r = await self._request("GET", "/incentive-programs")
            r.raise_for_status()
            return r.json()
        except Exception:
            return {}

    async def place_order(
        self,
        ticker: str,
        side: str,        # 'yes' or 'no'
        action: str,      # 'buy' or 'sell'
        price_cents: int,
        qty: int,
        post_only: bool = True,
        time_in_force: str = "GTC",
    ) -> PlacementResult:
        client_order_id = f"kw-{uuid.uuid4().hex[:18]}"
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side,
            "action": action,
            "type": "limit",
            "yes_price": price_cents if side == "yes" else None,
            "no_price": price_cents if side == "no" else None,
            "count": qty,
            "time_in_force": time_in_force,
            "post_only": post_only,
        }
        body = {k: v for k, v in body.items() if v is not None}
        r = await self._request("POST", "/portfolio/orders", json=body)
        try:
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            log.warning("place_order rejected %s: %s", r.status_code, r.text[:200])
            return PlacementResult(
                client_order_id=client_order_id,
                exchange_order_id=None,
                accepted=False,
                raw={"error": str(e), "status": r.status_code, "body": r.text},
            )
        data = r.json()
        return PlacementResult(
            client_order_id=client_order_id,
            exchange_order_id=(data.get("order") or {}).get("order_id"),
            accepted=True,
            raw=data,
        )

    async def cancel_order(self, exchange_order_id: str) -> bool:
        r = await self._request("DELETE", f"/portfolio/orders/{exchange_order_id}")
        return r.status_code in (200, 204)

    async def cancel_all_for_market(self, ticker: str) -> int:
        try:
            r = await self._request("GET", "/portfolio/orders", params={"ticker": ticker, "status": "resting"})
            r.raise_for_status()
            orders = r.json().get("orders", [])
        except Exception:
            return 0
        n = 0
        for o in orders:
            try:
                if await self.cancel_order(o["order_id"]):
                    n += 1
            except Exception:
                continue
        return n


def _parse_iso_or_now(s: str | None) -> datetime:
    if not s:
        return datetime.now(tz=UTC)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(tz=UTC)
