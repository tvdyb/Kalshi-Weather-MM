"""Top-300 maker-rewards eligibility tracker.

For each active market, every 60 s:
    - Pull GET /markets/{ticker}/orderbook?depth=300
    - For each side (yes_bid, yes_ask):
        - Identify our resting orders at the best price.
        - Walk the queue at best price; sum cumulative quantity. If our orders sit
          within the first 300 contracts, we are eligible for that side.
    - Persist a per-minute row with eligibility, our queue position, and totals.

The tracker assumes price-time priority within a price level. Without exchange-side
queue position info, we approximate by: orders at the best price level placed before
ours have priority. Since we cannot directly observe other participants' order ages,
we use the conservative assumption that all non-self quantity at the best price came
before us unless our own placed_ts predates the orderbook snapshot — in which case we
assume FIFO and rank by our own placed_ts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from kweather.market_data.kalshi_rest import KalshiREST
from kweather.quoter.placer import Placer
from kweather.storage.db import Store

log = logging.getLogger(__name__)


class RewardsTracker:
    REWARD_DEPTH = 300
    POLL_SECONDS = 60

    def __init__(self, rest: KalshiREST, placer: Placer, store: Store):
        self.rest = rest
        self.placer = placer
        self.store = store
        self._stop = asyncio.Event()
        self.last_status: dict[tuple[str, str], dict[str, Any]] = {}

    async def stop(self) -> None:
        self._stop.set()

    async def run(self, tickers_provider) -> None:
        log.info("RewardsTracker starting")
        while not self._stop.is_set():
            try:
                tickers = await tickers_provider()
                await asyncio.gather(
                    *(self._scan_one(t) for t in tickers), return_exceptions=True
                )
            except Exception:
                log.exception("RewardsTracker iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.POLL_SECONDS)
            except TimeoutError:
                pass

    async def _scan_one(self, ticker: str) -> None:
        try:
            ob = await self.rest.get_orderbook(ticker, depth=self.REWARD_DEPTH)
        except Exception:
            return
        minute_ts = datetime.now(tz=UTC).replace(second=0, microsecond=0).isoformat()
        resting = self.placer.resting(ticker)
        for _side_label, levels_key, side_value in (
            ("yes_bid", "yes_bids", "yes"),
            ("yes_ask", "yes_asks", "no"),
        ):
            levels = ob.get(levels_key) or []
            if not levels:
                continue
            best_price, best_qty = self._coerce(levels[0])
            our_quote = resting.get(side_value)
            our_qty_at_best = (
                our_quote.qty if our_quote is not None and our_quote.price_cents == best_price else 0
            )
            others_at_best = max(0, best_qty - our_qty_at_best)
            our_position_in_queue = others_at_best  # conservative
            eligible = our_qty_at_best > 0 and our_position_in_queue < self.REWARD_DEPTH
            our_qty_in_top_300 = (
                max(0, min(our_qty_at_best, self.REWARD_DEPTH - others_at_best))
                if eligible
                else 0
            )
            row = {
                "market_ticker": ticker,
                "side": side_value,
                "minute_ts": minute_ts,
                "eligible": int(bool(eligible)),
                "our_qty_in_top_300": int(our_qty_in_top_300),
                "total_qty_at_best": int(best_qty),
                "our_qty_total_at_best": int(our_qty_at_best),
            }
            await self.store.insert_reward(row)
            self.last_status[(ticker, side_value)] = row

    @staticmethod
    def _coerce(row: Any) -> tuple[int, int]:
        if isinstance(row, dict):
            p = int(row.get("price", row.get("yes_price", 0)))
            q = int(row.get("count", row.get("qty", 0)))
        else:
            p, q = int(row[0]), int(row[1])
        return p, q
