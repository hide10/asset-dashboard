"""ai_comment.py のテスト。"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import patch

from src.analysis import ai_comment
from src.prediction.montecarlo import PredictionRange


def test_build_lifeplan_prompt_uses_explicit_prediction_args(monkeypatch, tmp_path):
    db_path = tmp_path / "assets.db"
    conn = ai_comment.init_db(str(db_path))
    conn.execute(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        (
            "2026-02-14",
            10_000_000,
            json.dumps({"株式（現物）": 4_000_000, "投資信託": 2_000_000, "預金・現金・暗号資産": 4_000_000}),
            "dummy.json",
        ),
    )
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)", ("monthly_contribution", "75000"))
    conn.commit()
    conn.close()

    called: dict[str, dict] = {}

    def fake_no_contribution(db_path_arg, risk_value, safe_value, **kwargs):
        called["no"] = {
            "db_path": db_path_arg,
            "risk_value": risk_value,
            "safe_value": safe_value,
            **kwargs,
        }
        return [PredictionRange(years=1, p10=1, p50=2, p90=3)], {"is_estimated": True}

    def fake_with_contribution(db_path_arg, risk_value, safe_value, monthly_contribution, **kwargs):
        called["with"] = {
            "db_path": db_path_arg,
            "risk_value": risk_value,
            "safe_value": safe_value,
            "monthly_contribution": monthly_contribution,
            **kwargs,
        }
        return [PredictionRange(years=1, p10=1, p50=2, p90=3)], {"is_estimated": True}

    monkeypatch.setattr("src.prediction.montecarlo.predict_no_contribution", fake_no_contribution)
    monkeypatch.setattr("src.prediction.montecarlo.predict_with_contribution", fake_with_contribution)

    prompt = ai_comment._build_lifeplan_prompt(str(db_path), "2026-02-14")

    assert "ライフプランデータ" in prompt
    assert called["no"]["years_list"] == [1, 3, 5]
    assert called["no"]["simulations"] == 2000
    assert called["with"]["years_list"] == [1, 3, 5]
    assert called["with"]["simulations"] == 2000
    assert called["with"]["monthly_contribution"] == 75000.0


def _setup_cf_db(db_path, year_month="2026-03"):
    """家計簿テスト用のDBセットアップ。"""
    conn = ai_comment.init_db(str(db_path))
    conn.execute(
        "INSERT INTO cf_transactions (id, year_month, date, description, amount, major_category, minor_category, fetched) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t1", year_month, f"{year_month}-05", "スーパー", -5000, "食費", "食料品", "2026-03-06"),
    )
    conn.execute(
        "INSERT INTO cf_transactions (id, year_month, date, description, amount, major_category, minor_category, fetched) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("t2", year_month, f"{year_month}-03", "給与", 300000, "収入", "給与", "2026-03-06"),
    )
    conn.commit()
    conn.close()
    return str(db_path)


def test_build_cf_prompt_includes_elapsed_days_for_current_month(tmp_path):
    """当月データの場合、プロンプトに経過日数・経過率が含まれること（closing_day=1）。"""
    db_path = _setup_cf_db(tmp_path / "assets.db", "2026-03")

    # date.today() を 3月10日に固定、fiscal month も "2026-03"
    fake_today = date(2026, 3, 10)
    with (
        patch("src.analysis.ai_comment.date") as mock_date,
        patch("src.analysis.ai_comment._current_fiscal_month", return_value="2026-03"),
    ):
        mock_date.today.return_value = fake_today
        mock_date.fromisoformat = date.fromisoformat
        prompt = ai_comment._build_cf_prompt(db_path, "2026-03")

    assert "月途中のデータ" in prompt
    assert "10日目" in prompt
    assert "31日中" in prompt
    elapsed_pct = round(10 / 31 * 100)
    assert f"経過率{elapsed_pct}%" in prompt
    assert "日割りペース" in prompt


def test_build_cf_prompt_past_month_is_confirmed(tmp_path):
    """過去月データの場合、月末確定データとして扱われること。"""
    db_path = _setup_cf_db(tmp_path / "assets.db", "2026-01")

    fake_today = date(2026, 3, 10)
    with (
        patch("src.analysis.ai_comment.date") as mock_date,
        patch("src.analysis.ai_comment._current_fiscal_month", return_value="2026-03"),
    ):
        mock_date.today.return_value = fake_today
        mock_date.fromisoformat = date.fromisoformat
        prompt = ai_comment._build_cf_prompt(db_path, "2026-01")

    assert "月末確定データ" in prompt
    assert "月途中" not in prompt
    assert "日割りペース" not in prompt


def test_build_cf_prompt_closing_day_25(tmp_path):
    """締め日25日の場合、fiscal month の期間で経過日数が計算されること。"""
    # closing_day=25: "2026-03" の期間は 2026-02-25 〜 2026-03-24（28日間）
    db_path = _setup_cf_db(tmp_path / "assets.db", "2026-03")
    conn = ai_comment.init_db(db_path)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('closing_day', '25')")
    conn.commit()
    conn.close()

    # 今日=3/10、fiscal month="2026-03"、期間=2/25〜3/24
    # 経過日数 = 3/10 - 2/25 + 1 = 14日目
    fake_today = date(2026, 3, 10)
    with (
        patch("src.analysis.ai_comment.date") as mock_date,
        patch("src.analysis.ai_comment._current_fiscal_month", return_value="2026-03"),
    ):
        mock_date.today.return_value = fake_today
        mock_date.fromisoformat = date.fromisoformat
        prompt = ai_comment._build_cf_prompt(db_path, "2026-03")

    assert "月途中のデータ" in prompt
    assert "14日目" in prompt
    assert "28日中" in prompt
    assert "2026-02-25" in prompt
    assert "2026-03-24" in prompt
    elapsed_pct = round(14 / 28 * 100)
    assert f"経過率{elapsed_pct}%" in prompt
