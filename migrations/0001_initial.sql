CREATE TABLE IF NOT EXISTS holdings (
  symbol TEXT PRIMARY KEY,
  shares REAL NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  kind TEXT NOT NULL,
  signal_date TEXT NOT NULL,
  macd REAL NOT NULL,
  signal REAL NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(symbol, kind, signal_date)
);

CREATE TABLE IF NOT EXISTS universe_cache (
  group_name TEXT PRIMARY KEY,
  symbols TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_scan_results (
  group_name TEXT NOT NULL,
  as_of TEXT NOT NULL,
  payload TEXT NOT NULL,
  PRIMARY KEY(group_name, as_of)
);
