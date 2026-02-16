"""CLIレポート表示。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

DB_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "assets.db"


def _fmt(n: float) -> str:
    """数値を3桁区切りで表示する。"""
    return f"{n:,.0f}"


def print_report(db_path: str, date: str | None = None) -> None:
    """資産レポートをCLIに表示する。"""
    conn = sqlite3.connect(db_path)

    # 対象日決定
    if date is None:
        row = conn.execute("SELECT date FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
        if not row:
            print("データがありません。先にデータを取得してください。")
            return
        date = row[0]

    # スナップショット取得
    row = conn.execute("SELECT total_asset, by_class_json FROM snapshots WHERE date = ?", (date,)).fetchone()
    if not row:
        print(f"{date} のデータがありません。")
        return

    total_asset = row[0]
    by_class = json.loads(row[1])

    # 口座
    accounts = conn.execute(
        "SELECT account_name, asset_class, balance, institution FROM snapshot_accounts WHERE date = ? ORDER BY balance DESC",
        (date,),
    ).fetchall()

    # 銘柄
    holdings = conn.execute(
        "SELECT name, symbol_or_code, asset_class, value, quantity FROM snapshot_holdings WHERE date = ? ORDER BY value DESC",
        (date,),
    ).fetchall()

    conn.close()

    # --- 表示 ---
    w = 60
    print()
    print("=" * w)
    print(f"  資産レポート  {date}")
    print("=" * w)
    print()
    print(f"  資産総額:  {_fmt(total_asset)} 円")
    print()

    # クラス別内訳
    print("-" * w)
    print("  資産クラス別内訳")
    print("-" * w)
    for cls, amt in by_class.items():
        ratio = amt / total_asset * 100 if total_asset else 0
        bar = "█" * int(ratio / 2)
        print(f"  {cls:<14s}  {_fmt(amt):>14s}円  {ratio:5.1f}%  {bar}")
    print()

    # 口座一覧
    print("-" * w)
    print(f"  口座一覧 ({len(accounts)}件)")
    print("-" * w)
    for name, _cls, balance, inst in accounts:
        label = f"{inst} / {name}" if inst and inst != name else name
        print(f"  {label:<36s}  {_fmt(balance):>12s}円")
    print()

    # 銘柄一覧（クラス別）
    print("-" * w)
    print(f"  保有銘柄 ({len(holdings)}件)")
    print("-" * w)
    current_class = None
    for name, code, cls, value, qty in holdings:
        if cls != current_class:
            current_class = cls
            print(f"\n  [{cls}]")
        code_str = f"({code}) " if code else ""
        qty_str = f" x{qty:,.0f}" if qty else ""
        print(f"    {code_str}{name}{qty_str:<30s}  {_fmt(value):>12s}円")
    print()
    print("=" * w)


def main() -> None:
    parser = argparse.ArgumentParser(description="資産レポート表示")
    parser.add_argument("--db", type=str, default=str(DB_DEFAULT), help="SQLiteデータベースのパス")
    parser.add_argument("--date", type=str, default=None, help="対象日（YYYY-MM-DD）。省略時は最新")
    args = parser.parse_args()
    print_report(args.db, args.date)


if __name__ == "__main__":
    main()
