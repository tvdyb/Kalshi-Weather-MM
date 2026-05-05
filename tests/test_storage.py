"""Storage layer tests."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kweather.storage.db import Store


@pytest.mark.asyncio
async def test_init_and_insert_theo(tmp_db):
    store = Store(tmp_db)
    await store.init()
    await store.insert_theo(
        {
            "market_ticker": "X",
            "fair_price_cents": 50,
            "fair_prob": 0.5,
            "mu_f": 70.0,
            "sigma_f": 2.0,
            "ts": datetime.now(tz=UTC).isoformat(),
        }
    )
    rows = await store.fetch_recent_theos("X")
    assert len(rows) == 1
    assert rows[0]["fair_price_cents"] == 50


@pytest.mark.asyncio
async def test_reward_capture_aggregation(tmp_db):
    store = Store(tmp_db)
    await store.init()
    base = datetime.now(tz=UTC).replace(second=0, microsecond=0)
    for i in range(10):
        await store.insert_reward(
            {
                "market_ticker": "X",
                "side": "yes",
                "minute_ts": base.replace(minute=i % 60).isoformat(),
                "eligible": 1 if i < 7 else 0,
                "our_qty_in_top_300": 50 if i < 7 else 0,
                "total_qty_at_best": 200,
                "our_qty_total_at_best": 50,
            }
        )
    cap = await store.fetch_reward_capture("X")
    assert "yes" in cap
    assert 0.6 < cap["yes"] < 0.8
