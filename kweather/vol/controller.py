"""Vol controller — pulls quotes ahead of vol windows and re-quotes after.

Loop:
    every 0.5 s:
        for window in upcoming_windows[:5]:
            if now >= window.start_ts - cancel_lead and not window.cancelled:
                cancel_all_quotes_at(window.station)
                window.cancelled = true
            if now >= window.end_ts + requote_delay and window.cancelled and not window.requoted:
                await theo_engine.wait_for_refresh_after(window.end_ts)
                requote_all(window.station)
                window.requoted = true

Per the spec, end-to-end requote latency target is ≤ 60 s post-event.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from kweather.config import Settings
from kweather.types import VolWindow
from kweather.vol.schedule import build_windows

log = logging.getLogger(__name__)

UTC = UTC

CancelFn = Callable[[str], Awaitable[None]]
RequoteFn = Callable[[str], Awaitable[None]]
WaitForRefreshFn = Callable[[datetime], Awaitable[None]]


class VolController:
    def __init__(
        self,
        settings: Settings,
        cancel_station_fn: CancelFn,
        requote_station_fn: RequoteFn,
        wait_for_refresh_fn: WaitForRefreshFn | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ):
        self.settings = settings
        self.cancel_station = cancel_station_fn
        self.requote_station = requote_station_fn
        self.wait_for_refresh = wait_for_refresh_fn
        self.clock = clock
        self._stop = asyncio.Event()
        self.windows: list[VolWindow] = []
        self._last_metrics: dict[str, Any] = {}
        self._last_schedule_refresh_ts: datetime | None = None
        self.schedule_refresh_seconds: int = 30 * 60

    def upcoming(self, n: int = 5) -> list[VolWindow]:
        now = self.clock()
        return [w for w in self.windows if w.end_ts >= now][:n]

    def refresh_windows(self) -> None:
        self.windows = build_windows(self.settings, now=self.clock())
        self._last_schedule_refresh_ts = self.clock()

    async def stop(self) -> None:
        self._stop.set()

    async def step(self) -> None:
        now = self.clock()
        # Refresh the schedule on a clock-based timer; never overwrite an
        # explicitly set self.windows that the caller supplied for testing.
        if self._last_schedule_refresh_ts is None:
            # First step: only auto-build if windows hasn't been seeded.
            if not self.windows:
                self.refresh_windows()
            else:
                self._last_schedule_refresh_ts = now
        elif (now - self._last_schedule_refresh_ts).total_seconds() >= self.schedule_refresh_seconds:
            self.refresh_windows()
        for w in self.windows[:5]:
            cancel_at = w.start_ts - timedelta(seconds=w.cancel_lead_seconds)
            requote_at = w.end_ts + timedelta(seconds=w.requote_delay_seconds)
            if not w.cancelled and now >= cancel_at:
                t0 = time.perf_counter()
                try:
                    await self.cancel_station(w.station_code)
                finally:
                    self._last_metrics[f"cancel_{w.name}"] = time.perf_counter() - t0
                w.cancelled = True
            if w.cancelled and not w.requoted and now >= requote_at:
                if self.wait_for_refresh is not None:
                    try:
                        await asyncio.wait_for(self.wait_for_refresh(w.end_ts), timeout=10)
                    except TimeoutError:
                        pass
                t0 = time.perf_counter()
                try:
                    await self.requote_station(w.station_code)
                finally:
                    self._last_metrics[f"requote_{w.name}"] = time.perf_counter() - t0
                w.requoted = True

    async def run(self, interval_s: float = 0.5) -> None:
        log.info("VolController starting")
        while not self._stop.is_set():
            try:
                await self.step()
            except Exception:
                log.exception("VolController step failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_s)
            except TimeoutError:
                pass
