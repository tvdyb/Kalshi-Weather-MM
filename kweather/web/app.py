"""FastAPI app + SSE for the localhost dashboard."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from kweather.web.events import EventBus

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(WEB_DIR / "templates"))


class DashboardState:
    """Shared state the FastAPI handlers read from. The orchestrator updates this
    object whenever theos, orderbooks, quotes, or reward snapshots change."""

    def __init__(self) -> None:
        self.markets: dict[str, dict[str, Any]] = {}
        self.theos: dict[str, dict[str, Any]] = {}
        self.books: dict[str, dict[str, Any]] = {}
        self.quotes: dict[str, dict[str, Any]] = {}
        self.rewards: dict[str, dict[str, Any]] = {}
        self.vol_windows: list[dict[str, Any]] = []
        self.theo_history: dict[str, list[tuple[str, int]]] = {}
        self.connected_ws: bool = False
        self.last_theo_refresh: datetime | None = None
        self.last_orderbook_update: datetime | None = None
        self.mode: str = "paper"
        self.kill_switch: bool = False
        self.paused: bool = False
        self.daily_pnl_cents: float = 0.0


def make_app(state: DashboardState, bus: EventBus, controls) -> FastAPI:
    app = FastAPI(title="Kalshi Weather MM", default_response_class=JSONResponse)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> Any:
        return TEMPLATES.TemplateResponse(
            request,
            "index.html",
            {"state": state},
        )

    @app.get("/api/state", response_class=JSONResponse)
    async def api_state() -> Any:
        return _serialize_state(state)

    @app.get("/api/rows", response_class=HTMLResponse)
    async def api_rows(request: Request) -> Any:
        return TEMPLATES.TemplateResponse(
            request,
            "rows.html",
            {"rows": _market_rows(state)},
        )

    @app.get("/api/health", response_class=PlainTextResponse)
    async def health() -> str:
        return "ok"

    @app.get("/sse")
    async def sse(request: Request) -> EventSourceResponse:
        async def gen():
            yield {"event": "snapshot", "data": _serialize_state(state)}
            async for raw in bus.subscribe():
                if await request.is_disconnected():
                    break
                yield raw
        return EventSourceResponse(gen())

    @app.post("/api/pause")
    async def pause() -> Any:
        state.paused = True
        await controls.pause()
        return {"paused": True}

    @app.post("/api/resume")
    async def resume() -> Any:
        state.paused = False
        await controls.resume()
        return {"paused": False}

    @app.post("/api/withdraw")
    async def withdraw_all() -> Any:
        await controls.withdraw_all()
        return {"withdrawn": True}

    @app.post("/api/kill-station/{station_code}")
    async def kill_station(station_code: str) -> Any:
        await controls.kill_station(station_code)
        return {"station": station_code, "killed": True}

    return app


def _serialize_state(state: DashboardState) -> dict[str, Any]:
    return {
        "ts": datetime.now(tz=UTC).isoformat(),
        "connected_ws": state.connected_ws,
        "last_theo_refresh": state.last_theo_refresh.isoformat() if state.last_theo_refresh else None,
        "last_orderbook_update": state.last_orderbook_update.isoformat() if state.last_orderbook_update else None,
        "mode": state.mode,
        "kill_switch": state.kill_switch,
        "paused": state.paused,
        "daily_pnl_cents": state.daily_pnl_cents,
        "vol_windows": state.vol_windows[:12],
        "rows": _market_rows(state),
    }


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _format_bracket(lo: float | None, hi: float | None) -> str:
    lo_i = int(lo) if lo is not None else None
    hi_i = int(hi) if hi is not None else None
    if lo_i is None and hi_i is not None:
        return f"≤{hi_i - 1}°"
    if lo_i is not None and hi_i is None:
        return f"≥{lo_i}°"
    if lo_i is not None and hi_i is not None:
        if hi_i - lo_i == 1:
            return f"{lo_i}°"
        return f"{lo_i}–{hi_i - 1}°"  # noqa: RUF001
    return "—"


def _format_date_short(target_date: str | None) -> str:
    if not target_date:
        return ""
    try:
        y, m, d = target_date.split("-")
        return f"{_MONTH_ABBR[int(m) - 1]} {int(d)}"
    except Exception:
        return target_date


def _market_display_name(m: dict[str, Any]) -> str:
    station = (m.get("station_code") or "").lstrip("K") or "?"
    target = (m.get("target") or "").upper() or "?"
    when = _format_date_short(m.get("target_date"))
    rng = _format_bracket(m.get("bracket_lo"), m.get("bracket_hi"))
    parts = [station, target, when, rng]
    return " · ".join(p for p in parts if p)


def _kalshi_url(ticker: str, event_ticker: str | None) -> str:
    base = (event_ticker or ticker).lower()
    return f"https://kalshi.com/markets/{base}"


def _market_rows(state: DashboardState) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ticker, m in state.markets.items():
        theo = state.theos.get(ticker, {})
        book = state.books.get(ticker, {})
        quote = state.quotes.get(ticker, {})
        reward_yes = state.rewards.get(f"{ticker}|yes", {})
        reward_no = state.rewards.get(f"{ticker}|no", {})
        rows.append(
            {
                "ticker": ticker,
                "display_name": _market_display_name(m),
                "kalshi_url": _kalshi_url(ticker, m.get("event_ticker")),
                "station": m.get("station_code"),
                "target_date": m.get("target_date"),
                "bracket_label": m.get("bracket_label"),
                "theo_cents": theo.get("fair_price_cents"),
                "sigma_f": theo.get("sigma_f"),
                "best_bid": book.get("best_bid"),
                "best_ask": book.get("best_ask"),
                "mid": book.get("mid"),
                "spread": book.get("spread"),
                "edge_to_mid": (
                    (theo.get("fair_price_cents") or 0) - (book.get("mid") or 0)
                    if theo.get("fair_price_cents") is not None and book.get("mid") is not None
                    else None
                ),
                "our_bid": quote.get("bid_price"),
                "our_ask": quote.get("ask_price"),
                "position": quote.get("position", 0),
                "tob_bid": quote.get("tob_bid", False),
                "tob_ask": quote.get("tob_ask", False),
                "reward_eligible_bid": reward_yes.get("eligible", False),
                "reward_eligible_ask": reward_no.get("eligible", False),
                "state": quote.get("state", "flat"),
                "pnl_cents": quote.get("pnl_cents", 0),
                "theo_history": state.theo_history.get(ticker, [])[-60:],
            }
        )
    rows.sort(key=lambda r: (r["station"] or "", r["target_date"] or "", r["ticker"]))
    return rows
