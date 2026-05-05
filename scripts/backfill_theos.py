"""One-shot: backfill theos for the last 30 days for monitoring/calibration plots."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from kweather.config import load_settings
from kweather.storage.db import Store
from kweather.theo.engine import TheoEngine
from kweather.types import Bracket, Market

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    settings = load_settings()
    store = Store(settings.db_path)
    await store.init()
    engine = TheoEngine(settings)
    await engine.refresh_all()
    today = datetime.now(tz=timezone.utc).date()
    for offset in range(30):
        d = (today - timedelta(days=offset)).isoformat()
        for s in settings.stations:
            for target in ("high", "low"):
                # Use a single-temp synthetic bracket centered on a typical CLI value, just
                # to populate the theos table for monitoring; real backfill should use the
                # actual bracket suite from listed markets.
                synthetic = Bracket(lo=70, hi=71, label="T70")
                m = Market(
                    ticker=f"BACKFILL-{s.code}-{target}-{d}-T70",
                    event_ticker=f"BACKFILL-{s.code}",
                    station_code=s.code,
                    target=target,
                    target_date=d,
                    bracket=synthetic,
                    open_ts=datetime.now(tz=timezone.utc),
                    close_ts=datetime.now(tz=timezone.utc),
                )
                t = await engine.compute_theo(m)
                if t is not None:
                    await store.insert_theo(
                        {
                            "market_ticker": t.market_ticker,
                            "fair_price_cents": t.fair_price_cents,
                            "fair_prob": t.fair_prob,
                            "mu_f": t.mu_f,
                            "sigma_f": t.sigma_f,
                            "ts": t.ts.isoformat(),
                        }
                    )
    print("Backfill complete")


if __name__ == "__main__":
    asyncio.run(main())
