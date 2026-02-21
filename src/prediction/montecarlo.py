"""モンテカルロシミュレーションによる成長予測。

フェーズ1: 追加投資なし（過去リターンからレンジ推定）
フェーズ2: 積立込み（月次入金パラメータ付き）

リスク資産（株式・投信・株式型年金）のみシミュレーション対象とし、
安全資産（預金・不動産・保険型年金）は固定値として加算する。
データが少ない場合（60日未満）は資産クラス別デフォルトパラメータの加重平均を使用する。
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
    "株式（現物）": (0.07, 0.20),  # 国内・海外個別株混合
    "投資信託": (0.07, 0.15),  # インデックス投信主体
    "年金（株式型）": (0.07, 0.15),  # iDeCo インデックス中心
    "不動産": (0.01, 0.05),
    "預金・現金・暗号資産": (0.0, 0.0),
    "年金（保険型）": (0.01, 0.02),  # 個人年金保険等
    "債券": (0.01, 0.03),
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


@dataclass
class SimulatorResult:
    """ライフサイクルシミュレーション結果。"""

    yearly_balances: list[dict]  # [{"age": 35, "p10":..., "p25":..., "p50":..., "p75":..., "p90":...}, ...]
    depletion_probability: float  # 枯渇確率 (0.0-1.0)
    principal_loss_probability: float  # 元本割れ確率 (0.0-1.0)
    total_principal: float  # 投入元本合計
    total_gains: float  # 運用益（P50）
    total_tax: float  # 税金合計（P50）
    net_final: float  # 最終残高（P50）


def _get_daily_totals(db_path: str) -> list[tuple[str, float]]:
    """全日の(date, total_asset)を日付昇順で返す。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT date, total_asset FROM snapshots ORDER BY date ASC").fetchall()
    conn.close()
    return rows


def _estimate_params(
    totals: list[tuple[str, float]],
    class_values: dict[str, float] | None = None,
) -> tuple[float, float, bool]:
    """日次リターンから年率期待リターンとボラティリティを推定する。

    データが60日未満の場合は class_values の加重平均デフォルト値を使用する。
    短期間のデータではノイズが大きく、年率換算で極端な値になるため。

    Returns:
        (annual_return, annual_volatility, is_estimated)
        is_estimated=True はデフォルト値を使用したことを意味する。
    """
    min_days = 60  # 約3ヶ月の営業日

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

    if len(daily_returns) < min_days:
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
            monthly_growth = math.exp((monthly_return - 0.5 * monthly_vol**2) + monthly_vol * z)
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
        years_list = [1, 3, 5, 10, 20, 30]

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
        years_list = [1, 3, 5, 10, 20, 30]

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


def run_lifecycle_simulation(
    current_age: int,
    retirement_age: int,
    end_age: int,
    initial_investment: float,
    monthly_contribution: float,
    annual_return: float,
    annual_volatility: float,
    monthly_withdrawal: float,
    inflation_rate: float = 0.0,
    expense_ratio: float = 0.0,
    pension_start_age: int = 65,
    monthly_pension: float = 0.0,
    other_monthly_income: float = 0.0,
    tax_rate: float = 0.20315,
    safe_value: float = 0.0,
    simulations: int = 2000,
    rng_seed: int | None = 42,
) -> SimulatorResult:
    """ライフサイクル全体のモンテカルロシミュレーションを実行する。

    蓄積期間（current_age → retirement_age）と取崩し期間（retirement_age → end_age）を
    月次ステップでシミュレーションし、パーセンタイル・枯渇確率等を返す。

    initial_investment: リスク資産額（GBMで成長）
    safe_value: 安全資産額（成長なし、取崩し時に先に消費）
    """
    import random

    if rng_seed is not None:
        rng = random.Random(rng_seed)
    else:
        rng = random.Random()

    # ドリフト調整: インフレ率・信託報酬を差し引く
    drift = annual_return - inflation_rate - expense_ratio
    monthly_drift = drift / 12
    monthly_vol = annual_volatility / math.sqrt(12)

    total_years = end_age - current_age
    total_months = total_years * 12
    accumulation_months = (retirement_age - current_age) * 12

    # 元本合計 = リスク資産 + 安全資産 + 蓄積期間の積立合計
    total_principal = initial_investment + safe_value + monthly_contribution * accumulation_months

    # 各シミュレーションの年末残高を記録
    sim_yearly: list[list[float]] = []
    depleted_count = 0
    principal_loss_count = 0

    sim_final: list[float] = []
    sim_tax_total: list[float] = []

    for _ in range(simulations):
        risk = initial_investment  # リスク資産（GBMで成長）
        safe = safe_value  # 安全資産（成長なし）
        cost_basis = initial_investment
        tax_cumulative = 0.0
        depleted = False
        yearly_values: list[float] = []

        for month in range(total_months):
            if depleted:
                if month % 12 == 11:
                    yearly_values.append(0.0)
                continue

            # GBM で成長（リスク資産のみ）
            z = rng.gauss(0, 1)
            growth = math.exp((monthly_drift - 0.5 * monthly_vol**2) + monthly_vol * z)
            risk = risk * growth

            if month < accumulation_months:
                # 蓄積期間: 月次積立（リスク資産へ）
                risk += monthly_contribution
                cost_basis += monthly_contribution
            else:
                # 取崩し期間
                year_in_sim = month // 12
                age = current_age + year_in_sim

                # 収入（年金 + その他）→ 安全資産へ
                income = other_monthly_income
                if age >= pension_start_age:
                    income += monthly_pension
                safe += income

                # 取崩し: 安全資産から先に消費
                withdrawal = monthly_withdrawal

                # 税金はリスク資産の含み益に対してのみ発生
                if risk > 0 and cost_basis < risk:
                    gain_ratio = (risk - cost_basis) / risk
                    # 取崩しのうちリスク資産から出る分に課税
                    risk_withdrawal = max(0.0, withdrawal - safe)
                    if risk_withdrawal > 0:
                        tax = risk_withdrawal * gain_ratio * tax_rate
                        tax_cumulative += tax
                        risk -= tax

                # 安全資産から先に引き出し、足りなければリスク資産から
                if safe >= withdrawal:
                    safe -= withdrawal
                else:
                    remainder = withdrawal - safe
                    safe = 0.0
                    risk -= remainder

                if risk + safe <= 0:
                    risk = 0.0
                    safe = 0.0
                    depleted = True

            # 年末に記録（リスク + 安全の合計）
            if month % 12 == 11:
                yearly_values.append(max(risk + safe, 0.0))

        total_value = max(risk + safe, 0.0)
        sim_yearly.append(yearly_values)
        sim_final.append(total_value)
        sim_tax_total.append(tax_cumulative)

        if depleted:
            depleted_count += 1
        if total_value < total_principal:
            principal_loss_count += 1

    # 年次パーセンタイル集計
    yearly_balances: list[dict] = []
    for year_idx in range(total_years):
        age = current_age + year_idx + 1  # 年末時点の年齢
        values = sorted(sim_yearly[s][year_idx] for s in range(simulations) if year_idx < len(sim_yearly[s]))
        n = len(values)
        if n == 0:
            continue
        yearly_balances.append(
            {
                "age": age,
                "p10": values[int(n * 0.10)],
                "p25": values[int(n * 0.25)],
                "p50": values[int(n * 0.50)],
                "p75": values[int(n * 0.75)],
                "p90": values[int(n * 0.90)],
            }
        )

    # P50 ベースの財務サマリー
    sim_final.sort()
    sim_tax_total.sort()
    n = len(sim_final)
    net_final_p50 = sim_final[int(n * 0.50)]
    total_tax_p50 = sim_tax_total[int(n * 0.50)]
    total_gains = net_final_p50 + total_tax_p50 - total_principal

    return SimulatorResult(
        yearly_balances=yearly_balances,
        depletion_probability=depleted_count / simulations,
        principal_loss_probability=principal_loss_count / simulations,
        total_principal=total_principal,
        total_gains=total_gains,
        total_tax=total_tax_p50,
        net_final=net_final_p50,
    )
