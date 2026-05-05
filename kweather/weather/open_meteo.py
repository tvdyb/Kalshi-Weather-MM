"""Open-Meteo client: multi-model ensemble daily Tmax/Tmin and ECMWF hourly features.

We fetch the public, no-key Open-Meteo endpoints. For each station we keep a parquet
cache of (station, model, target_date, tmax_c, tmin_c) and refresh every 15 minutes.

Models polled: ecmwf_ifs025, gfs_seamless, icon_seamless, gem_seamless, jma_seamless.
The historical-forecast endpoint is used for residual computation against past forecasts;
the archive endpoint provides realized daily Tmax/Tmin for past dates. Hourly variables
(cloudcover, dewpoint_2m, windspeed_10m, surface_pressure) come from the ECMWF model.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from kweather.config import Station
from kweather.weather.cache import ParquetCache

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HIST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

ENSEMBLE_MODELS = [
    "ecmwf_ifs025",
    "gfs_seamless",
    "icon_seamless",
    "gem_seamless",
    "jma_seamless",
]


def _c_to_f(c: float | None) -> float | None:
    if c is None:
        return None
    return c * 9.0 / 5.0 + 32.0


class OpenMeteoClient:
    def __init__(self, cache_root: Path, timeout: float = 20.0):
        self.cache = ParquetCache(cache_root / "openmeteo")
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "kweather/0.1 (localhost mm)"}
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def target_date(station: Station, day_offset: int) -> str:
        tz = ZoneInfo(station.timezone)
        d = datetime.now(tz=tz) + timedelta(days=day_offset)
        return d.date().isoformat()

    async def fetch_station_forecast(
        self, station: Station, target_date: str
    ) -> dict[str, list[float]]:
        """Returns {"high": [member1_F, ...], "low": [member1_F, ...]} for the target date."""
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "models": ",".join(ENSEMBLE_MODELS),
            "timezone": station.timezone,
            "start_date": target_date,
            "end_date": target_date,
            "temperature_unit": "fahrenheit",
        }
        try:
            r = await self._client.get(FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.warning("Open-Meteo fetch failed for %s %s: %s", station.code, target_date, e)
            return {"high": [], "low": []}

        highs: list[float] = []
        lows: list[float] = []
        # When multiple models are requested, Open-Meteo returns suffixed columns:
        # temperature_2m_max_<model_id>. The single-model schema (no suffix) is also possible
        # if only one model is asked for, so we handle both.
        daily = data.get("daily", {})
        for m in ENSEMBLE_MODELS:
            for prefix, bucket in (("temperature_2m_max", highs), ("temperature_2m_min", lows)):
                key = f"{prefix}_{m}"
                vals = daily.get(key)
                if vals:
                    v = vals[0]
                    if v is not None:
                        bucket.append(float(v))
        # Fallback to non-suffixed keys if needed
        if not highs and "temperature_2m_max" in daily:
            v = daily["temperature_2m_max"][0]
            if v is not None:
                highs.append(float(v))
        if not lows and "temperature_2m_min" in daily:
            v = daily["temperature_2m_min"][0]
            if v is not None:
                lows.append(float(v))

        # Persist a snapshot.
        df = pd.DataFrame({"target_date": [target_date], "high_members": [highs], "low_members": [lows]})
        self.cache.write(station.code, f"forecast_{target_date}", df)
        return {"high": highs, "low": lows}

    async def fetch_member_means(
        self, station: Station, target_date: str
    ) -> dict[str, list[float]]:
        cached = self.cache.read(station.code, f"forecast_{target_date}")
        if cached is not None and not cached.empty:
            row = cached.iloc[0]
            return {"high": list(row["high_members"]), "low": list(row["low_members"])}
        return await self.fetch_station_forecast(station, target_date)

    async def fetch_observed(self, station: Station, target_date: str) -> dict[str, float | None]:
        """Realized daily Tmax/Tmin from the ERA5/archive endpoint."""
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": station.timezone,
            "start_date": target_date,
            "end_date": target_date,
            "temperature_unit": "fahrenheit",
        }
        try:
            r = await self._client.get(ARCHIVE_URL, params=params)
            r.raise_for_status()
            d = r.json().get("daily", {})
            tmax = d.get("temperature_2m_max", [None])[0]
            tmin = d.get("temperature_2m_min", [None])[0]
            return {"tmax_f": tmax, "tmin_f": tmin}
        except Exception as e:
            log.debug("Archive fetch failed for %s %s: %s", station.code, target_date, e)
            return {"tmax_f": None, "tmin_f": None}

    async def fetch_hourly_features(
        self, station: Station, target_date: str
    ) -> pd.DataFrame:
        """ECMWF hourly cloudcover/dewpoint/windspeed/pressure."""
        params = {
            "latitude": station.lat,
            "longitude": station.lon,
            "hourly": "cloudcover,dewpoint_2m,windspeed_10m,winddirection_10m,surface_pressure",
            "models": "ecmwf_ifs025",
            "timezone": station.timezone,
            "start_date": target_date,
            "end_date": target_date,
            "temperature_unit": "fahrenheit",
        }
        try:
            r = await self._client.get(FORECAST_URL, params=params)
            r.raise_for_status()
            d = r.json().get("hourly", {})
            return pd.DataFrame(d)
        except Exception:
            return pd.DataFrame()

    async def refresh_all_stations(self, stations: list[Station]) -> None:
        await asyncio.gather(*(self._refresh_station(s) for s in stations), return_exceptions=True)

    async def _refresh_station(self, station: Station) -> None:
        for offset in range(0, 6):
            d = self.target_date(station, offset)
            await self.fetch_station_forecast(station, d)
