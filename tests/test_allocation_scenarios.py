"""資産配分シナリオ比較 (#89) の受け入れテスト。"""

import json
from datetime import date
from io import BytesIO

import pytest

from src.db.repository import (
    ALLOCATION_PRESETS,
    calculate_allocation_scenario,
    get_allocation_context,
    save_life_plan_inflation_rate,
    save_setting,
)
from src.db.schema import init_db
from src.web.server import Handler, _build_allocation_html, _demo_allocation_data, _get_allocation_data


def _build_test_db(tmp_path) -> str:
    db_path = tmp_path / "allocation.db"
    conn = init_db(str(db_path))
    conn.execute(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        (
            "2026-07-31",
            10_000_000,
            json.dumps({"預金・現金": 4_000_000, "投資信託": 3_000_000, "株式(現物)": 3_000_000}),
            "fixture",
        ),
    )
    conn.executemany(
        """
        INSERT INTO snapshot_holdings
            (date, symbol_or_code, name, quantity, value, asset_class, position)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("2026-07-31", "1111", "架空日本株", 10, 2_000_000, "株式(現物)", 1),
            ("2026-07-31", "DEMO", "架空米国株", 10, 1_000_000, "株式(現物)", 2),
            ("2026-07-31", "F001", "架空投信", 1, 3_000_000, "投資信託", 3),
        ],
    )
    save_setting(conn, "monthly_living_expense", "250000")
    save_setting(conn, "emergency_fund_months", "6")
    save_setting(conn, "planned_expense_horizon_months", "12")
    save_setting(conn, "additional_cash_reserve", "0")
    save_life_plan_inflation_rate(conn, 0)
    conn.close()
    return str(db_path)


def test_allocation_scenario_conserves_total_assets(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    context = get_allocation_context(conn, as_of=date(2026, 7, 31))

    scenario = calculate_allocation_scenario(
        context,
        {"cash": 10, "fund": 50, "jp_stock": 25, "us_stock": 15},
        name="バランス",
    )

    conn.close()
    assert context["investable_cash"] == 2_500_000
    assert context["current_values"]["jp_stock"] == 2_000_000
    assert context["current_values"]["us_stock"] == 1_000_000
    assert scenario["allocation_amounts"] == {
        "cash": 250_000,
        "fund": 1_250_000,
        "jp_stock": 625_000,
        "us_stock": 375_000,
    }
    assert sum(scenario["post_values"].values()) == context["total_asset"]
    assert scenario["post_values"]["cash"] == 1_750_000
    assert scenario["post_values"]["fund"] == 4_250_000


def test_allocation_presets_all_total_100_percent():
    assert {preset["name"] for preset in ALLOCATION_PRESETS} == {"守り重視", "バランス", "成長重視"}
    assert all(sum(preset["allocation"].values()) == 100 for preset in ALLOCATION_PRESETS)


def test_allocation_scenario_rejects_invalid_total(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    context = get_allocation_context(conn, as_of=date(2026, 7, 31))
    conn.close()

    with pytest.raises(ValueError, match="100"):
        calculate_allocation_scenario(context, {"cash": 0, "fund": 50, "jp_stock": 20, "us_stock": 20})


def test_allocation_page_shows_presets_custom_form_and_post_values(tmp_path):
    data = _get_allocation_data(_build_test_db(tmp_path))

    html = _build_allocation_html(data)

    assert "余剰資金の配分を比較" in html
    for label in ["守り重視", "バランス", "成長重視", "現金", "投資信託", "日本株", "米国株"]:
        assert label in html
    assert 'data-testid="allocation-custom-form"' in html
    assert 'data-card-id="allocation-scenarios"' in html
    assert "購入後の構成" in html
    assert "2,500,000円" in html
    assert 'class="active">資産配分' in html


def test_allocation_ai_prompt_contains_scenarios_and_decision_request(tmp_path):
    db_path = _build_test_db(tmp_path)
    handler = Handler.__new__(Handler)
    handler.db_path = db_path

    prompt = handler._ai_prompt_allocation()

    assert "# 余剰資金の配分シナリオ" in prompt
    assert "守り重視" in prompt and "成長重視" in prompt
    assert "投資信託・日本株・米国株・現金" in prompt
    assert "2,500,000円" in prompt


def test_demo_allocation_page_is_available():
    data = _demo_allocation_data()
    html = _build_allocation_html(data)

    assert data["context"]["investable_cash"] > 0
    assert "余剰資金の配分を比較" in html


def test_allocation_route_applies_custom_query(tmp_path):
    handler = Handler.__new__(Handler)
    handler.path = "/allocation?cash=20&fund=40&jp_stock=25&us_stock=15"
    handler.db_path = _build_test_db(tmp_path)
    handler.demo = False
    handler.wfile = BytesIO()
    response = {"status": None, "headers": {}}
    handler.send_response = lambda status: response.update(status=status)
    handler.send_header = lambda key, value: response["headers"].update({key: value})
    handler.end_headers = lambda: None

    handler.do_GET()

    html = handler.wfile.getvalue().decode()
    assert response["status"] == 200
    for value in [20, 40, 25, 15]:
        assert f'value="{value}"' in html
    assert "購入後の構成" in html
