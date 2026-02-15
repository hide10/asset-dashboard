"""スナップショットの保存・取得を担うリポジトリ層。"""

from __future__ import annotations

import json
import sqlite3

from src.parser.normalize import AssetSnapshot
from src.parser.cashflow import CashflowMonth


def save_snapshot(conn: sqlite3.Connection, snapshot: AssetSnapshot, raw_path: str) -> None:
    """AssetSnapshotをDBに保存する。同日データがあれば差し替える。"""
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        (snapshot.date, snapshot.total_asset, json.dumps(snapshot.by_class, ensure_ascii=False), raw_path),
    )
    # 同日の既存データを削除してから再挿入
    conn.execute("DELETE FROM snapshot_accounts WHERE date = ?", (snapshot.date,))
    conn.execute("DELETE FROM snapshot_holdings WHERE date = ?", (snapshot.date,))

    for acc in snapshot.accounts:
        conn.execute(
            "INSERT INTO snapshot_accounts (date, account_name, asset_class, balance, institution) VALUES (?, ?, ?, ?, ?)",
            (snapshot.date, acc.account_name, acc.asset_class, acc.balance, acc.institution),
        )
    for h in snapshot.holdings:
        conn.execute(
            "INSERT INTO snapshot_holdings (date, symbol_or_code, name, quantity, value, asset_class, position, acquisition_price, current_price, unrealized_gain, unrealized_gain_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot.date, h.symbol_or_code, h.name, h.quantity, h.value, h.asset_class, h.position, h.acquisition_price, h.current_price, h.unrealized_gain, h.unrealized_gain_pct),
        )
    conn.commit()


def get_snapshot(conn: sqlite3.Connection, target_date: str) -> dict | None:
    """指定日のスナップショットを取得する。"""
    row = conn.execute(
        "SELECT date, total_asset, by_class_json, raw_path FROM snapshots WHERE date = ?",
        (target_date,),
    ).fetchone()
    if row is None:
        return None
    return {
        "date": row[0],
        "total_asset": row[1],
        "by_class": json.loads(row[2]),
        "raw_path": row[3],
    }


def get_nearest_snapshot(conn: sqlite3.Connection, target_date: str) -> dict | None:
    """指定日に最も近いスナップショットを取得する（同日含む、過去方向優先）。"""
    row = conn.execute(
        "SELECT date, total_asset, by_class_json, raw_path FROM snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (target_date,),
    ).fetchone()
    if row is None:
        return None
    return {
        "date": row[0],
        "total_asset": row[1],
        "by_class": json.loads(row[2]),
        "raw_path": row[3],
    }


def get_all_total_assets(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """全日の(date, total_asset)リストを返す（日付昇順）。"""
    rows = conn.execute(
        "SELECT date, total_asset FROM snapshots ORDER BY date ASC"
    ).fetchall()
    return rows


def save_cashflows(conn: sqlite3.Connection, months: list[CashflowMonth], fetched_date: str) -> None:
    """月次収支データをDBに保存する。同月データがあれば差し替える。"""
    for m in months:
        conn.execute(
            "INSERT OR REPLACE INTO monthly_cashflows (year_month, income, expense, fetched) VALUES (?, ?, ?, ?)",
            (m.year_month, m.income, m.expense, fetched_date),
        )
    conn.commit()


def get_cashflows(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    """月次収支データを新しい順に取得する。"""
    rows = conn.execute(
        "SELECT year_month, income, expense FROM monthly_cashflows ORDER BY year_month DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"year_month": r[0], "income": r[1], "expense": r[2]}
        for r in rows
    ]


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """設定値を取得する。"""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def save_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """設定値を保存する。"""
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
