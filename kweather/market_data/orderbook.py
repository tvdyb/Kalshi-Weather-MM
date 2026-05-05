"""L2 orderbook maintenance per market_ticker, fed by REST snapshot + WS deltas.

Kalshi schema (discovered live 2026-05-05):
    REST GET /markets/{ticker}/orderbook returns
        {"orderbook_fp": {"yes_dollars": [[price_$_str, qty_str], ...],
                          "no_dollars":  [[price_$_str, qty_str], ...]}}
    WS orderbook_snapshot.msg has the same arrays as `yes_dollars_fp` /
    `no_dollars_fp` at the top level.
    WS orderbook_delta.msg = {"price_dollars": str, "delta_fp": str,
                              "side": "yes"|"no", "seq": int}.

Conversion: $-prices→cents. The NO side is mirrored into our internal yes_asks
book at (100 - p_cents): a NO bid at $0.79 corresponds to a YES ask at 21¢.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from kweather.types import L2Book, L2Level

log = logging.getLogger(__name__)


def _to_cents(price_str: Any) -> int:
    return round(float(price_str) * 100)


def _to_qty(q: Any) -> int:
    return int(round(float(q)))


class OrderBookStore:
    def __init__(self) -> None:
        self.books: dict[str, L2Book] = {}
        self._yes_bids: dict[str, dict[int, int]] = defaultdict(dict)
        self._yes_asks: dict[str, dict[int, int]] = defaultdict(dict)

    def reset(self, ticker: str, snapshot: dict[str, Any]) -> None:
        ob = snapshot.get("orderbook_fp") or snapshot.get("orderbook") or snapshot
        yes = ob.get("yes_dollars_fp") or ob.get("yes_dollars") or ob.get("yes") or ob.get("yes_bids") or []
        no = ob.get("no_dollars_fp") or ob.get("no_dollars") or ob.get("no") or []
        explicit_asks = ob.get("yes_asks")
        bids: dict[int, int] = {}
        asks: dict[int, int] = {}
        for row in yes:
            p, q = self._coerce_level(row)
            if q > 0 and 1 <= p <= 99:
                bids[p] = bids.get(p, 0) + q
        for row in no:
            p, q = self._coerce_level(row)
            if q > 0 and 1 <= p <= 99:
                ask_price = 100 - p
                asks[ask_price] = asks.get(ask_price, 0) + q
        if explicit_asks:
            for row in explicit_asks:
                p, q = self._coerce_level(row)
                if q > 0 and 1 <= p <= 99:
                    asks[p] = asks.get(p, 0) + q
        self._yes_bids[ticker] = bids
        self._yes_asks[ticker] = asks
        self._publish(ticker, seq=int(snapshot.get("seq", 0) or 0))

    @staticmethod
    def _coerce_level(row: Any) -> tuple[int, int]:
        if isinstance(row, dict):
            raw_p = row.get("price_dollars", row.get("price", row.get("yes_price", 0)))
            raw_q = row.get("delta_fp", row.get("count", row.get("qty", 0)))
        else:
            raw_p, raw_q = row[0], row[1]
        p = _to_cents(raw_p) if isinstance(raw_p, str) and "." in raw_p else int(float(raw_p))
        return p, _to_qty(raw_q)

    def apply_delta(self, ticker: str, delta: dict[str, Any]) -> None:
        raw_p = delta.get("price_dollars", delta.get("price"))
        raw_d = delta.get("delta_fp", delta.get("delta"))
        side = (delta.get("side") or "").lower()
        if raw_p is None or raw_d is None:
            return
        if isinstance(raw_p, str) and "." in raw_p:
            price = _to_cents(raw_p)
        else:
            price = int(float(raw_p))
        d = _to_qty(raw_d)
        if not (1 <= price <= 99):
            return
        if side in ("yes", "yes_bid"):
            book = self._yes_bids[ticker]
            key = price
        elif side == "yes_ask":
            book = self._yes_asks[ticker]
            key = price
        elif side == "no":
            book = self._yes_asks[ticker]
            key = 100 - price
        else:
            return
        new_qty = book.get(key, 0) + d
        if new_qty <= 0:
            book.pop(key, None)
        else:
            book[key] = new_qty
        self._publish(ticker, seq=int(delta.get("seq", 0) or 0))

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
