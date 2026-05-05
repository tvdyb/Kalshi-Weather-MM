"""L2 orderbook maintenance per market_ticker, fed by REST snapshot + WS deltas."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from kweather.types import L2Book, L2Level

log = logging.getLogger(__name__)


class OrderBookStore:
    def __init__(self) -> None:
        self.books: dict[str, L2Book] = {}
        self._yes_bids: dict[str, dict[int, int]] = defaultdict(dict)
        self._yes_asks: dict[str, dict[int, int]] = defaultdict(dict)

    def reset(self, ticker: str, snapshot: dict[str, Any]) -> None:
        """Replace internal state with REST snapshot result.

        Kalshi returns yes_bids and yes_asks as arrays of [price, qty] tuples or
        objects {price, count}. We coerce to int cents and int qty.
        """
        bids: dict[int, int] = {}
        asks: dict[int, int] = {}
        for row in snapshot.get("yes_bids", []) or []:
            p, q = self._coerce_level(row)
            if q > 0:
                bids[p] = q
        for row in snapshot.get("yes_asks", []) or []:
            p, q = self._coerce_level(row)
            if q > 0:
                asks[p] = q
        self._yes_bids[ticker] = bids
        self._yes_asks[ticker] = asks
        self._publish(ticker, seq=snapshot.get("seq", 0))

    @staticmethod
    def _coerce_level(row: Any) -> tuple[int, int]:
        if isinstance(row, dict):
            p = int(row.get("price", row.get("yes_price", 0)))
            q = int(row.get("count", row.get("qty", 0)))
        else:
            p, q = int(row[0]), int(row[1])
        return p, q

    def apply_delta(self, ticker: str, delta: dict[str, Any]) -> None:
        """Apply a single Kalshi orderbook_delta message.

        Delta shape (canonical):
            {"market_ticker": "...", "side": "yes_bid"|"yes_ask",
             "price": 47, "delta": -10, "seq": 12345}
        """
        side = delta.get("side", "")
        price = int(delta.get("price", 0))
        d = int(delta.get("delta", 0))
        if "bid" in side:
            book = self._yes_bids[ticker]
        elif "ask" in side:
            book = self._yes_asks[ticker]
        else:
            return
        new_qty = book.get(price, 0) + d
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty
        self._publish(ticker, seq=int(delta.get("seq", 0)))

    def _publish(self, ticker: str, seq: int) -> None:
        bids = sorted(self._yes_bids[ticker].items(), key=lambda x: -x[0])
        asks = sorted(self._yes_asks[ticker].items(), key=lambda x: x[0])
        self.books[ticker] = L2Book(
            market_ticker=ticker,
            yes_bids=[L2Level(price_cents=p, qty=q) for p, q in bids],
            yes_asks=[L2Level(price_cents=p, qty=q) for p, q in asks],
            seq=seq,
        )

    def get(self, ticker: str) -> L2Book | None:
        return self.books.get(ticker)
