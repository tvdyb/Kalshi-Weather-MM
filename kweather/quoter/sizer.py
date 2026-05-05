"""Position sizing utilities."""
from __future__ import annotations

from kweather.config import Risk
from kweather.quoter.decision import kelly_qty
from kweather.types import Theo


def size_quote(
    theo: Theo,
    side: str,
    quote_price_cents: int,
    risk: Risk,
    notional_cap_usd: float,
) -> int:
    return kelly_qty(theo.fair_prob, quote_price_cents, side, risk, notional_cap_usd)
