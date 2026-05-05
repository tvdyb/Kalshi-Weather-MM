"""Replay a recorded sequence of WS deltas against the OrderBookStore."""
from __future__ import annotations

from kweather.market_data.orderbook import OrderBookStore


def test_snapshot_then_deltas():
    store = OrderBookStore()
    store.reset(
        "T1",
        {
            "yes_bids": [[48, 100], [47, 200]],
            "yes_asks": [[52, 150], [53, 80]],
            "seq": 1,
        },
    )
    book = store.get("T1")
    assert book.best_bid().price_cents == 48
    assert book.best_ask().price_cents == 52

    # New bid at 49 (jumps the spread): +75
    store.apply_delta("T1", {"side": "yes_bid", "price": 49, "delta": 75, "seq": 2})
    assert store.get("T1").best_bid().price_cents == 49
    assert store.get("T1").best_bid().qty == 75

    # Bid at 49 fully cancelled
    store.apply_delta("T1", {"side": "yes_bid", "price": 49, "delta": -75, "seq": 3})
    assert store.get("T1").best_bid().price_cents == 48

    # Best ask reduced
    store.apply_delta("T1", {"side": "yes_ask", "price": 52, "delta": -150, "seq": 4})
    assert store.get("T1").best_ask().price_cents == 53


def test_negative_delta_clears_level():
    store = OrderBookStore()
    store.reset("T2", {"yes_bids": [[40, 100]], "yes_asks": [[60, 100]], "seq": 0})
    store.apply_delta("T2", {"side": "yes_bid", "price": 40, "delta": -200, "seq": 1})
    assert store.get("T2").best_bid() is None
