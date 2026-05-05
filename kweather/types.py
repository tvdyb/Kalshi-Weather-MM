"""Pydantic models for messages, orders, theos, etc."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Side(str, Enum):
    YES = "yes"
    NO = "no"


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"


class QuoteState(str, Enum):
    TWO_SIDED = "two_sided"
    BID_ONLY = "bid_only"
    ASK_ONLY = "ask_only"
    FLAT = "flat"
    WITHDRAWN_VOL = "withdrawn_vol"
    WIDE_DISAGREEMENT = "wide_disagreement"


class Bracket(BaseModel):
    """A single Kalshi temperature bracket — open lower bound, closed upper, or open-ended."""
    lo: float | None = None   # inclusive lower bound (F); None = open below
    hi: float | None = None   # exclusive upper bound (F); None = open above
    label: str                   # bracket label e.g. "73-T-77" or "T76"

    def contains(self, x: float) -> bool:
        lo_ok = self.lo is None or x >= self.lo
        hi_ok = self.hi is None or x < self.hi
        return lo_ok and hi_ok


class Market(BaseModel):
    ticker: str
    event_ticker: str
    station_code: str
    target: Literal["high", "low"]
    target_date: str             # ISO yyyy-mm-dd
    bracket: Bracket
    open_ts: datetime
    close_ts: datetime
    last_price: int | None = None
    yes_bid: int | None = None
    yes_ask: int | None = None


class Theo(BaseModel):
    market_ticker: str
    station_code: str
    target_date: str
    bracket: Bracket
    fair_price_cents: int        # 1..99
    fair_prob: float             # 0..1
    mu_f: float                  # forecast mean (deg F)
    sigma_f: float               # forecast std (deg F) post-shrinkage
    ts: datetime = Field(default_factory=utcnow)


class L2Level(BaseModel):
    price_cents: int
    qty: int


class L2Book(BaseModel):
    market_ticker: str
    yes_bids: list[L2Level] = []
    yes_asks: list[L2Level] = []
    seq: int = 0
    ts: datetime = Field(default_factory=utcnow)

    def best_bid(self) -> L2Level | None:
        return self.yes_bids[0] if self.yes_bids else None

    def best_ask(self) -> L2Level | None:
        return self.yes_asks[0] if self.yes_asks else None

    def mid_cents(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb.price_cents + ba.price_cents) / 2


class Quote(BaseModel):
    market_ticker: str
    side: Side
    action: Action
    price_cents: int
    qty: int
    client_order_id: str
    placed_ts: datetime = Field(default_factory=utcnow)
    exchange_order_id: str | None = None


class Fill(BaseModel):
    market_ticker: str
    side: Side
    action: Action
    price_cents: int
    qty: int
    fee_cents: float
    ts: datetime = Field(default_factory=utcnow)
    exchange_fill_id: str | None = None
    client_order_id: str | None = None


class Position(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    market_ticker: str
    qty: int = 0                 # signed; +long YES, -long NO
    avg_price_cents: float = 0.0
    realized_pnl_cents: float = 0.0


class RewardSnapshot(BaseModel):
    market_ticker: str
    side: Side
    minute_ts: datetime
    eligible: bool
    our_qty_in_top_300: int
    total_qty_at_best: int
    our_qty_total_at_best: int


class VolWindow(BaseModel):
    name: str
    station_code: str
    start_ts: datetime
    end_ts: datetime
    cancel_lead_seconds: int
    requote_delay_seconds: int
    severity: str = "normal"
    cancelled: bool = False
    requoted: bool = False
