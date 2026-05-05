"""Quoter decision logic: turn (theo, mid, spread) into a quote action."""
from __future__ import annotations

import math
from dataclasses import dataclass

from kweather.config import Risk
from kweather.types import L2Book, QuoteState, Theo


@dataclass
class QuoteIntent:
    state: QuoteState
    bid_price_cents: int | None
    ask_price_cents: int | None
    bid_qty: int = 0
    ask_qty: int = 0


def _half_spread(theo: Theo, risk: Risk) -> int:
    """Half spread in cents. Floor = base; widen with sigma."""
    base = risk.half_spread_cents
    extra = risk.half_spread_sigma_coef * theo.sigma_f
    return max(1, int(round(base + extra)))


def decide(
    theo: Theo,
    book: L2Book,
    risk: Risk,
    *,
    widen_factor: float = 1.0,
    size_factor: float = 1.0,
    base_qty: int = 100,
) -> QuoteIntent:
    """Decision tree.

    - tight: |theo - mid| < tight_threshold → two-sided around theo
    - lean-bid: theo > mid + tight → bid only
    - lean-ask: theo < mid - tight → ask only
    - stale: |theo - mid| > stale_threshold → flat (wide disagreement)
    """
    bb = book.best_bid()
    ba = book.best_ask()
    if bb is None or ba is None:
        return QuoteIntent(state=QuoteState.FLAT, bid_price_cents=None, ask_price_cents=None)

    mid = (bb.price_cents + ba.price_cents) / 2
    theo_p = theo.fair_price_cents
    diff = theo_p - mid
    half = max(1, int(round(_half_spread(theo, risk) * widen_factor)))
    tight = risk.tight_threshold_cents
    stale = risk.stale_threshold_cents

    qty = max(1, int(round(base_qty * size_factor)))

    if abs(diff) > stale:
        return QuoteIntent(state=QuoteState.WIDE_DISAGREEMENT, bid_price_cents=None, ask_price_cents=None)

    if abs(diff) < tight:
        bid_p = max(1, theo_p - half)
        ask_p = min(99, theo_p + half)
        if bid_p >= ask_p:
            return QuoteIntent(state=QuoteState.FLAT, bid_price_cents=None, ask_price_cents=None)
        return QuoteIntent(
            state=QuoteState.TWO_SIDED,
            bid_price_cents=bid_p,
            ask_price_cents=ask_p,
            bid_qty=qty,
            ask_qty=qty,
        )

    if diff > 0:
        bid_p = max(1, theo_p - half)
        return QuoteIntent(
            state=QuoteState.BID_ONLY,
            bid_price_cents=bid_p,
            ask_price_cents=None,
            bid_qty=qty,
        )
    else:
        ask_p = min(99, theo_p + half)
        return QuoteIntent(
            state=QuoteState.ASK_ONLY,
            bid_price_cents=None,
            ask_price_cents=ask_p,
            ask_qty=qty,
        )


def kelly_qty(
    theo_prob: float,
    quote_price_cents: int,
    side: str,
    risk: Risk,
    notional_cap_usd: float,
) -> int:
    """Fractional Kelly size in contracts.

    Each contract pays $1 at settlement. For a YES bid at p cents:
        win prob   = theo_prob,    win amount  = (100 - p) cents
        loss prob  = 1 - theo_prob, loss amount = p cents
    Kelly f* = (b*pwin - ploss) / b, where b = win/loss ratio.
    """
    p = max(1, min(99, quote_price_cents))
    if side == "yes":
        win = (100 - p) / 100.0
        loss = p / 100.0
        prob_win = theo_prob
    else:
        win = p / 100.0
        loss = (100 - p) / 100.0
        prob_win = 1.0 - theo_prob

    if win <= 0 or loss <= 0 or prob_win <= 0 or prob_win >= 1:
        return 0
    b = win / loss
    f_star = (b * prob_win - (1 - prob_win)) / b
    f_star = max(0.0, f_star) * risk.kelly_fraction
    notional = notional_cap_usd * f_star
    contracts = int(math.floor(notional / max(loss, 0.01)))
    return min(max(contracts, 0), risk.position_cap_contracts)
