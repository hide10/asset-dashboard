"""SQLiteデータベースの初期化とスキーマ定義。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "assets.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS snapshots (
    date        TEXT PRIMARY KEY,  -- YYYY-MM-DD
    total_asset REAL NOT NULL,
    by_class_json TEXT NOT NULL,   -- JSON: {"現金": 100000, "証券": 200000, ...}
    raw_path    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    date          TEXT NOT NULL,
    account_name  TEXT NOT NULL,
    asset_class   TEXT NOT NULL,
    balance       REAL NOT NULL,
    institution   TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (date) REFERENCES snapshots(date)
);

CREATE TABLE IF NOT EXISTS snapshot_holdings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    date           TEXT NOT NULL,
    symbol_or_code TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL,
    quantity       REAL,           -- nullable
    value          REAL NOT NULL,
    asset_class    TEXT NOT NULL DEFAULT '',
    position       INTEGER NOT NULL DEFAULT 0,  -- テーブル内行位置（同名銘柄区別用）
    acquisition_price  REAL,           -- 平均取得単価（nullable）
    current_price      REAL,           -- 現在値/基準価額（nullable）
    unrealized_gain    REAL,           -- 評価損益（nullable）
    unrealized_gain_pct REAL,          -- 評価損益率 %（nullable）
    FOREIGN KEY (date) REFERENCES snapshots(date)
);

CREATE TABLE IF NOT EXISTS monthly_cashflows (
    year_month  TEXT PRIMARY KEY,  -- YYYY-MM
    income      REAL NOT NULL,
    expense     REAL NOT NULL,
    fetched     TEXT NOT NULL       -- YYYY-MM-DD
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_comments (
    date       TEXT NOT NULL,
    page       TEXT NOT NULL,       -- 'dashboard' or 'lifeplan'
    comment    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (date, page)
);

CREATE TABLE IF NOT EXISTS cf_transactions (
    id               TEXT PRIMARY KEY,
    year_month       TEXT NOT NULL,        -- YYYY-MM
    date             TEXT NOT NULL,        -- YYYY-MM-DD
    description      TEXT NOT NULL,
    amount           INTEGER NOT NULL,     -- 負=支出, 正=収入
    institution      TEXT NOT NULL DEFAULT '',
    major_category   TEXT NOT NULL DEFAULT '',
    minor_category   TEXT NOT NULL DEFAULT '',
    memo             TEXT NOT NULL DEFAULT '',
    is_transfer      INTEGER NOT NULL DEFAULT 0,
    is_target        INTEGER NOT NULL DEFAULT 1,
    fetched          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cf_ym ON cf_transactions(year_month);

CREATE TABLE IF NOT EXISTS cf_csv_months (
    year_month  TEXT PRIMARY KEY,
    fetched     TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0
);
"""


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """データベースを初期化し、接続を返す。"""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # マイグレーション: 不足カラムがあれば追加
    cols = [row[1] for row in conn.execute("PRAGMA table_info(snapshot_holdings)").fetchall()]
    if "position" not in cols:
        conn.execute("ALTER TABLE snapshot_holdings ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
    if "acquisition_price" not in cols:
        conn.execute("ALTER TABLE snapshot_holdings ADD COLUMN acquisition_price REAL")
    if "current_price" not in cols:
        conn.execute("ALTER TABLE snapshot_holdings ADD COLUMN current_price REAL")
    if "unrealized_gain" not in cols:
        conn.execute("ALTER TABLE snapshot_holdings ADD COLUMN unrealized_gain REAL")
    if "unrealized_gain_pct" not in cols:
        conn.execute("ALTER TABLE snapshot_holdings ADD COLUMN unrealized_gain_pct REAL")
    conn.commit()

    return conn


if __name__ == "__main__":
    connection = init_db()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
    connection.close()
