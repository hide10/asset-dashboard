"""モンテカルロシミュレーションによる成長予測。

フェーズ1: 追加投資なし（過去リターンからレンジ推定）
フェーズ2: 積立込み（月次入金パラメータ付き）

リスク資産（株式・投信・株式型年金）のみシミュレーション対象とし、
安全資産（預金・不動産・保険型年金）は固定値として加算する。
データが少ない場合（5日未満）は資産クラス別デフォルトパラメータの加重平均を使用する。
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass


TRADING_DAYS_PER_YEAR = 252

# リスク資産とみなす資産クラス（基本）
RISK_CLASSES = {"株式（現物）", "投資信託"}

# 資産クラス別のデフォルトパラメータ（年率リターン, 年率ボラティリティ）
CLASS_PARAMS: dict[str, tuple[float, float]] = {
    "株式（現物）":       (0.07, 0.20),   # 国内株
    "投資信託":           (0.08, 0.18),   # 先進国株投信が主
    "年金（株式型）":     (0.08, 0.18),   # iDeCo・DC（株式運用）
    "不動産":             (0.01, 0.05),
    "預金・現金・暗号資産": (0.0, 0.0),
    "年金（保険型）":     (0.01, 0.02),   # 個人年金保険等
}

# 年金の保有銘柄名から株式型を判定するパターン
_EQUITY_PENSION_RE = re.compile(
    r"(iDeCo|確定拠出|DC|株式|先進国|全世界|S&P|オルカン|インデックス)",
    re.IGNORECASE,
)


def classify_pension_holdings(
    holdings: list[dict],
) -> tuple[float, float]:
    """年金クラスの保有銘柄を株式型/保険型に分類する。

    Returns:
        (equity_pension_value, insurance_pension_value)
    """
    equity = 0.0
    insurance = 0.0
    for h in holdings:
        if h.get("asset_class") != "年金":
            continue
        name = h.get("name", "")
        if _EQUITY_PENSION_RE.search(name):
            equity += h["value"]
        else:
            insurance += h["value"]
    return equity, insurance


def weighted_default_params(class_values: dict[str, float]) -> tuple[float, float]:
    """資産クラス別の評価額から加重平均のリターン/ボラティリティを算出する。

    リスク資産のみを対象に加重平均を計算する。
    """
    total = 0.0
    w_return = 0.0
    w_vol = 0.0
    for cls, value in class_values.items():
        params = CLASS_PARAMS.get(cls)
        if params is None or params == (0.0, 0.0):
            continue  # 安全資産はスキップ
        ret, vol = params
        w_return += ret * value
        w_vol += vol * value
        total += value
    if total == 0:
        return 0.05, 0.15  # フォールバック
    return w_return / total, w_vol / total


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


def _estimate_params(
    totals: list[tuple[str, float]],
    class_values: dict[str, float] | None = None,
) -> tuple[float, float, bool]:
    """日次リターンから年率期待リターンとボラティリティを推定する。

    データが5日未満の場合は class_values の加重平均デフォルト値を使用する。

    Returns:
        (annual_return, annual_volatility, is_estimated)
        is_estimated=True はデフォルト値を使用したことを意味する。
    """
    def _defaults() -> tuple[float, float]:
        if class_values:
            return weighted_default_params(class_values)
        return 0.05, 0.15

    if len(totals) < 2:
        r, v = _defaults()
        return r, v, True

    # 日次リターンを計算
    daily_returns = []
    for i in range(1, len(totals)):
        prev = totals[i - 1][1]
        curr = totals[i][1]
        if prev > 0:
            daily_returns.append(curr / prev - 1)

    if len(daily_returns) < 5:
        r, v = _defaults()
        return r, v, True

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
    risk_value: float,
    safe_value: float,
    annual_return: float,
    annual_volatility: float,
    years: int,
    monthly_contribution: float,
    simulations: int,
    rng_seed: int | None = 42,
) -> PredictionRange:
    """モンテカルロシミュレーションを実行し、PredictionRangeを返す。

    risk_value のみ市場変動の対象。safe_value は固定で最終結果に加算する。
    monthly_contribution はリスク資産側に積み立てる。
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
        value = risk_value
        for _ in range(total_months):
            # 幾何ブラウン運動（対数正規）
            z = rng.gauss(0, 1)
            monthly_growth = math.exp(
                (monthly_return - 0.5 * monthly_vol ** 2) + monthly_vol * z
            )
            value = value * monthly_growth + monthly_contribution
        # 安全資産を固定で加算
        results.append(value + safe_value)

    results.sort()
    n = len(results)
    p10 = results[int(n * 0.10)]
    p50 = results[int(n * 0.50)]
    p90 = results[int(n * 0.90)]

    return PredictionRange(years=years, p10=p10, p50=p50, p90=p90)


def predict_no_contribution(
    db_path: str,
    risk_value: float,
    safe_value: float,
    years_list: list[int] | None = None,
    simulations: int = 10000,
    class_values: dict[str, float] | None = None,
) -> tuple[list[PredictionRange], dict]:
    """追加投資なしの成長予測（フェーズ1）。

    Args:
        risk_value: リスク資産額（株式+投信+株式型年金）。シミュレーション対象。
        safe_value: 安全資産額（預金・不動産・保険型年金）。固定値として加算。
        class_values: 資産クラス別の評価額。デフォルトパラメータの加重平均計算に使用。

    Returns:
        (predictions, params) where params contains estimation details.
    """
    if years_list is None:
        years_list = [1, 3, 5]

    totals = _get_daily_totals(db_path)
    if not totals:
        return [], {"error": "データがありません"}

    annual_return, annual_vol, is_estimated = _estimate_params(totals, class_values)

    params = {
        "risk_value": risk_value,
        "safe_value": safe_value,
        "total_value": risk_value + safe_value,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "is_estimated": is_estimated,
        "data_points": len(totals),
    }

    predictions = []
    for years in years_list:
        pred = _run_simulation(
            risk_value=risk_value,
            safe_value=safe_value,
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
    risk_value: float,
    safe_value: float,
    monthly_contribution: float,
    years_list: list[int] | None = None,
    simulations: int = 10000,
    class_values: dict[str, float] | None = None,
) -> tuple[list[PredictionRange], dict]:
    """積立込みの成長予測（フェーズ2）。

    Args:
        risk_value: リスク資産額（株式+投信+株式型年金）。シミュレーション対象。
        safe_value: 安全資産額（預金・不動産・保険型年金）。固定値として加算。
        monthly_contribution: 月次積立額（リスク資産側に投入）。
        class_values: 資産クラス別の評価額。デフォルトパラメータの加重平均計算に使用。

    Returns:
        (predictions, params) where params contains estimation details.
    """
    if years_list is None:
        years_list = [1, 3, 5]

    totals = _get_daily_totals(db_path)
    if not totals:
        return [], {"error": "データがありません"}

    annual_return, annual_vol, is_estimated = _estimate_params(totals, class_values)

    params = {
        "risk_value": risk_value,
        "safe_value": safe_value,
        "total_value": risk_value + safe_value,
        "annual_return": annual_return,
        "annual_volatility": annual_vol,
        "is_estimated": is_estimated,
        "data_points": len(totals),
        "monthly_contribution": monthly_contribution,
    }

    predictions = []
    for years in years_list:
        pred = _run_simulation(
            risk_value=risk_value,
            safe_value=safe_value,
            annual_return=annual_return,
            annual_volatility=annual_vol,
            years=years,
            monthly_contribution=monthly_contribution,
            simulations=simulations,
        )
        predictions.append(pred)

    return predictions, params
