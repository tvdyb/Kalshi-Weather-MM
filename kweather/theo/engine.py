"""End-to-end Theo engine. Pulls forecast features, applies EMOS + persistence + sigma
shrinkage, integrates the Gaussian over the bracket, and emits Theo messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from kweather.config import Settings, Station
from kweather.theo.bracket import bracket_probability, fair_price_cents
from kweather.theo.emos import EmosCoeffs, predict
from kweather.theo.persistence import apply_lag1
from kweather.theo.sigma_shrink import shrink
from kweather.types import Market, Theo
from kweather.weather.open_meteo import OpenMeteoClient

log = logging.getLogger(__name__)

ListenerFn = Callable[[Theo], Awaitable[None]]


@dataclass
class StationForecast:
    station: str
    target_date: str
    member_means_high: list[float]   # one per ensemble member
    member_means_low: list[float]
    yesterday_residual_high: float | None
    yesterday_residual_low: float | None
    fetched_ts: datetime


class TheoEngine:
    REFRESH_INTERVAL_SECONDS = 15 * 60

    def __init__(
        self,
        settings: Settings,
        listeners: list[ListenerFn] | None = None,
    ) -> None:
        self.settings = settings
        self.listeners = listeners or []
        self.weather = OpenMeteoClient(settings.cache_dir)
        self.coeffs: dict[tuple[str, str], EmosCoeffs] = {}
        self._forecast_cache: dict[tuple[str, str], StationForecast] = {}
        self._last_full_refresh: float = 0.0
        self._stop = asyncio.Event()
        self._refresh_event = asyncio.Event()
        self._coeffs_path = settings.cache_dir / "emos_coeffs.json"
        self._load_coeffs()

    def add_listener(self, fn: ListenerFn) -> None:
        self.listeners.append(fn)

    def _load_coeffs(self) -> None:
        if not self._coeffs_path.exists():
            return
        try:
            data = json.loads(self._coeffs_path.read_text())
            for k, v in data.items():
                station, target = k.split(":")
                self.coeffs[(station, target)] = EmosCoeffs.from_dict(v)
            log.info("Loaded EMOS coeffs for %d (station,target) pairs", len(self.coeffs))
        except Exception:
            log.exception("Failed to load EMOS coeffs; using defaults")

    def save_coeffs(self) -> None:
        self.settings.cache_dir.mkdir(parents=True, exist_ok=True)
        data = {f"{s}:{t}": c.to_dict() for (s, t), c in self.coeffs.items()}
        self._coeffs_path.write_text(json.dumps(data, indent=2))

    def get_coeffs(self, station: str, target: str) -> EmosCoeffs:
        return self.coeffs.get((station, target), EmosCoeffs(b=[1.0]))

    async def refresh_all(self) -> None:
        t0 = time.perf_counter()
        await self.weather.refresh_all_stations(self.settings.stations)
        for s in self.settings.stations:
            for offset in range(0, 6):  # today + next 5 days
                await self._refresh_station_forecast(s, offset)
        self._last_full_refresh = time.time()
        self._refresh_event.set()
        self._refresh_event.clear()
        log.info("Forecast refresh complete in %.2fs", time.perf_counter() - t0)

    async def _refresh_station_forecast(self, s: Station, day_offset: int) -> None:
        target_date = self.weather.target_date(s, day_offset)
        forecast = await self.weather.fetch_station_forecast(s, target_date)
        # Yesterday's residual: prior-day observation minus prior-day mu.
        yest_date = self.weather.target_date(s, day_offset - 1)
        try:
            yobs = await self.weather.fetch_observed(s, yest_date)
            ymean = await self.weather.fetch_member_means(s, yest_date)
            res_high = (
                yobs.get("tmax_f")
                - (
                    sum(ymean["high"]) / len(ymean["high"])
                    if ymean["high"]
                    else yobs.get("tmax_f")
                )
                if yobs.get("tmax_f") is not None
                else None
            )
            res_low = (
                yobs.get("tmin_f")
                - (
                    sum(ymean["low"]) / len(ymean["low"])
                    if ymean["low"]
                    else yobs.get("tmin_f")
                )
                if yobs.get("tmin_f") is not None
                else None
            )
        except Exception:
            res_high = None
            res_low = None

        self._forecast_cache[(s.code, target_date)] = StationForecast(
            station=s.code,
            target_date=target_date,
            member_means_high=forecast["high"],
            member_means_low=forecast["low"],
            yesterday_residual_high=res_high,
            yesterday_residual_low=res_low,
            fetched_ts=datetime.now(tz=UTC),
        )

    async def compute_theo(self, market: Market) -> Theo | None:
        key = (market.station_code, market.target_date)
        sf = self._forecast_cache.get(key)
        if sf is None:
            station = next(
                (s for s in self.settings.stations if s.code == market.station_code), None
            )
            if station is None:
                return None
            await self._refresh_station_forecast(station, 0)
            sf = self._forecast_cache.get(key)
            if sf is None:
                return None

        if market.target == "high":
            members = np.array(sf.member_means_high, dtype=float).reshape(-1, 1)
            yres = sf.yesterday_residual_high
        else:
            members = np.array(sf.member_means_low, dtype=float).reshape(-1, 1)
            yres = sf.yesterday_residual_low

        if members.size == 0:
            return None

        coeffs = self.get_coeffs(market.station_code, market.target)
        # Defensive: ensure coeffs.b matches the member count we have today.
        if len(coeffs.b) != members.shape[0]:
            coeffs = EmosCoeffs(
                a=coeffs.a,
                b=[1.0 / members.shape[0]] * members.shape[0],
                c=coeffs.c,
                d=coeffs.d,
            )
        mu_arr, sigma_arr = predict(coeffs, members)
        mu = float(mu_arr[0])
        sigma = float(sigma_arr[0])
        mu = apply_lag1(mu, yres, market.target)
        sigma = shrink(sigma, market.target)

        prob = bracket_probability(market.bracket, mu, sigma)
        return Theo(
            market_ticker=market.ticker,
            station_code=market.station_code,
            target_date=market.target_date,
            bracket=market.bracket,
            fair_price_cents=fair_price_cents(prob),
            fair_prob=prob,
            mu_f=mu,
            sigma_f=sigma,
        )

    async def emit(self, theo: Theo) -> None:
        for fn in self.listeners:
            try:
                await fn(theo)
            except Exception:
                log.exception("Theo listener failed")

    async def compute_and_emit(self, markets: list[Market]) -> None:
        for m in markets:
            t = await self.compute_theo(m)
            if t is not None:
                await self.emit(t)

    async def run(self, markets_provider: Callable[[], Awaitable[list[Market]]]) -> None:
        """Background loop: refresh forecast, compute theos, emit."""
        log.info("TheoEngine starting")
        await self.refresh_all()
        while not self._stop.is_set():
            t0 = time.perf_counter()
            try:
                markets = await markets_provider()
                await self.compute_and_emit(markets)
            except Exception:
                log.exception("Theo cycle failed")
            elapsed = time.perf_counter() - t0
            sleep_for = max(1.0, self.REFRESH_INTERVAL_SECONDS - elapsed)
            if time.time() - self._last_full_refresh > self.REFRESH_INTERVAL_SECONDS:
                try:
                    await self.refresh_all()
                except Exception:
                    log.exception("Forecast refresh failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except TimeoutError:
                pass

    async def wait_for_refresh_after(self, ts: datetime) -> None:
        deadline = time.time() + 60
        while time.time() < deadline:
            await asyncio.wait_for(self._refresh_event.wait(), timeout=2)
            if self._last_full_refresh >= ts.timestamp():
                return

    async def stop(self) -> None:
        self._stop.set()


# Backward-compatible name expected by the spec
def latency_assert(fn_name: str, elapsed_ms: float, budget_ms: float) -> None:
    assert elapsed_ms < budget_ms, f"{fn_name} took {elapsed_ms:.1f}ms (budget {budget_ms}ms)"
