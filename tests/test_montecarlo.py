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
