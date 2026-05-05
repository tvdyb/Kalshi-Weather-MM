"""Realised-vol monitor from orderbook trade prints."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta


class RealizedVolMonitor:
    """Tracks last-N print prices per market and exposes a simple stdev measure."""

    def __init__(self, window_minutes: int = 30, max_prints: int = 200):
        self._prints: dict[str, deque[tuple[datetime, int]]] = defaultdict(deque)
        self.window = timedelta(minutes=window_minutes)
        self.max_prints = max_prints

    def record(self, ticker: str, price_cents: int, ts: datetime | None = None) -> None:
        ts = ts or datetime.now(tz=UTC)
        dq = self._prints[ticker]
        dq.append((ts, price_cents))
        cutoff = ts - self.window
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        while len(dq) > self.max_prints:
            dq.popleft()

    def stdev_cents(self, ticker: str) -> float:
        dq = self._prints.get(ticker)
        if not dq or len(dq) < 5:
            return 0.0
        prices = [p for _, p in dq]
        mean = sum(prices) / len(prices)
        var = sum((p - mean) ** 2 for p in prices) / (len(prices) - 1)
        return var**0.5
