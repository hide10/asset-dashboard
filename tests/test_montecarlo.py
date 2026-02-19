"""montecarlo.py のテスト。"""

from __future__ import annotations

import pytest

from src.prediction.montecarlo import _estimate_params, weighted_default_params


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
