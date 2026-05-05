"""Kalshi Trading REST API client.

Includes a 10 req/s token bucket and exponential-backoff retries on 429/5xx.
Order placement is post-only by default. Cancel-then-replace is the only refresh path.
"""
from __future__ import annotations

import asyncio
import logging
import math
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


# Kalshi weather market ticker shape: <SERIES>-<DATE>-<BRACKET>
# DATE is 25NOV05; BRACKET is one of T<int>, B<num>, A<num>, <num>-T-<num>.
WEATHER_TICKER_RE = re.compile(
    r"^(?P<series>KX(?:HIGH|LOW)T?[A-Z0-9]+)-(?P<date>\d{2}[A-Z]{3}\d{2})-(?P<bracket>.+)$"
)


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


_NUM_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _parse_bracket_from_subtitle(subtitle: str, label: str) -> Bracket | None:
    """Kalshi weather subtitles are unambiguous range descriptions.

    Forms (degree symbol may be present or absent):
        "85° or below"   → lo=None, hi=86
        "94° or above"   → lo=94,   hi=None
        "86° to 87°"     → lo=86,   hi=88
        "below 32"       → lo=None, hi=32 (cold tail variant)
        "above 100"      → lo=101,  hi=None
    """
    s = subtitle.replace("°", "").strip().lower()
    m = re.match(r"^\s*(-?\d+)\s*(?:or)?\s*below\s*$", s)
    if m:
        return Bracket(lo=None, hi=int(m.group(1)) + 1, label=label)
    m = re.match(r"^\s*below\s*(-?\d+)\s*$", s)
    if m:
        return Bracket(lo=None, hi=int(m.group(1)), label=label)
    m = re.match(r"^\s*(-?\d+)\s*(?:or)?\s*above\s*$", s)
    if m:
        return Bracket(lo=int(m.group(1)), hi=None, label=label)
    m = re.match(r"^\s*above\s*(-?\d+)\s*$", s)
    if m:
        return Bracket(lo=int(m.group(1)) + 1, hi=None, label=label)
    m = re.match(r"^\s*(-?\d+)\s*to\s*(-?\d+)\s*$", s)
    if m:
        return Bracket(lo=int(m.group(1)), hi=int(m.group(2)) + 1, label=label)
    return None


def _parse_bracket(label: str) -> Bracket:
    """Decode a Kalshi bracket suffix.

    Kalshi weather convention:
        "T76"        -> tail at 76; direction (lower vs upper) is event-dependent
                        and resolved post-hoc. Provisional: 1° point [76, 77).
        "B<x>.5"     -> 2° range centered at x.5 → [floor(x), floor(x)+2)
                        e.g. "B73.5" → [73, 75) i.e. {73, 74}
                             "B86.5" → [86, 88) i.e. {86, 87}
        "B<int>"     -> "below x" tail → (-∞, x)
        "A<x>.5"     -> "above" tail with rounding → [floor(x)+1, ∞)
        "A<int>"     -> "above" tail → [x+1, ∞)
        "73-T-77"    -> explicit range → [73, 78)
    """
    s = label.upper().strip()
    if s.startswith("T") and _NUM_RE.match(s[1:]):
        v = int(float(s[1:]))
        return Bracket(lo=v, hi=v + 1, label=label)
    m = re.match(r"^(\d+(?:\.\d+)?)-T-(\d+(?:\.\d+)?)$", s)
    if m:
        lo = int(math.floor(float(m.group(1))))
        hi = int(math.floor(float(m.group(2)))) + 1
        return Bracket(lo=lo, hi=hi, label=label)
    if s.startswith("B") and _NUM_RE.match(s[1:]):
        v = float(s[1:])
        if v != math.floor(v):
            base = int(math.floor(v))
            return Bracket(lo=base, hi=base + 2, label=label)
        return Bracket(lo=None, hi=int(v), label=label)
    if s.startswith("A") and _NUM_RE.match(s[1:]):
        return Bracket(lo=int(math.floor(float(s[1:]))) + 1, hi=None, label=label)
    if s.endswith("OR-BELOW"):
        v = float(re.search(r"\d+(?:\.\d+)?", s).group(0))
        return Bracket(lo=None, hi=int(math.ceil(v)) + (0 if v != math.floor(v) else 1), label=label)
    if s.endswith("OR-ABOVE"):
        v = float(re.search(r"\d+(?:\.\d+)?", s).group(0))
        return Bracket(lo=int(math.floor(v)), hi=None, label=label)
    return Bracket(lo=None, hi=None, label=label)


_T_LABEL_RE = re.compile(r"^T(\d+)$", re.IGNORECASE)


def _resolve_tail_brackets(events: dict[str, list[Market]], suffix_only: set[str]) -> None:
    """Within each event, the lowest T<x> label is the lower tail and the
    highest T<x> label is the upper tail. Convention (verified against HIGH
    subtitles like "87° or below" → T88, "above 95" → T95): T<x> on the lower
    end means strictly below x, and on the upper end means strictly above x.
    Subtitle-parsed brackets are already correct, so only rewrite markets in
    `suffix_only`.
    """
    for markets in events.values():
        t_markets: list[tuple[int, Market]] = []
        for mk in markets:
            if mk.ticker not in suffix_only:
                continue
            m = _T_LABEL_RE.match(mk.bracket.label.strip())
            if m:
                t_markets.append((int(m.group(1)), mk))
        if not t_markets:
            continue
        t_markets.sort(key=lambda kv: kv[0])
        lo_v, lo_m = t_markets[0]
        lo_m.bracket = Bracket(lo=None, hi=lo_v, label=lo_m.bracket.label)
        if len(t_markets) > 1:
            hi_v, hi_m = t_markets[-1]
            hi_m.bracket = Bracket(lo=hi_v + 1, hi=None, label=hi_m.bracket.label)


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
        # Build (series_ticker, station_code, target) tuples from explicit station config.
        targets: list[tuple[str, str, str]] = []
        for st in stations:
            if getattr(st, "kalshi_high_series", None):
                targets.append((st.kalshi_high_series, st.code, "high"))
            if getattr(st, "kalshi_low_series", None):
                targets.append((st.kalshi_low_series, st.code, "low"))
        # Track which markets relied on suffix-only parsing so we can resolve T<x>
        # tails post-hoc (lowest T<x> in an event = lower tail; highest = upper tail).
        suffix_only: set[str] = set()
        events: dict[str, list[Market]] = {}
        for series, station_code, target in targets:
            try:
                rows = await self.list_markets(status="open", series_ticker=series)
            except Exception:
                log.exception("list_markets %s failed", series)
                continue
            for row in rows:
                ticker = row.get("ticker", "")
                m = WEATHER_TICKER_RE.match(ticker)
                if not m:
                    continue
                subtitle = row.get("subtitle") or ""
                bracket = _parse_bracket_from_subtitle(subtitle, m.group("bracket"))
                if bracket is None:
                    bracket = _parse_bracket(m.group("bracket"))
                    suffix_only.add(ticker)
                market = Market(
                    ticker=ticker,
                    event_ticker=row.get("event_ticker", ""),
                    station_code=station_code,
                    target=target,
                    target_date=_parse_kalshi_date(m.group("date")),
                    bracket=bracket,
                    open_ts=_parse_iso_or_now(row.get("open_time")),
                    close_ts=_parse_iso_or_now(row.get("close_time")),
                    last_price=row.get("last_price"),
                    yes_bid=row.get("yes_bid"),
                    yes_ask=row.get("yes_ask"),
                )
                out.append(market)
                events.setdefault(market.event_ticker, []).append(market)
        _resolve_tail_brackets(events, suffix_only)
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
