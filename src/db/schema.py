"""SQLiteデータベースの初期化とスキーマ定義。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from src.asset_classes import CASH_ASSET_CLASS, LEGACY_CASH_ASSET_CLASS, normalize_asset_classes

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

CREATE TABLE IF NOT EXISTS life_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type          TEXT NOT NULL,             -- one_time / recurring / education
    title               TEXT NOT NULL,
    amount              REAL NOT NULL,             -- 基準年の年額（円）
    start_year          INTEGER NOT NULL,
    repeat_every_years  INTEGER,                   -- NULL or 0: 単発
    end_year            INTEGER,                   -- NULL: 無期限
    enabled             INTEGER NOT NULL DEFAULT 1,
    note                TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_life_events_start_year ON life_events(start_year);

CREATE TABLE IF NOT EXISTS children_profiles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    birth_year          INTEGER NOT NULL,
    birth_month         INTEGER NOT NULL,
    education_plan_json TEXT NOT NULL DEFAULT '{}', -- JSON: {"kindergarten":"public", ...}
    enabled             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS life_plan_settings (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    inflation_rate REAL NOT NULL DEFAULT 0.01,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _migrate_asset_class_names(conn: sqlite3.Connection) -> None:
    """廃止済みの資産クラス名を履歴データを含めて統一する。"""
    snapshots = conn.execute("SELECT date, by_class_json FROM snapshots").fetchall()
    for snapshot_date, raw_by_class in snapshots:
        try:
            by_class = json.loads(raw_by_class)
        except (json.JSONDecodeError, TypeError):
            continue
        normalized = normalize_asset_classes(by_class)
        if normalized != by_class:
            conn.execute(
                "UPDATE snapshots SET by_class_json = ? WHERE date = ?",
                (json.dumps(normalized, ensure_ascii=False), snapshot_date),
            )

    conn.execute(
        "UPDATE snapshot_accounts SET asset_class = ? WHERE asset_class = ?",
        (CASH_ASSET_CLASS, LEGACY_CASH_ASSET_CLASS),
    )
    conn.execute(
        "UPDATE snapshot_holdings SET asset_class = ? WHERE asset_class = ?",
        (CASH_ASSET_CLASS, LEGACY_CASH_ASSET_CLASS),
    )


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

    _migrate_asset_class_names(conn)

    # ライフプラン設定の初期行
    conn.execute(
        "INSERT OR IGNORE INTO life_plan_settings (id, inflation_rate, updated_at) VALUES (1, 0.01, datetime('now'))"
    )
    conn.commit()

    return conn


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """軽量な接続取得（スキーマチェック・マイグレーションなし）。

    init_db() で初期化済みの DB に対して使う。
    PRAGMA 設定のみ行い、毎リクエストのオーバーヘッドを削減する。
    """
    if db_path is None:
        db_path = DEFAULT_DB_PATH
    db_path = Path(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


if __name__ == "__main__":
    connection = init_db()
    print(f"Database initialized at {DEFAULT_DB_PATH}")
    connection.close()
