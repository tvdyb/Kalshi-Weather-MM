"""Daily reward % captured per market and aggregated."""
from __future__ import annotations

from datetime import UTC, datetime

from kweather.storage.db import Store


async def daily_capture(store: Store, market_tickers: list[str]) -> dict[str, dict[str, float]]:
    cutoff = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    out: dict[str, dict[str, float]] = {}
    for t in market_tickers:
        out[t] = await store.fetch_reward_capture(t, since_iso=cutoff)
    return out
