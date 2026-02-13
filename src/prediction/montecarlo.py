"""モンテカルロシミュレーションによる成長予測。

フェーズ1: 追加投資なし（過去リターンからレンジ推定）
フェーズ2: 積立込み（月次入金パラメータ付き）

データが少ない場合（30日未満）は保守的なデフォルトパラメータを使用する。
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass


# データ不足時のデフォルトパラメータ（年率）
DEFAULT_ANNUAL_RETURN = 0.05  # 5%
DEFAULT_ANNUAL_VOLATILITY = 0.15  # 15%

TRADING_DAYS_PER_YEAR = 252


@dataclass
class PredictionRange:
    """予測レンジ（P10/P50/P90）。"""
    years: int
    p10: float
    p50: float
    p90: float


def _get_daily_totals(db_path: str) -> list[tuple[str, float]]:
    """全日の(date, total_asset)を日付昇順で返す。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT date, total_asset FROM snapshots ORDER BY date ASC"
    ).fetchall()
    conn.close()
    return rows


def _estimate_params(totals: list[tuple[str, float]]) -> tuple[float, float, bool]:
    """日次リターンから年率期待リターンとボラティリティを推定する。

    Returns:
        (annual_return, annual_volatility, is_estimated)
        is_estimated=True はデフォルト値を使用したことを意味する。
    """
    if len(totals) < 2:
        return DEFAULT_ANNUAL_RETURN, DEFAULT_ANNUAL_VOLATILITY, True

    # 日次リターンを計算
    daily_returns = []
    for i in range(1, len(totals)):
        prev = totals[i - 1][1]
        curr = totals[i][1]
        if prev > 0:
            daily_returns.append(curr / prev - 1)

    if len(daily_returns) < 5:
        return DEFAULT_ANNUAL_RETURN, DEFAULT_ANNUAL_VOLATILITY, True

    # 平均日次リターン
    mean_daily = sum(daily_returns) / len(daily_returns)
    # 日次ボラティリティ（標準偏差）
    variance = sum((r - mean_daily) ** 2 for r in daily_returns) / len(daily_returns)
    std_daily = math.sqrt(variance)

    # 年率換算
    annual_return = mean_daily * TRADING_DAYS_PER_YEAR
    annual_volatility = std_daily * math.sqrt(TRADING_DAYS_PER_YEAR)

    # 極端な値のクリッピング
    annual_return = max(-0.30, min(0.50, annual_return))
    annual_volatility = max(0.05, min(0.60, annual_volatility))

    return annual_return, annual_volatility, False


def _run_simulation(
    current_value: float,
    annual_return: float,
    annual_volatility: float,
    years: int,
    monthly_contribution: float,
    simulations: int,
    rng_seed: int | None = 42,
) -> PredictionRange:
    """モンテカルロシミュレーションを実行し、PredictionRangeを返す。

    月次ステップで計算する（年12ステップ）。
    """
    import random

    if rng_seed is not None:
        rng = random.Random(rng_seed)
    else:
        rng = random.Random()

    monthly_return = annual_return / 12
    monthly_vol = annual_volatility / math.sqrt(12)

    total_months = years * 12
    results = []

    for _ in range(simulations):
        value = current_value
        for _ in range(total_months):
            # 幾何ブラウン運動（対数正規）
            z = rng.gauss(0, 1)
            monthly_growth = math.exp(
                (monthly_return - 0.5 * monthly_vol ** 2) + monthly_vol * z
            )
            value = value * monthly_growth + monthly_contribution
        results.append(value)

    results.sort()
    n = len(results)
    p10 = results[int(n * 0.10)]
    p50 = results[int(n * 0.50)]
    p90 = results[int(n * 0.90)]

    return PredictionRange(years=years, p10=p10, p50=p50, p90=p90)


def predict_no_contribution(
    db_path: str,
    years_list: list[int] | None = None,
    simulations: int = 10000,
) -> tuple[list[PredictionRange], dict]:
    """追加投資なしの成長予測（フェーズ1）。

    Returns:
        (predictions, params) where params contains estimation details.
    """
    if years_list is None:
        years_list = [1, 3, 5]

    totals = _get_daily_totals(db_path)
    if not totals:
        return [], {"error": "データがありません"}

    current_value = totals[-1][1]
    annual_return, annual_vol, is_estimated = _estimate_params(totals)

    params = {
        "current_value": current_value,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "is_estimated": is_estimated,
        "data_points": len(totals),
    }

    predictions = []
    for years in years_list:
        pred = _run_simulation(
            current_value=current_value,
            annual_return=annual_return,
            annual_volatility=annual_vol,
            years=years,
            monthly_contribution=0,
            simulations=simulations,
        )
        predictions.append(pred)

    return predictions, params


def predict_with_contribution(
    db_path: str,
    monthly_contribution: float,
    years_list: list[int] | None = None,
    simulations: int = 10000,
) -> tuple[list[PredictionRange], dict]:
    """積立込みの成長予測（フェーズ2）。

    Returns:
        (predictions, params) where params contains estimation details.
    """
    if years_list is None:
        years_list = [1, 3, 5]

    totals = _get_daily_totals(db_path)
    if not totals:
        return [], {"error": "データがありません"}

    current_value = totals[-1][1]
    annual_return, annual_vol, is_estimated = _estimate_params(totals)

    params = {
        "current_value": current_value,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "is_estimated": is_estimated,
        "data_points": len(totals),
        "monthly_contribution": monthly_contribution,
    }

    predictions = []
    for years in years_list:
        pred = _run_simulation(
            current_value=current_value,
            annual_return=annual_return,
            annual_volatility=annual_vol,
            years=years,
            monthly_contribution=monthly_contribution,
            simulations=simulations,
        )
        predictions.append(pred)

    return predictions, params
