"""前日／前月／前年との比較ロジック。

対象日のスナップショットと比較対象日のスナップショットの差分を計算する。
比較対象日は、指定期間前に最も近い日のデータを使う。
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta


@dataclass
class ComparisonResult:
    """比較結果。"""
    label: str  # "前日比", "前月比", "前年比"
    target_date: str
    compare_date: str | None
    total_diff: float | None
    total_ratio: float | None  # 変動率 (%)
    by_class_diff: dict[str, float]  # クラス名 → 差分
    account_diffs: list[dict]  # [{name, diff, current, previous}]
    holding_diffs: list[dict]  # [{name, code, diff, current, previous}]


def _get_snapshot_full(conn: sqlite3.Connection, target_date: str) -> dict | None:
    """指定日のフルスナップショット（総資産+口座+銘柄）を取得する。"""
    row = conn.execute(
        "SELECT total_asset, by_class_json FROM snapshots WHERE date = ?",
        (target_date,),
    ).fetchone()
    if row is None:
        return None

    accounts = conn.execute(
        "SELECT account_name, asset_class, balance, institution FROM snapshot_accounts WHERE date = ?",
        (target_date,),
    ).fetchall()

    holdings = conn.execute(
        "SELECT symbol_or_code, name, value, quantity, asset_class, position FROM snapshot_holdings WHERE date = ? ORDER BY asset_class, position",
        (target_date,),
    ).fetchall()

    return {
        "total_asset": row[0],
        "by_class": json.loads(row[1]),
        "accounts": accounts,
        "holdings": holdings,
    }


def _find_nearest_date(conn: sqlite3.Connection, target: str, before: bool = True) -> str | None:
    """target日以前(before=True)または以後の最も近い日付を返す。target自身は除く。"""
    if before:
        row = conn.execute(
            "SELECT date FROM snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
            (target,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT date FROM snapshots WHERE date > ? ORDER BY date ASC LIMIT 1",
            (target,),
        ).fetchone()
    return row[0] if row else None


def _find_nearest_to_target(conn: sqlite3.Connection, target_date_str: str) -> str | None:
    """target_date_strに最も近い日付のデータを返す（target自身含む、過去方向優先）。"""
    row = conn.execute(
        "SELECT date FROM snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (target_date_str,),
    ).fetchone()
    if row:
        return row[0]
    # 過去にない場合、未来方向
    row = conn.execute(
        "SELECT date FROM snapshots WHERE date > ? ORDER BY date ASC LIMIT 1",
        (target_date_str,),
    ).fetchone()
    return row[0] if row else None


def _compute_diff(
    conn: sqlite3.Connection,
    target_date: str,
    compare_date: str | None,
    label: str,
) -> ComparisonResult:
    """2つの日付間の差分を計算する。"""
    if compare_date is None:
        return ComparisonResult(
            label=label,
            target_date=target_date,
            compare_date=None,
            total_diff=None,
            total_ratio=None,
            by_class_diff={},
            account_diffs=[],
            holding_diffs=[],
        )

    current = _get_snapshot_full(conn, target_date)
    previous = _get_snapshot_full(conn, compare_date)

    if current is None or previous is None:
        return ComparisonResult(
            label=label,
            target_date=target_date,
            compare_date=compare_date,
            total_diff=None,
            total_ratio=None,
            by_class_diff={},
            account_diffs=[],
            holding_diffs=[],
        )

    # 総資産差分
    total_diff = current["total_asset"] - previous["total_asset"]
    total_ratio = (total_diff / previous["total_asset"] * 100) if previous["total_asset"] else None

    # クラス別差分
    all_classes = set(current["by_class"].keys()) | set(previous["by_class"].keys())
    by_class_diff = {}
    for cls in all_classes:
        cur_val = current["by_class"].get(cls, 0)
        prev_val = previous["by_class"].get(cls, 0)
        diff = cur_val - prev_val
        if diff != 0:
            by_class_diff[cls] = diff

    # 口座別差分 (account_name + institution をキーにマッチング)
    prev_accounts = {}
    for acc in previous["accounts"]:
        key = (acc[0], acc[3])  # (account_name, institution)
        prev_accounts[key] = acc[2]  # balance

    account_diffs = []
    for acc in current["accounts"]:
        key = (acc[0], acc[3])
        cur_bal = acc[2]
        prev_bal = prev_accounts.get(key, 0)
        diff = cur_bal - prev_bal
        if diff != 0:
            label_name = f"{acc[3]} / {acc[0]}" if acc[3] and acc[3] != acc[0] else acc[0]
            account_diffs.append({
                "name": label_name,
                "diff": diff,
                "current": cur_bal,
                "previous": prev_bal,
            })
    account_diffs.sort(key=lambda x: abs(x["diff"]), reverse=True)

    # 銘柄別差分
    # 同名銘柄（投信など）は (asset_class, name) でグループ化し、
    # 評価額の大きい順に並べてランク同士をマッチングする。
    # これにより、新規口座の挿入でテーブル位置がずれても正しくマッチできる。
    prev_by_name: dict[tuple, list] = defaultdict(list)
    for h in previous["holdings"]:
        prev_by_name[(h[4], h[1])].append(h)  # (asset_class, name)
    for group in prev_by_name.values():
        group.sort(key=lambda x: x[2], reverse=True)  # value desc

    cur_by_name: dict[tuple, list] = defaultdict(list)
    for h in current["holdings"]:
        cur_by_name[(h[4], h[1])].append(h)
    for group in cur_by_name.values():
        group.sort(key=lambda x: x[2], reverse=True)

    holding_diffs = []
    all_names = set(prev_by_name.keys()) | set(cur_by_name.keys())
    for name_key in all_names:
        prev_list = prev_by_name.get(name_key, [])
        cur_list = cur_by_name.get(name_key, [])
        for i in range(max(len(prev_list), len(cur_list))):
            cur_val = cur_list[i][2] if i < len(cur_list) else 0
            prev_val = prev_list[i][2] if i < len(prev_list) else 0
            diff = cur_val - prev_val
            if diff != 0:
                ref = cur_list[i] if i < len(cur_list) else prev_list[i]
                holding_diffs.append({
                    "name": ref[1],
                    "code": ref[0],
                    "asset_class": ref[4],
                    "diff": diff,
                    "current": cur_val,
                    "previous": prev_val,
                })
    holding_diffs.sort(key=lambda x: abs(x["diff"]), reverse=True)

    return ComparisonResult(
        label=label,
        target_date=target_date,
        compare_date=compare_date,
        total_diff=total_diff,
        total_ratio=total_ratio,
        by_class_diff=by_class_diff,
        account_diffs=account_diffs,
        holding_diffs=holding_diffs,
    )


def compare_daily(db_path: str, target_date: str) -> ComparisonResult:
    """前日比を計算する。"""
    conn = sqlite3.connect(db_path)
    try:
        compare_date = _find_nearest_date(conn, target_date, before=True)
        return _compute_diff(conn, target_date, compare_date, "前日比")
    finally:
        conn.close()


def compare_monthly(db_path: str, target_date: str) -> ComparisonResult:
    """前月比を計算する。"""
    conn = sqlite3.connect(db_path)
    try:
        d = date.fromisoformat(target_date)
        # 約1ヶ月前
        target_month = d - timedelta(days=30)
        compare_date = _find_nearest_to_target(conn, target_month.isoformat())
        # 同日は除外
        if compare_date == target_date:
            compare_date = None
        return _compute_diff(conn, target_date, compare_date, "前月比")
    finally:
        conn.close()


def compare_yearly(db_path: str, target_date: str) -> ComparisonResult:
    """前年比を計算する。"""
    conn = sqlite3.connect(db_path)
    try:
        d = date.fromisoformat(target_date)
        # 約1年前
        try:
            target_year = d.replace(year=d.year - 1)
        except ValueError:
            # 2/29 → 2/28
            target_year = d.replace(year=d.year - 1, day=28)
        compare_date = _find_nearest_to_target(conn, target_year.isoformat())
        # 同日は除外
        if compare_date == target_date:
            compare_date = None
        return _compute_diff(conn, target_date, compare_date, "前年比")
    finally:
        conn.close()


def get_all_comparisons(db_path: str, target_date: str) -> list[ComparisonResult]:
    """前日比・前月比・前年比をまとめて返す。"""
    return [
        compare_daily(db_path, target_date),
        compare_monthly(db_path, target_date),
        compare_yearly(db_path, target_date),
    ]
