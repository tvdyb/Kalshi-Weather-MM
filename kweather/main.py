"""Entry point. Wires Quoter + MarketData + TheoEngine + WebApp + RewardsTracker.

Boot ordering:
    1. Load settings, init storage.
    2. (live mode only) Probe Kalshi balance to verify trading-tier key.
    3. Start TheoEngine (refresh forecast, then run loop).
    4. Start MarketData REST list refresh and WS subscription.
    5. Start Quoter (subscribes to TheoEngine + book updates).
    6. Start VolController.
    7. Start RewardsTracker.
    8. Start FastAPI/uvicorn.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict, deque
from datetime import UTC, datetime
from typing import Any

import uvicorn

try:
    import uvloop  # type: ignore
    uvloop.install()
except Exception:
    pass

from kweather.config import Settings, load_settings
from kweather.market_data.kalshi_rest import KalshiREST
from kweather.market_data.kalshi_ws import KalshiWS
from kweather.market_data.orderbook import OrderBookStore
from kweather.quoter.decision import decide
from kweather.quoter.placer import PaperPlacer, Placer
from kweather.rewards.tracker import RewardsTracker
from kweather.storage.db import Store
from kweather.theo.engine import TheoEngine
from kweather.types import Market, Theo
from kweather.vol.controller import VolController
from kweather.vol.schedule import widen_factors_for
from kweather.web.app import DashboardState, make_app
from kweather.web.events import EventBus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("kweather")


class Controls:
    """Dashboard control surface that wraps the orchestrator."""

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.o = orchestrator

    async def pause(self) -> None:
        self.o.paused = True

    async def resume(self) -> None:
        self.o.paused = False

    async def withdraw_all(self) -> None:
        await self.o.placer.cancel_all()

    async def kill_station(self, station_code: str) -> None:
        markets = self.o.markets_by_station()
        await self.o.placer.cancel_all_for_station(station_code, markets)


class Orchestrator:
    def __init__(self, settings: Settings, paper: bool):
        self.settings = settings
        self.paper = paper
        self.bus = EventBus()
        self.state = DashboardState()
        self.state.mode = "paper" if paper else "live"
        self.state.kill_switch = settings.kill_switch
        self.store = Store(settings.db_path)
        self.rest = KalshiREST(settings)
        self.book_store = OrderBookStore()
        self.theo_engine = TheoEngine(settings)
        self.theo_engine.add_listener(self._on_theo)
        placer_cls = PaperPlacer if (paper or settings.kill_switch) else Placer
        self.placer = placer_cls(settings, self.rest, self.store, settings.risk)
        self.rewards = RewardsTracker(self.rest, self.placer, self.store)
        self.markets: dict[str, Market] = {}
        self._theo_history: dict[str, deque[tuple[str, int]]] = defaultdict(lambda: deque(maxlen=240))
        self.vol = VolController(
            settings,
            cancel_station_fn=self._vol_cancel_station,
            requote_station_fn=self._vol_requote_station,
            wait_for_refresh_fn=self.theo_engine.wait_for_refresh_after,
        )
        self.ws = KalshiWS(settings, self._on_ws_message)
        self.ws.add_reconnect_hook(self._resync_books)
        self.ws.add_state_change_hook(self._on_ws_state)
        self.paused = False
        self._tasks: list[asyncio.Task] = []

    def markets_by_station(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = defaultdict(list)
        for t, m in self.markets.items():
            out[m.station_code].append(t)
        return out

    async def boot(self) -> None:
        await self.store.init()
        self.state.last_theo_refresh = None
        if not self.paper:
            try:
                bal = await self.rest.get_balance()
                log.info("Kalshi balance check OK: %s", bal)
            except Exception:
                log.exception("Kalshi balance check FAILED — refusing to start in live mode")
                raise SystemExit(2)
        await self._refresh_markets()

    async def _refresh_markets(self) -> None:
        try:
            ms = await self.rest.list_weather_markets(self.settings.stations)
        except Exception:
            log.exception("list_weather_markets failed")
            return
        for m in ms:
            self.markets[m.ticker] = m
            self.state.markets[m.ticker] = {
                "station_code": m.station_code,
                "target_date": m.target_date,
                "bracket_label": m.bracket.label,
                "bracket_lo": m.bracket.lo,
                "bracket_hi": m.bracket.hi,
                "target": m.target,
                "event_ticker": m.event_ticker,
            }
            await self.store.upsert_market(
                {
                    "ticker": m.ticker,
                    "event_ticker": m.event_ticker,
                    "station_code": m.station_code,
                    "target": m.target,
                    "target_date": m.target_date,
                    "bracket_lo": m.bracket.lo,
                    "bracket_hi": m.bracket.hi,
                    "bracket_label": m.bracket.label,
                    "open_ts": m.open_ts.isoformat(),
                    "close_ts": m.close_ts.isoformat(),
                }
            )
        if ms:
            await self.ws.subscribe([m.ticker for m in ms])
            log.info("Refreshed %d weather markets", len(ms))

    async def _on_theo(self, theo: Theo) -> None:
        await self.store.insert_theo(
            {
                "market_ticker": theo.market_ticker,
                "fair_price_cents": theo.fair_price_cents,
                "fair_prob": theo.fair_prob,
                "mu_f": theo.mu_f,
                "sigma_f": theo.sigma_f,
                "ts": theo.ts.isoformat(),
            }
        )
        self.state.theos[theo.market_ticker] = {
            "fair_price_cents": theo.fair_price_cents,
            "fair_prob": theo.fair_prob,
            "mu_f": theo.mu_f,
            "sigma_f": theo.sigma_f,
            "ts": theo.ts.isoformat(),
        }
        self._theo_history[theo.market_ticker].append((theo.ts.isoformat(), theo.fair_price_cents))
        self.state.theo_history[theo.market_ticker] = list(self._theo_history[theo.market_ticker])
        self.state.last_theo_refresh = theo.ts
        await self._maybe_quote(theo.market_ticker)
        self.bus.publish("delta", {"theo": self.state.theos[theo.market_ticker], "ticker": theo.market_ticker})

    async def _on_ws_message(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type") or msg.get("msg")
        ch = msg.get("channel")
        payload = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
        # Orderbook snapshot or delta
        if ch == "orderbook_delta" or msg_type in ("orderbook_delta", "orderbook_snapshot"):
            ticker = payload.get("market_ticker") or payload.get("ticker")
            if ticker is None:
                return
            if msg_type == "orderbook_snapshot":
                self.book_store.reset(ticker, payload)
            else:
                self.book_store.apply_delta(ticker, payload)
            book = self.book_store.get(ticker)
            if book is not None:
                bb = book.best_bid()
                ba = book.best_ask()
                self.state.books[ticker] = {
                    "best_bid": bb.price_cents if bb else None,
                    "best_ask": ba.price_cents if ba else None,
                    "mid": book.mid_cents(),
                    "spread": (ba.price_cents - bb.price_cents) if (bb and ba) else None,
                }
                await self.store.insert_book_snapshot(
                    {
                        "market_ticker": ticker,
                        "ts": book.ts.isoformat(),
                        "best_bid": bb.price_cents if bb else None,
                        "best_ask": ba.price_cents if ba else None,
                        "bid_qty": bb.qty if bb else None,
                        "ask_qty": ba.qty if ba else None,
                    }
                )
                self.state.last_orderbook_update = book.ts
                self.state.connected_ws = True
                await self._maybe_quote(ticker)
        elif ch == "fill" or msg_type == "fill":
            await self._on_fill(payload)
        elif ch == "trade" or msg_type == "trade":
            pass

    async def _on_fill(self, payload: dict[str, Any]) -> None:
        ticker = payload.get("market_ticker") or payload.get("ticker")
        if ticker is None:
            return
        await self.store.insert_fill(
            {
                "exchange_fill_id": payload.get("trade_id") or payload.get("fill_id"),
                "client_order_id": payload.get("client_order_id"),
                "market_ticker": ticker,
                "side": payload.get("side", "yes"),
                "action": payload.get("action", "buy"),
                "price_cents": int(payload.get("yes_price") or payload.get("price") or 0),
                "qty": int(payload.get("count") or payload.get("qty") or 0),
                "fee_cents": float(payload.get("fee") or 0.0),
                "ts": (payload.get("created_ts") or datetime.now(tz=UTC).isoformat()),
            }
        )
        await self._refresh_pnl()

    async def _refresh_pnl(self) -> None:
        try:
            self.state.daily_pnl_cents = await self.store.fetch_today_pnl()
        except Exception:
            pass

    async def _on_ws_state(self, connected: bool) -> None:
        self.state.connected_ws = connected

    async def _resync_books(self) -> None:
        for ticker in list(self.markets.keys()):
            try:
                snap = await self.rest.get_orderbook(ticker, depth=200)
                self.book_store.reset(ticker, snap)
            except Exception:
                continue

    async def _maybe_quote(self, ticker: str) -> None:
        if self.paused or self.settings.kill_switch:
            return
        market = self.markets.get(ticker)
        theo_d = self.state.theos.get(ticker)
        book = self.book_store.get(ticker)
        if market is None or theo_d is None or book is None:
            return
        # Daily P&L stop
        if self.state.daily_pnl_cents <= -self.settings.risk.daily_loss_stop_usd * 100:
            return
        theo = Theo(
            market_ticker=ticker,
            station_code=market.station_code,
            target_date=market.target_date,
            bracket=market.bracket,
            fair_price_cents=theo_d["fair_price_cents"],
            fair_prob=theo_d["fair_prob"],
            mu_f=theo_d["mu_f"],
            sigma_f=theo_d["sigma_f"],
            ts=datetime.fromisoformat(theo_d["ts"]),
        )
        # Probabilistic widen factors per station
        station = next(s for s in self.settings.stations if s.code == market.station_code)
        widen, size = widen_factors_for(self.settings, station)
        intent = decide(theo, book, self.settings.risk, widen_factor=widen, size_factor=size)
        await self.placer.apply(ticker, intent)
        resting = self.placer.resting(ticker)
        bid_q = resting.get("yes")
        ask_q = resting.get("no")
        bb = book.best_bid()
        ba = book.best_ask()
        self.state.quotes[ticker] = {
            "bid_price": bid_q.price_cents if bid_q else None,
            "ask_price": (100 - ask_q.price_cents) if ask_q else None,
            "position": 0,
            "tob_bid": bool(bid_q and bb and bid_q.price_cents == bb.price_cents),
            "tob_ask": bool(ask_q and ba and (100 - ask_q.price_cents) == ba.price_cents),
            "state": intent.state.value,
            "pnl_cents": 0,
        }

    async def _vol_cancel_station(self, station_code: str) -> None:
        await self.placer.cancel_all_for_station(station_code, self.markets_by_station())

    async def _vol_requote_station(self, station_code: str) -> None:
        for t, m in self.markets.items():
            if m.station_code == station_code:
                await self._maybe_quote(t)

    async def vol_state_publisher(self) -> None:
        while True:
            self.state.vol_windows = [
                {
                    "name": w.name,
                    "station_code": w.station_code,
                    "start_ts": w.start_ts.isoformat(),
                    "end_ts": w.end_ts.isoformat(),
                    "severity": w.severity,
                    "cancelled": w.cancelled,
                    "requoted": w.requoted,
                }
                for w in self.vol.upcoming(20)
            ]
            await asyncio.sleep(2)

    async def market_refresher(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                await self._refresh_markets()
            except Exception:
                log.exception("market refresh failed")

    async def start(self) -> None:
        await self.boot()
        self._tasks.append(asyncio.create_task(self.theo_engine.run(self._markets_provider)))
        self._tasks.append(asyncio.create_task(self.ws.run()))
        self._tasks.append(asyncio.create_task(self.vol.run()))
        self._tasks.append(asyncio.create_task(self.rewards.run(self._tickers_provider)))
        self._tasks.append(asyncio.create_task(self.market_refresher()))
        self._tasks.append(asyncio.create_task(self.vol_state_publisher()))

    async def _markets_provider(self) -> list[Market]:
        return list(self.markets.values())

    async def _tickers_provider(self) -> list[str]:
        return list(self.markets.keys())

    async def shutdown(self) -> None:
        log.info("Shutting down")
        await self.theo_engine.stop()
        await self.ws.stop()
        await self.vol.stop()
        await self.rewards.stop()
        await self.placer.cancel_all()
        for t in self._tasks:
            t.cancel()
        await self.rest.aclose()


async def amain(paper: bool) -> None:
    settings = load_settings()
    if paper:
        settings.mode = "paper"
    orch = Orchestrator(settings, paper=(settings.mode == "paper"))
    await orch.start()
    controls = Controls(orch)
    app = make_app(orch.state, orch.bus, controls)
    cfg = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
        lifespan="on",
        loop="asyncio",
    )
    server = uvicorn.Server(cfg)
    try:
        await server.serve()
    finally:
        await orch.shutdown()


def cli() -> None:
    ap = argparse.ArgumentParser("kweather")
    ap.add_argument("--paper", action="store_true", help="Force paper mode (no live orders)")
    ap.add_argument("--live", action="store_true", help="Force live mode (will hit the exchange)")
    args = ap.parse_args()
    paper = args.paper or not args.live
    try:
        asyncio.run(amain(paper=paper))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    cli()
