"""montecarlo.py のテスト。"""

from __future__ import annotations

import pytest

from src.prediction.montecarlo import _estimate_params, run_lifecycle_simulation, weighted_default_params


def _totals_with_growth(days: int, daily_growth: float) -> list[tuple[str, float]]:
    """Create (date, total_asset) rows with a fixed daily growth rate."""
    value = 1_000_000.0
    rows: list[tuple[str, float]] = []
    for d in range(days):
        rows.append((f"2025-01-{d + 1:02d}", value))
        value *= 1 + daily_growth
    return rows


class TestEstimateParams:
    def test_uses_default_when_less_than_60_daily_returns(self):
        totals = _totals_with_growth(days=60, daily_growth=0.001)  # daily_returns = 59
        class_values = {"投資信託": 3_000_000, "株式（現物）": 1_000_000}

        annual_return, annual_vol, is_estimated = _estimate_params(totals, class_values)

        expected_return, expected_vol = weighted_default_params(class_values)
        assert is_estimated is True
        assert annual_return == pytest.approx(expected_return)
        assert annual_vol == pytest.approx(expected_vol)

    def test_estimates_from_data_when_60_or_more_daily_returns(self):
        totals = _totals_with_growth(days=61, daily_growth=0.001)  # daily_returns = 60

        annual_return, annual_vol, is_estimated = _estimate_params(totals, class_values=None)

        assert is_estimated is False
        assert annual_return > 0
        # fixed growth series has near-zero std, but clipped at lower bound 0.05
        assert annual_vol == pytest.approx(0.05)


class TestLifecycleSimulation:
    """run_lifecycle_simulation のテスト。"""

    def test_accumulation_only(self):
        """蓄積のみ（retirement_age = end_age）→ P50 > 元本 + 積立合計。"""
        result = run_lifecycle_simulation(
            current_age=35,
            retirement_age=65,
            end_age=65,
            initial_investment=5_000_000,
            monthly_contribution=50_000,
            annual_return=0.05,
            annual_volatility=0.15,
            monthly_withdrawal=0,
            simulations=500,
            rng_seed=42,
        )
        total_principal = 5_000_000 + 50_000 * (65 - 35) * 12
        assert result.net_final > total_principal
        assert result.total_principal == total_principal
        assert result.depletion_probability == 0.0
        assert len(result.yearly_balances) == 30

    def test_high_withdrawal_causes_depletion(self):
        """高額取崩し → 枯渇確率 > 0。"""
        result = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=95,
            initial_investment=10_000_000,
            monthly_contribution=0,
            annual_return=0.03,
            annual_volatility=0.15,
            monthly_withdrawal=500_000,
            simulations=500,
            rng_seed=42,
        )
        assert result.depletion_probability > 0

    def test_inflation_reduces_final_balance(self):
        """インフレあり vs なし → インフレありの方が最終残高が低い。"""
        no_inflation = run_lifecycle_simulation(
            current_age=35,
            retirement_age=65,
            end_age=65,
            initial_investment=5_000_000,
            monthly_contribution=50_000,
            annual_return=0.05,
            annual_volatility=0.15,
            monthly_withdrawal=0,
            inflation_rate=0.0,
            simulations=500,
            rng_seed=42,
        )
        with_inflation = run_lifecycle_simulation(
            current_age=35,
            retirement_age=65,
            end_age=65,
            initial_investment=5_000_000,
            monthly_contribution=50_000,
            annual_return=0.05,
            annual_volatility=0.15,
            monthly_withdrawal=0,
            inflation_rate=0.02,
            simulations=500,
            rng_seed=42,
        )
        assert no_inflation.net_final > with_inflation.net_final

    def test_pension_reduces_depletion(self):
        """年金あり → 枯渇確率が低下。"""
        no_pension = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=90,
            initial_investment=20_000_000,
            monthly_contribution=0,
            annual_return=0.03,
            annual_volatility=0.15,
            monthly_withdrawal=300_000,
            pension_start_age=65,
            monthly_pension=0,
            simulations=500,
            rng_seed=42,
        )
        with_pension = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=90,
            initial_investment=20_000_000,
            monthly_contribution=0,
            annual_return=0.03,
            annual_volatility=0.15,
            monthly_withdrawal=300_000,
            pension_start_age=65,
            monthly_pension=150_000,
            simulations=500,
            rng_seed=42,
        )
        assert with_pension.depletion_probability <= no_pension.depletion_probability

    def test_tax_reduces_final_balance(self):
        """税金あり vs なし → 税金ありの方が最終残高が低い。"""
        no_tax = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=70,
            initial_investment=50_000_000,
            monthly_contribution=0,
            annual_return=0.05,
            annual_volatility=0.08,
            monthly_withdrawal=100_000,
            tax_rate=0.0,
            simulations=500,
            rng_seed=42,
        )
        with_tax = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=70,
            initial_investment=50_000_000,
            monthly_contribution=0,
            annual_return=0.05,
            annual_volatility=0.08,
            monthly_withdrawal=100_000,
            tax_rate=0.20315,
            simulations=500,
            rng_seed=42,
        )
        assert no_tax.net_final > with_tax.net_final
        assert with_tax.total_tax > 0

    def test_result_has_correct_percentiles(self):
        """yearly_balances に正しいパーセンタイルキーがある。"""
        result = run_lifecycle_simulation(
            current_age=35,
            retirement_age=65,
            end_age=70,
            initial_investment=5_000_000,
            monthly_contribution=50_000,
            annual_return=0.05,
            annual_volatility=0.15,
            monthly_withdrawal=200_000,
            simulations=200,
            rng_seed=42,
        )
        assert len(result.yearly_balances) == 35
        for yb in result.yearly_balances:
            assert "age" in yb
            assert "p10" in yb
            assert "p25" in yb
            assert "p50" in yb
            assert "p75" in yb
            assert "p90" in yb
            assert yb["p10"] <= yb["p25"] <= yb["p50"] <= yb["p75"] <= yb["p90"]

    def test_annual_event_expenses_reduce_final_balance(self):
        """年次イベント支出を与えると最終残高が減る。"""
        baseline = run_lifecycle_simulation(
            current_age=35,
            retirement_age=65,
            end_age=70,
            initial_investment=10_000_000,
            monthly_contribution=50_000,
            annual_return=0.05,
            annual_volatility=0.15,
            monthly_withdrawal=150_000,
            simulations=400,
            rng_seed=42,
        )
        with_events = run_lifecycle_simulation(
            current_age=35,
            retirement_age=65,
            end_age=70,
            initial_investment=10_000_000,
            monthly_contribution=50_000,
            annual_return=0.05,
            annual_volatility=0.15,
            monthly_withdrawal=150_000,
            annual_event_expenses={
                40: 2_000_000,
                45: 2_000_000,
                50: 2_000_000,
            },
            simulations=400,
            rng_seed=42,
        )
        assert with_events.net_final < baseline.net_final

    def test_reemployment_reduces_depletion_by_end_of_reemployment(self):
        """60歳定年・65歳まで再雇用収入あり → 65歳までの枯渇確率が再雇用なしより低い。"""
        common = dict(
            current_age=60,
            retirement_age=60,
            end_age=65,  # 65歳までの枯渇確率を直接測る
            initial_investment=10_000_000,
            monthly_contribution=0,
            annual_return=0.03,
            annual_volatility=0.15,
            monthly_withdrawal=300_000,
            pension_start_age=65,
            monthly_pension=150_000,
            simulations=500,
            rng_seed=42,
        )
        without = run_lifecycle_simulation(**common)
        with_reemployment = run_lifecycle_simulation(
            **common,
            reemployment_end_age=65,
            reemployment_monthly_income=200_000,
        )
        assert with_reemployment.depletion_probability < without.depletion_probability

    def test_reemployment_income_stops_after_end_age(self):
        """reemployment_end_age を過ぎると再雇用収入が計上されない（決定論的に検証）。"""
        # リターン0・ボラ0・インフレ0で決定論的に:
        # 60〜64歳: 取崩し10万 = 再雇用収入10万 → 残高変化なし
        # 65〜69歳: 収入0 → 毎年120万ずつ減少
        result = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=70,
            initial_investment=0,
            safe_value=12_000_000,
            monthly_contribution=0,
            annual_return=0.0,
            annual_volatility=0.0,
            monthly_withdrawal=100_000,
            pension_start_age=75,  # 期間中は年金なし
            monthly_pension=0,
            reemployment_end_age=65,
            reemployment_monthly_income=100_000,
            simulations=10,
            rng_seed=42,
        )
        balances = {yb["age"]: yb["p50"] for yb in result.yearly_balances}
        # 再雇用期間中（〜65歳の年末）は残高が維持される
        assert balances[61] == pytest.approx(12_000_000)
        assert balances[65] == pytest.approx(12_000_000)
        # 再雇用終了後は毎年120万ずつ減る（収入が0になった証拠）
        assert balances[66] == pytest.approx(10_800_000)
        assert balances[70] == pytest.approx(6_000_000)

    def test_reemployment_disabled_matches_two_phase_model(self):
        """reemployment_end_age 未設定 or retirement_age と同値なら従来の2段階モデルと同一結果。"""
        common = dict(
            current_age=55,
            retirement_age=60,
            end_age=90,
            initial_investment=15_000_000,
            monthly_contribution=50_000,
            annual_return=0.04,
            annual_volatility=0.15,
            monthly_withdrawal=250_000,
            pension_start_age=65,
            monthly_pension=150_000,
            simulations=300,
            rng_seed=42,
        )
        baseline = run_lifecycle_simulation(**common)
        # None（デフォルト）+ 収入指定 → 収入は無視される
        with_none = run_lifecycle_simulation(
            **common,
            reemployment_end_age=None,
            reemployment_monthly_income=200_000,
        )
        # retirement_age と同値 + 収入指定 → 再雇用期間ゼロなので無効
        with_same_age = run_lifecycle_simulation(
            **common,
            reemployment_end_age=60,
            reemployment_monthly_income=200_000,
        )
        assert with_none.net_final == pytest.approx(baseline.net_final)
        assert with_none.depletion_probability == baseline.depletion_probability
        assert with_same_age.net_final == pytest.approx(baseline.net_final)
        assert with_same_age.depletion_probability == baseline.depletion_probability

    def test_reemployment_can_overlap_pension(self):
        """再雇用期間と年金受給が重なっても計算できる（併給で残高が増える）。"""
        common = dict(
            current_age=60,
            retirement_age=60,
            end_age=75,
            initial_investment=10_000_000,
            monthly_contribution=0,
            annual_return=0.03,
            annual_volatility=0.15,
            monthly_withdrawal=250_000,
            pension_start_age=65,
            monthly_pension=150_000,
            simulations=300,
            rng_seed=42,
        )
        # 再雇用終了68歳 > 年金開始65歳（重複期間あり）
        overlap = run_lifecycle_simulation(
            **common,
            reemployment_end_age=68,
            reemployment_monthly_income=150_000,
        )
        no_overlap = run_lifecycle_simulation(
            **common,
            reemployment_end_age=65,
            reemployment_monthly_income=150_000,
        )
        assert overlap.depletion_probability < no_overlap.depletion_probability
        # 併給期間（65〜67歳）明けの残高中央値は、併給ありの方が高い
        overlap_p50 = {yb["age"]: yb["p50"] for yb in overlap.yearly_balances}
        no_overlap_p50 = {yb["age"]: yb["p50"] for yb in no_overlap.yearly_balances}
        assert overlap_p50[68] > no_overlap_p50[68]

    def test_annual_event_expenses_can_increase_tax(self):
        """イベント支出でリスク資産を売却する場合、税額が増える。"""
        baseline = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=70,
            initial_investment=40_000_000,
            monthly_contribution=0,
            annual_return=0.05,
            annual_volatility=0.08,
            monthly_withdrawal=50_000,
            safe_value=0,
            simulations=400,
            rng_seed=42,
        )
        with_events = run_lifecycle_simulation(
            current_age=60,
            retirement_age=60,
            end_age=70,
            initial_investment=40_000_000,
            monthly_contribution=0,
            annual_return=0.05,
            annual_volatility=0.08,
            monthly_withdrawal=50_000,
            safe_value=0,
            annual_event_expenses={65: 3_000_000},
            simulations=400,
            rng_seed=42,
        )
        assert with_events.total_tax > baseline.total_tax
