"""投資可能額 (#88) の受け入れテスト。"""

import json
from datetime import date
from io import BytesIO
from urllib.parse import urlencode

from src.db.repository import (
    calculate_investable_cash,
    create_life_event,
    get_setting,
    save_life_plan_inflation_rate,
    save_setting,
)
from src.db.schema import init_db
from src.web.server import Handler, _build_html, _build_settings_html, _demo_data, _get_data


def _build_test_db(tmp_path, cash: float = 5_000_000) -> str:
    db_path = tmp_path / "investable-cash.db"
    conn = init_db(str(db_path))
    conn.execute(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        ("2026-07-31", cash + 5_000_000, json.dumps({"預金・現金": cash, "投資信託": 5_000_000}), "fixture"),
    )
    save_setting(conn, "monthly_living_expense", "300000")
    save_setting(conn, "emergency_fund_months", "6")
    save_setting(conn, "planned_expense_horizon_months", "12")
    save_setting(conn, "additional_cash_reserve", "200000")
    save_life_plan_inflation_rate(conn, 0)
    conn.close()
    return str(db_path)


def test_calculates_cash_after_emergency_fund_events_and_reserve(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    create_life_event(conn, "one_time", "架空の予定支出", 500_000, 2027)

    result = calculate_investable_cash(conn, as_of=date(2026, 7, 31))

    conn.close()
    assert result["cash_balance"] == 5_000_000
    assert result["emergency_fund"] == 1_800_000
    assert result["planned_expenses"] == 500_000
    assert result["additional_reserve"] == 200_000
    assert result["investable_cash"] == 2_500_000
    assert result["formula"] == "cash - emergency_fund - planned_expenses - additional_reserve"


def test_excludes_events_outside_horizon(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    create_life_event(conn, "one_time", "期間外の予定支出", 900_000, 2028)

    result = calculate_investable_cash(conn, as_of=date(2026, 7, 31))

    conn.close()
    assert result["planned_expenses"] == 0
    assert result["investable_cash"] == 3_000_000


def test_investable_cash_never_becomes_negative(tmp_path):
    db_path = _build_test_db(tmp_path, cash=1_000_000)
    conn = init_db(db_path)

    result = calculate_investable_cash(conn, as_of=date(2026, 7, 31))

    conn.close()
    assert result["investable_cash"] == 0
    assert result["shortfall"] == 1_000_000


def test_settings_page_shows_inputs_and_current_calculation(tmp_path):
    db_path = _build_test_db(tmp_path)

    html = _build_settings_html(db_path)

    assert "投資可能額の計算" in html
    assert 'name="setting_type" value="investable_cash"' in html
    assert 'name="monthly_living_expense"' in html and 'value="300000"' in html
    assert 'name="emergency_fund_months"' in html and 'value="6"' in html
    assert "現在の投資可能額" in html


def test_dashboard_shows_investable_cash_card(tmp_path):
    db_path = _build_test_db(tmp_path)
    data = _get_data(db_path)

    html = _build_html(data, [data["date"]])

    assert data["investable_cash"]["investable_cash"] == 3_000_000
    assert 'data-card-id="dash-investable-cash"' in html
    assert "投資可能額" in html
    assert "生活防衛資金" in html


def test_demo_mode_has_investable_cash_card():
    data = _demo_data()
    html = _build_html(data, [data["date"]])

    assert "investable_cash" in data
    assert 'data-card-id="dash-investable-cash"' in html


def test_asset_consultation_prompt_includes_investable_cash(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)

    prompt = Handler._ai_prompt_asset(Handler.__new__(Handler), conn)

    conn.close()
    assert "## 投資可能額" in prompt
    assert "3,000,000円" in prompt
    assert "生活防衛資金" in prompt
    assert "投資信託・日本株・米国株・現金" in prompt


def test_settings_post_saves_investable_cash_conditions(tmp_path):
    db_path = _build_test_db(tmp_path)
    encoded = urlencode(
        {
            "setting_type": "investable_cash",
            "monthly_living_expense": "280000",
            "emergency_fund_months": "8",
            "planned_expense_horizon_months": "18",
            "additional_cash_reserve": "350000",
        }
    ).encode()
    handler = Handler.__new__(Handler)
    handler.path = "/settings"
    handler.db_path = db_path
    handler.rfile = BytesIO(encoded)
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": str(len(encoded)), "Host": "localhost"}
    response = {"status": None, "headers": {}}
    handler.send_response = lambda status: response.update(status=status)
    handler.send_header = lambda key, value: response["headers"].update({key: value})
    handler.end_headers = lambda: None

    handler.do_POST()

    conn = init_db(db_path)
    assert get_setting(conn, "monthly_living_expense") == "280000.0"
    assert get_setting(conn, "emergency_fund_months") == "8.0"
    assert get_setting(conn, "planned_expense_horizon_months") == "18"
    assert get_setting(conn, "additional_cash_reserve") == "350000.0"
    conn.close()
    assert response["status"] == 303
    assert response["headers"]["Location"] == "/settings?saved=investable_cash"
