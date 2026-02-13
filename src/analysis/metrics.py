"""資産分析指標の算出。

- 資産クラス比率
- 上位N口座/銘柄の集中度
- 日次変動率
- ボラティリティ（直近30日）
- 最大ドローダウン
- 短期/中期移動平均
"""

from __future__ import annotations

import json
import math
import sqlite3


def asset_class_ratio(db_path: str, date: str) -> dict[str, float]:
    """資産クラス別の比率(%)を算出する。"""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT by_class_json FROM snapshots WHERE date = ?", (date,)
    ).fetchone()
    conn.close()
    if not row:
        return {}
    by_class = json.loads(row[0])
    total = sum(by_class.values())
    if total == 0:
        return {}
    return {cls: val / total * 100 for cls, val in by_class.items()}


def concentration_top_n(db_path: str, date: str, n: int = 5) -> dict:
    """上位N銘柄の集中度を算出する。"""
    conn = sqlite3.connect(db_path)
    holdings = conn.execute(
        "SELECT name, value FROM snapshot_holdings WHERE date = ? ORDER BY value DESC",
        (date,),
    ).fetchall()
    total_row = conn.execute(
        "SELECT total_asset FROM snapshots WHERE date = ?", (date,)
    ).fetchone()
    conn.close()

    if not holdings or not total_row:
        return {"top_n": [], "concentration_pct": 0}

    total_asset = total_row[0]
    top = holdings[:n]
    top_sum = sum(h[1] for h in top)
    concentration = top_sum / total_asset * 100 if total_asset else 0

    return {
        "top_n": [{"name": h[0], "value": h[1], "pct": h[1] / total_asset * 100} for h in top],
        "concentration_pct": concentration,
    }


def daily_volatility(db_path: str, days: int = 30) -> float | None:
    """直近N日のボラティリティ（日次リターンの標準偏差、年率換算）を算出する。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT total_asset FROM snapshots ORDER BY date DESC LIMIT ?", (days + 1,)
    ).fetchall()
    conn.close()

    if len(rows) < 3:
        return None

    # 古い順に並べ替え
    values = [r[0] for r in reversed(rows)]
    returns = []
    for i in range(1, len(values)):
        if values[i - 1] > 0:
            returns.append(values[i] / values[i - 1] - 1)

    if len(returns) < 2:
        return None

    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    std = math.sqrt(variance)

    # 年率換算
    return std * math.sqrt(252)


def max_drawdown(db_path: str) -> float | None:
    """最大ドローダウン(%)を算出する。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT total_asset FROM snapshots ORDER BY date ASC"
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        return None

    values = [r[0] for r in rows]
    peak = values[0]
    max_dd = 0.0

    for v in values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    return max_dd


def moving_average(db_path: str, window: int) -> list[tuple[str, float]]:
    """移動平均を算出する。[(date, ma_value), ...]"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT date, total_asset FROM snapshots ORDER BY date ASC"
    ).fetchall()
    conn.close()

    if len(rows) < window:
        return []

    result = []
    for i in range(window - 1, len(rows)):
        window_values = [rows[j][1] for j in range(i - window + 1, i + 1)]
        ma = sum(window_values) / window
        result.append((rows[i][0], ma))

    return result
