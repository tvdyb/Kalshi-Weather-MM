"""SQLite + WAL persistence with a thin async wrapper."""
from __future__ import annotations

import json
from collections.abc import Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from kweather.config import load_settings

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.isoformat()


async def init_db(db_path: Path | None = None) -> Path:
    if db_path is None:
        db_path = load_settings().db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    sql = SCHEMA_PATH.read_text()
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(sql)
        await db.commit()
    return db_path


@asynccontextmanager
async def connect(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        yield db


class Store:
    """Lightweight async accessor used by quoter, theo engine, and rewards tracker."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    async def init(self) -> None:
        await init_db(self.db_path)

    async def upsert_market(self, m: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO markets(ticker,event_ticker,station_code,target,target_date,
                       bracket_lo,bracket_hi,bracket_label,open_ts,close_ts)
                   VALUES(:ticker,:event_ticker,:station_code,:target,:target_date,
                          :bracket_lo,:bracket_hi,:bracket_label,:open_ts,:close_ts)
                   ON CONFLICT(ticker) DO UPDATE SET
                       open_ts=excluded.open_ts, close_ts=excluded.close_ts""",
                m,
            )
            await db.commit()

    async def insert_theo(self, t: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO theos(market_ticker,fair_price_cents,fair_prob,mu_f,sigma_f,ts)
                   VALUES(:market_ticker,:fair_price_cents,:fair_prob,:mu_f,:sigma_f,:ts)""",
                t,
            )
            await db.commit()

    async def insert_order(self, o: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO orders(client_order_id,exchange_order_id,market_ticker,side,
                       action,price_cents,qty,status,placed_ts,closed_ts,filled_qty)
                   VALUES(:client_order_id,:exchange_order_id,:market_ticker,:side,:action,
                          :price_cents,:qty,:status,:placed_ts,:closed_ts,:filled_qty)""",
                o,
            )
            await db.commit()

    async def update_order_status(
        self, client_order_id: str, status: str, closed_ts: datetime | None = None
    ) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                "UPDATE orders SET status=?, closed_ts=? WHERE client_order_id=?",
                (status, _iso(closed_ts) if closed_ts else None, client_order_id),
            )
            await db.commit()

    async def insert_fill(self, f: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO fills(exchange_fill_id,client_order_id,market_ticker,side,action,
                       price_cents,qty,fee_cents,ts)
                   VALUES(:exchange_fill_id,:client_order_id,:market_ticker,:side,:action,
                          :price_cents,:qty,:fee_cents,:ts)""",
                f,
            )
            await db.commit()

    async def insert_reward(self, r: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                """INSERT OR IGNORE INTO rewards(market_ticker,side,minute_ts,eligible,
                       our_qty_in_top_300,total_qty_at_best,our_qty_total_at_best)
                   VALUES(:market_ticker,:side,:minute_ts,:eligible,
                          :our_qty_in_top_300,:total_qty_at_best,:our_qty_total_at_best)""",
                r,
            )
            await db.commit()

    async def insert_book_snapshot(self, s: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                """INSERT INTO book_snapshots(market_ticker,ts,best_bid,best_ask,bid_qty,ask_qty)
                   VALUES(:market_ticker,:ts,:best_bid,:best_ask,:bid_qty,:ask_qty)""",
                s,
            )
            await db.commit()

    async def audit(self, kind: str, payload: dict[str, Any]) -> None:
        async with connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO audit_log(ts,kind,payload) VALUES(?,?,?)",
                (datetime.now(tz=UTC).isoformat(), kind, json.dumps(payload, default=str)),
            )
            await db.commit()

    async def fetch_recent_theos(self, market_ticker: str, limit: int = 240) -> list[dict[str, Any]]:
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                """SELECT ts, fair_price_cents FROM theos WHERE market_ticker=?
                   ORDER BY id DESC LIMIT ?""",
                (market_ticker, limit),
            )
            rows = await cur.fetchall()
            return [dict(r) for r in reversed(rows)]

    async def fetch_today_pnl(self, market_ticker: str | None = None) -> float:
        """Best-effort realized P&L sum from fills since UTC midnight (cents)."""
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cutoff = datetime.now(tz=UTC).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            q = "SELECT side, action, price_cents, qty, fee_cents FROM fills WHERE ts >= ?"
            args: list[Any] = [cutoff]
            if market_ticker:
                q += " AND market_ticker=?"
                args.append(market_ticker)
            cur = await db.execute(q, args)
            rows = await cur.fetchall()
        # Cash flow accounting: BUY YES costs price_cents per contract; SELL YES receives 100-price_cents-equivalent (handled per Kalshi convention as price). We treat each fill as a cash event without mark-to-market.
        cash = 0.0
        for r in rows:
            sgn = -1 if r["action"] == "buy" else 1
            cash += sgn * r["price_cents"] * r["qty"] - r["fee_cents"]
        return cash

    async def fetch_reward_capture(
        self, market_ticker: str, since_iso: str | None = None
    ) -> dict[str, float]:
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = """SELECT side, AVG(eligible) AS pct
                   FROM rewards WHERE market_ticker=?"""
            args: list[Any] = [market_ticker]
            if since_iso:
                q += " AND minute_ts >= ?"
                args.append(since_iso)
            q += " GROUP BY side"
            cur = await db.execute(q, args)
            rows = await cur.fetchall()
        return {r["side"]: float(r["pct"] or 0.0) for r in rows}

    async def open_orders(self, market_ticker: str | None = None) -> list[dict[str, Any]]:
        async with connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            q = "SELECT * FROM orders WHERE status IN ('open','partially_filled')"
            args: Iterable[Any] = ()
            if market_ticker:
                q += " AND market_ticker=?"
                args = (market_ticker,)
            cur = await db.execute(q, args)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]
