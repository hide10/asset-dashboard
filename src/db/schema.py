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
"""


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """データベースを初期化し、接続を返す。"""
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_SQL)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # マイグレーション: position カラムがなければ追加
    cols = [row[1] for row in conn.execute("PRAGMA table_info(snapshot_holdings)").fetchall()]
    if "position" not in cols:
        conn.execute("ALTER TABLE snapshot_holdings ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
        conn.commit()

    return conn


if __name__ == "__main__":
    connection = init_db()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
    connection.close()
