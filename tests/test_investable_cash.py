"""投資可能額 (#88) の受け入れテスト。"""

import json
from datetime import date
from io import BytesIO
from urllib.parse import urlencode

from src.db.repository import (
    calculate_investable_cash,
    create_life_event,
    create_scheduled_card_payment,
    disable_scheduled_card_payment,
    get_setting,
    list_scheduled_card_payments,
    replace_moneyforward_card_payments,
    save_life_plan_inflation_rate,
    save_setting,
)
from src.db.schema import init_db
from src.parser.card_schedule import ScheduledCardPayment
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
    assert (
        result["formula"] == "cash - emergency_fund - planned_expenses - scheduled_card_payments - additional_reserve"
    )


def test_calculates_scheduled_card_payment_within_horizon(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    create_scheduled_card_payment(conn, "2026-08-15", "カードA", 120_000, "銀行A", "8月利用分")

    result = calculate_investable_cash(conn, as_of=date(2026, 7, 31))

    conn.close()
    assert result["scheduled_card_payment_total"] == 120_000
    assert len(result["scheduled_card_payments"]) == 1
    assert result["investable_cash"] == 2_880_000


def test_scheduled_card_payment_ignores_past_outside_horizon_and_disabled(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    create_scheduled_card_payment(conn, "2026-07-30", "過去カード", 100_000)
    create_scheduled_card_payment(conn, "2028-01-15", "期間外カード", 200_000)
    disabled_id = create_scheduled_card_payment(conn, "2026-08-20", "無効カード", 300_000)
    disable_scheduled_card_payment(conn, disabled_id)

    result = calculate_investable_cash(conn, as_of=date(2026, 7, 31))

    conn.close()
    assert result["scheduled_card_payment_total"] == 0
    assert result["scheduled_card_payments"] == []


def test_moneyforward_refresh_replaces_only_automatic_rows(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    manual_id = create_scheduled_card_payment(conn, "2026-08-20", "手入力カード", 10_000)
    replace_moneyforward_card_payments(
        conn,
        [
            ScheduledCardPayment(
                due_date=date(2026, 8, 10),
                card_name="自動カード",
                amount=20_000,
                external_id="/accounts/show/card#0",
            )
        ],
        "2026-08-01T07:00:00",
    )
    replace_moneyforward_card_payments(
        conn,
        [
            ScheduledCardPayment(
                due_date=date(2026, 8, 11),
                card_name="自動カード",
                amount=30_000,
                external_id="/accounts/show/card#0",
            )
        ],
        "2026-08-02T07:00:00",
    )

    payments = list_scheduled_card_payments(conn, include_disabled=True)
    conn.close()

    assert manual_id in {row["id"] for row in payments}
    auto = [row for row in payments if row["source"] == "moneyforward"]
    assert len(auto) == 1
    assert auto[0]["due_date"] == "2026-08-11"
    assert auto[0]["amount"] == 30_000
    assert auto[0]["fetched_at"] == "2026-08-02T07:00:00"


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
    assert 'data-testid="scheduled-card-payments"' in html
    assert 'name="scheduled_due_date"' in html


def test_dashboard_shows_investable_cash_card(tmp_path):
    db_path = _build_test_db(tmp_path)
    data = _get_data(db_path)

    html = _build_html(data, [data["date"]])

    assert data["investable_cash"]["investable_cash"] == 3_000_000
    assert 'data-card-id="dash-investable-cash"' in html
    assert "投資可能額" in html
    assert "生活防衛資金" in html


def test_dashboard_shows_scheduled_card_payment_deduction(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    create_scheduled_card_payment(conn, "2026-08-15", "カードA", 120_000)
    conn.close()

    data = _get_data(db_path)
    html = _build_html(data, [data["date"]])

    assert data["investable_cash"]["scheduled_card_payment_total"] == 120_000
    assert "カード引落予定 120,000円" in html


def test_settings_marks_moneyforward_rows_as_automatic(tmp_path):
    db_path = _build_test_db(tmp_path)
    conn = init_db(db_path)
    replace_moneyforward_card_payments(
        conn,
        [
            ScheduledCardPayment(
                due_date=date(2026, 8, 10),
                card_name="自動カード",
                amount=20_000,
                external_id="/accounts/show/card#0",
            )
        ],
        "2026-08-01T07:00:00",
    )
    save_setting(conn, "moneyforward_card_schedule_last_fetch_at", "2026-08-01T07:00:00")
    conn.close()

    html = _build_settings_html(db_path)

    assert "MoneyForwardから最終取得: 2026-08-01T07:00:00" in html
    assert "MoneyForward自動" in html
    assert "自動更新" in html


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


def test_settings_post_adds_and_disables_scheduled_card_payment(tmp_path):
    db_path = _build_test_db(tmp_path)
    encoded = urlencode(
        {
            "setting_type": "scheduled_card_payment",
            "scheduled_action": "add",
            "scheduled_due_date": "2026-08-20",
            "scheduled_card_name": "カードA",
            "scheduled_amount": "123000",
            "scheduled_withdrawal_account": "銀行A",
            "scheduled_memo": "8月利用分",
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
    payments = list_scheduled_card_payments(conn)
    assert len(payments) == 1
    assert payments[0]["card_name"] == "カードA"
    assert payments[0]["amount"] == 123000
    payment_id = payments[0]["id"]
    conn.close()
    assert response["status"] == 303
    assert response["headers"]["Location"] == "/settings?saved=scheduled_card_payment"

    disable_encoded = urlencode(
        {
            "setting_type": "scheduled_card_payment",
            "scheduled_action": "disable",
            "scheduled_payment_id": str(payment_id),
        }
    ).encode()
    handler.rfile = BytesIO(disable_encoded)
    handler.headers = {"Content-Length": str(len(disable_encoded)), "Host": "localhost"}
    handler.do_POST()

    conn = init_db(db_path)
    assert list_scheduled_card_payments(conn) == []
    assert list_scheduled_card_payments(conn, include_disabled=True)[0]["enabled"] is False
    conn.close()
