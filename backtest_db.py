"""
backtest_db.py  —  shared SQLite schema for the Polymarket backtest / resolution system.

Database: backtest.db  (separate from seen.db so monitor.py is unaffected)

Tables
------
  tracked_wallets           wallet registry
  position_snapshots        immutable point-in-time position rows
  resolved_positions        one final P/L record per wallet-position
  wallet_performance_summary  aggregated win-rate / ROI per wallet+category
"""

import sqlite3
from pathlib import Path

BACKTEST_DB = "backtest.db"

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Tracked wallets ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracked_wallets (
    wallet_address  TEXT PRIMARY KEY,
    wallet_label    TEXT NOT NULL,
    category_tags   TEXT    DEFAULT '',       -- comma-separated: "NBA,WNBA"
    source          TEXT    DEFAULT 'env',    -- "env" | "sharp_wallets_selected"
    is_active       INTEGER NOT NULL DEFAULT 1,
    added_at        TEXT    NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notes           TEXT    DEFAULT ''
);

-- ── Immutable position snapshots ─────────────────────────────────────────────
-- One row per (run, wallet, token).  Never updated after insert.
CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts     TEXT    NOT NULL,         -- ISO-8601 UTC e.g. 2026-01-01T12:00:00Z
    wallet_address  TEXT    NOT NULL,
    wallet_label    TEXT    NOT NULL,
    market_id       TEXT,
    condition_id    TEXT,
    event_id        TEXT,
    market_slug     TEXT,
    market_title    TEXT,
    category        TEXT,
    side            TEXT,                     -- "Yes" | "No"
    token_id        TEXT,                     -- ERC-1155 asset id
    shares          REAL,
    avg_entry_price REAL,                     -- cents  0-100
    current_price   REAL,                     -- cents
    current_value   REAL,
    cost_basis      REAL,
    unrealized_pnl  REAL,
    resolution_time TEXT,                     -- YYYY-MM-DD
    market_status   TEXT    DEFAULT 'open',   -- "open" | "resolved"
    raw_payload     TEXT,                     -- JSON from positions API
    UNIQUE(snapshot_ts, wallet_address, token_id)   -- exact-dup guard within a run
);

CREATE INDEX IF NOT EXISTS idx_ps_wallet   ON position_snapshots(wallet_address);
CREATE INDEX IF NOT EXISTS idx_ps_cond     ON position_snapshots(condition_id);
CREATE INDEX IF NOT EXISTS idx_ps_ts       ON position_snapshots(snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_ps_res_time ON position_snapshots(resolution_time);

-- ── Resolved positions ────────────────────────────────────────────────────────
-- One final row per (wallet, token, condition).  UNIQUE prevents double-resolve.
CREATE TABLE IF NOT EXISTS resolved_positions (
    resolved_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address      TEXT    NOT NULL,
    wallet_label        TEXT    NOT NULL,
    market_id           TEXT,
    condition_id        TEXT,
    market_title        TEXT,
    category            TEXT,
    side                TEXT,
    token_id            TEXT,
    shares              REAL,
    avg_entry_price     REAL,                 -- cents
    final_price         REAL,                 -- 100=WIN 0=LOSS cents
    cost_basis          REAL,
    payout_value        REAL,
    realized_pnl        REAL,
    roi_pct             REAL,
    win_loss            TEXT,                 -- "WIN"|"LOSS"|"PUSH"|"NEEDS_REVIEW"
    first_seen_at       TEXT,                 -- ISO-8601 UTC
    last_seen_at        TEXT,
    resolved_at         TEXT,
    holding_hours       REAL,
    source_snapshot_id  INTEGER,
    raw_resolution      TEXT,                 -- JSON from gamma-api
    UNIQUE(wallet_address, token_id, condition_id)
);

CREATE INDEX IF NOT EXISTS idx_rp_wallet ON resolved_positions(wallet_address);
CREATE INDEX IF NOT EXISTS idx_rp_cat    ON resolved_positions(category);
CREATE INDEX IF NOT EXISTS idx_rp_wl     ON resolved_positions(win_loss);
CREATE INDEX IF NOT EXISTS idx_rp_res_at ON resolved_positions(resolved_at);

-- ── Wallet performance summary ────────────────────────────────────────────────
-- Recomputed after every resolver run.  One row per (wallet, category).
-- category = '__all__' is the cross-category aggregate.
CREATE TABLE IF NOT EXISTS wallet_performance_summary (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_address        TEXT    NOT NULL,
    category              TEXT    NOT NULL DEFAULT '__all__',
    total_positions       INTEGER DEFAULT 0,
    resolved_positions    INTEGER DEFAULT 0,
    wins                  INTEGER DEFAULT 0,
    losses                INTEGER DEFAULT 0,
    pushes                INTEGER DEFAULT 0,
    win_rate              REAL    DEFAULT 0.0,
    total_cost_basis      REAL    DEFAULT 0.0,
    total_payout          REAL    DEFAULT 0.0,
    total_pnl             REAL    DEFAULT 0.0,
    roi_pct               REAL    DEFAULT 0.0,
    avg_position_size     REAL    DEFAULT 0.0,
    median_position_size  REAL    DEFAULT 0.0,
    rolling_7d_roi        REAL,
    rolling_30d_roi       REAL,
    rolling_90d_roi       REAL,
    last_updated_at       TEXT,
    UNIQUE(wallet_address, category)
);
"""


def get_conn(path: str = BACKTEST_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(path: str = BACKTEST_DB) -> sqlite3.Connection:
    """Create tables if they don't exist and return an open connection."""
    conn = get_conn(path)
    conn.executescript(_SCHEMA)
    return conn
