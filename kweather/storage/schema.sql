PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA temp_store=MEMORY;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS markets (
  ticker TEXT PRIMARY KEY,
  event_ticker TEXT NOT NULL,
  station_code TEXT NOT NULL,
  target TEXT NOT NULL,
  target_date TEXT NOT NULL,
  bracket_lo REAL,
  bracket_hi REAL,
  bracket_label TEXT NOT NULL,
  open_ts TEXT NOT NULL,
  close_ts TEXT NOT NULL,
  inserted_ts TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_markets_station_date ON markets(station_code, target_date);

CREATE TABLE IF NOT EXISTS theos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_ticker TEXT NOT NULL,
  fair_price_cents INTEGER NOT NULL,
  fair_prob REAL NOT NULL,
  mu_f REAL NOT NULL,
  sigma_f REAL NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_theos_ticker_ts ON theos(market_ticker, ts);

CREATE TABLE IF NOT EXISTS orders (
  client_order_id TEXT PRIMARY KEY,
  exchange_order_id TEXT,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  action TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  status TEXT NOT NULL,
  placed_ts TEXT NOT NULL,
  closed_ts TEXT,
  filled_qty INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_orders_ticker_status ON orders(market_ticker, status);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  exchange_fill_id TEXT,
  client_order_id TEXT,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  action TEXT NOT NULL,
  price_cents INTEGER NOT NULL,
  qty INTEGER NOT NULL,
  fee_cents REAL NOT NULL,
  ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fills_ticker_ts ON fills(market_ticker, ts);

CREATE TABLE IF NOT EXISTS rewards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_ticker TEXT NOT NULL,
  side TEXT NOT NULL,
  minute_ts TEXT NOT NULL,
  eligible INTEGER NOT NULL,
  our_qty_in_top_300 INTEGER NOT NULL,
  total_qty_at_best INTEGER NOT NULL,
  our_qty_total_at_best INTEGER NOT NULL,
  UNIQUE(market_ticker, side, minute_ts)
);
CREATE INDEX IF NOT EXISTS idx_rewards_ticker_ts ON rewards(market_ticker, minute_ts);

CREATE TABLE IF NOT EXISTS book_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  market_ticker TEXT NOT NULL,
  ts TEXT NOT NULL,
  best_bid INTEGER,
  best_ask INTEGER,
  bid_qty INTEGER,
  ask_qty INTEGER
);
CREATE INDEX IF NOT EXISTS idx_book_ticker_ts ON book_snapshots(market_ticker, ts);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL
);
