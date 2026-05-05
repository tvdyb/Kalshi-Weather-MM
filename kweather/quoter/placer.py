"""Order placement: post-only quotes, cancel-then-replace on theo move.

Tracks resting per-(market, side) intents and replays cancel+place when the desired
price changes. Honors a per-market cancel debounce (default 250 ms) to avoid
cancel-storm loops.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from kweather.config import Risk, Settings
from kweather.market_data.kalshi_rest import KalshiREST, PlacementResult
from kweather.quoter.decision import QuoteIntent
from kweather.storage.db import Store
from kweather.types import Action, Side

log = logging.getLogger(__name__)


@dataclass
class RestingQuote:
    side: Side
    price_cents: int
    qty: int
    client_order_id: str
    exchange_order_id: str | None
    placed_ts: float


class Placer:
    def __init__(
        self,
        settings: Settings,
        rest: KalshiREST,
        store: Store,
        risk: Risk,
    ):
        self.settings = settings
        self.rest = rest
        self.store = store
        self.risk = risk
        self._resting: dict[tuple[str, Side], RestingQuote] = {}
        self._last_cancel_ts: dict[str, float] = defaultdict(float)
        self._lock: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def resting(self, ticker: str) -> dict[Side, RestingQuote]:
        return {s: q for (t, s), q in self._resting.items() if t == ticker}

    async def apply(self, ticker: str, intent: QuoteIntent) -> None:
        async with self._lock[ticker]:
            await self._reconcile_side(ticker, Side.YES, intent.bid_price_cents, intent.bid_qty, Action.BUY)
            # The 'no' side is implemented in Kalshi as a 'no' contract. We map our 'ask' to a NO buy.
            await self._reconcile_side(ticker, Side.NO, intent.ask_price_cents, intent.ask_qty, Action.BUY)

    async def _reconcile_side(
        self,
        ticker: str,
        side: Side,
        target_price: int | None,
        target_qty: int,
        action: Action,
    ) -> None:
        existing = self._resting.get((ticker, side))
        # Convert ask to NO-side: target_price for ask-of-yes is YES price; convert to NO price.
        if side == Side.NO and target_price is not None:
            target_price = 100 - target_price

        if target_price is None or target_qty <= 0:
            if existing is not None:
                await self._cancel(ticker, side, existing)
            return

        if existing is not None and existing.price_cents == target_price and existing.qty == target_qty:
            return

        if existing is not None:
            await self._cancel(ticker, side, existing)
            now = time.monotonic()
            wait_for = self.risk.cancel_debounce_ms / 1000.0
            elapsed = now - self._last_cancel_ts[ticker]
            if elapsed < wait_for:
                await asyncio.sleep(wait_for - elapsed)

        await self._place(ticker, side, action, target_price, target_qty)

    async def _place(
        self, ticker: str, side: Side, action: Action, price_cents: int, qty: int
    ) -> None:
        try:
            res: PlacementResult = await self.rest.place_order(
                ticker=ticker,
                side=side.value,
                action=action.value,
                price_cents=price_cents,
                qty=qty,
                post_only=True,
                time_in_force="GTC",
            )
        except Exception:
            log.exception("place_order errored ticker=%s side=%s", ticker, side)
            return
        if not res.accepted:
            log.warning("Order rejected ticker=%s side=%s price=%d qty=%d", ticker, side, price_cents, qty)
            return
        rq = RestingQuote(
            side=side,
            price_cents=price_cents,
            qty=qty,
            client_order_id=res.client_order_id,
            exchange_order_id=res.exchange_order_id,
            placed_ts=time.monotonic(),
        )
        self._resting[(ticker, side)] = rq
        await self.store.insert_order(
            {
                "client_order_id": rq.client_order_id,
                "exchange_order_id": rq.exchange_order_id,
                "market_ticker": ticker,
                "side": side.value,
                "action": action.value,
                "price_cents": price_cents,
                "qty": qty,
                "status": "open",
                "placed_ts": datetime.now(tz=UTC).isoformat(),
                "closed_ts": None,
                "filled_qty": 0,
            }
        )

    async def _cancel(self, ticker: str, side: Side, q: RestingQuote) -> None:
        if q.exchange_order_id is None:
            self._resting.pop((ticker, side), None)
            return
        try:
            ok = await self.rest.cancel_order(q.exchange_order_id)
        except Exception:
            log.exception("cancel_order errored")
            return
        self._last_cancel_ts[ticker] = time.monotonic()
        self._resting.pop((ticker, side), None)
        if ok:
            await self.store.update_order_status(
                q.client_order_id, "cancelled", datetime.now(tz=UTC)
            )

    async def cancel_all_for_station(self, station_code: str, markets_by_station: dict[str, list[str]]) -> None:
        tickers = markets_by_station.get(station_code, [])
        await asyncio.gather(
            *(self.cancel_all_for_market(t) for t in tickers), return_exceptions=True
        )

    async def cancel_all_for_market(self, ticker: str) -> None:
        async with self._lock[ticker]:
            for side in (Side.YES, Side.NO):
                rq = self._resting.get((ticker, side))
                if rq is not None:
                    await self._cancel(ticker, side, rq)

    async def cancel_all(self) -> None:
        keys = list(self._resting.keys())
        seen: set[str] = set()
        for ticker, _side in keys:
            if ticker in seen:
                continue
            seen.add(ticker)
            await self.cancel_all_for_market(ticker)


class PaperPlacer(Placer):
    """Drop-in replacement that does not call the exchange. Records to the store only."""

    async def _place(
        self, ticker: str, side: Side, action: Action, price_cents: int, qty: int
    ) -> None:
        coid = f"paper-{uuid.uuid4().hex[:18]}"
        rq = RestingQuote(
            side=side,
            price_cents=price_cents,
            qty=qty,
            client_order_id=coid,
            exchange_order_id=f"paper-{coid}",
            placed_ts=time.monotonic(),
        )
        self._resting[(ticker, side)] = rq
        await self.store.insert_order(
            {
                "client_order_id": rq.client_order_id,
                "exchange_order_id": rq.exchange_order_id,
                "market_ticker": ticker,
                "side": side.value,
                "action": action.value,
                "price_cents": price_cents,
                "qty": qty,
                "status": "open",
                "placed_ts": datetime.now(tz=UTC).isoformat(),
                "closed_ts": None,
                "filled_qty": 0,
            }
        )

    async def _cancel(self, ticker: str, side: Side, q: RestingQuote) -> None:
        self._last_cancel_ts[ticker] = time.monotonic()
        self._resting.pop((ticker, side), None)
        await self.store.update_order_status(
            q.client_order_id, "cancelled", datetime.now(tz=UTC)
        )
