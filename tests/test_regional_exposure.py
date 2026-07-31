"""投資信託・年金の地域エクスポージャー (#87) の受け入れテスト。"""

import json
import re

import pytest

from src.db.repository import (
    get_portfolio_regional_exposure,
    get_regional_exposure_config,
    save_regional_exposure_config,
)
from src.db.schema import init_db
from src.regional_exposure import is_regional_exposure_applicable, suggest_regional_exposure
from src.web.server import _build_settings_html


def _build_test_db(tmp_path) -> tuple[str, str, str]:
    db_path = tmp_path / "regional-exposure.db"
    conn = init_db(str(db_path))
    conn.execute(
        "INSERT INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        ("2026-01-16", 6_000_000, json.dumps({"投資信託": 4_000_000, "年金": 2_000_000}), "fixture"),
    )
    conn.executemany(
        """
        INSERT INTO snapshot_holdings
            (date, symbol_or_code, name, quantity, value, asset_class, position)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("2026-01-16", "F001", "架空世界投信", 1, 4_000_000, "投資信託", 1),
            ("2026-01-16", "P001", "架空年金商品", 1, 2_000_000, "年金", 2),
        ],
    )
    conn.commit()
    conn.close()
    return str(db_path), "投資信託|F001|架空世界投信", "年金|P001|架空年金商品"


def test_regional_exposure_config_round_trip_and_aggregation(tmp_path):
    db_path, fund_key, pension_key = _build_test_db(tmp_path)
    conn = init_db(db_path)
    config = {
        fund_key: {"日本": 10, "米国": 60, "先進国（日本・米国除く）": 20, "新興国": 10, "その他": 0},
        pension_key: {"日本": 100, "米国": 0, "先進国（日本・米国除く）": 0, "新興国": 0, "その他": 0},
    }

    save_regional_exposure_config(conn, config)

    assert get_regional_exposure_config(conn) == config
    exposure = get_portfolio_regional_exposure(conn)
    conn.close()
    assert exposure["as_of"] == "2026-01-16"
    assert exposure["by_region"] == {
        "日本": 2_400_000,
        "米国": 2_400_000,
        "先進国（日本・米国除く）": 800_000,
        "新興国": 400_000,
        "その他": 0,
    }
    assert exposure["unconfigured"] == []


def test_regional_exposure_rejects_allocation_not_totaling_100(tmp_path):
    db_path, fund_key, _ = _build_test_db(tmp_path)
    conn = init_db(db_path)
    with pytest.raises(ValueError, match="100"):
        save_regional_exposure_config(conn, {fund_key: {"日本": 40, "米国": 40}})
    conn.close()


def test_regional_exposure_keeps_unconfigured_holdings_explicit(tmp_path):
    db_path, fund_key, _ = _build_test_db(tmp_path)
    conn = init_db(db_path)
    save_regional_exposure_config(
        conn,
        {fund_key: {"日本": 0, "米国": 100, "先進国（日本・米国除く）": 0, "新興国": 0, "その他": 0}},
    )
    exposure = get_portfolio_regional_exposure(conn)
    conn.close()

    assert exposure["configured_value"] == 4_000_000
    assert exposure["unconfigured_value"] == 2_000_000
    assert exposure["unconfigured"] == [{"key": "年金|P001|架空年金商品", "name": "架空年金商品", "value": 2_000_000}]


def test_settings_page_has_regional_exposure_inputs(tmp_path):
    db_path, fund_key, _ = _build_test_db(tmp_path)
    conn = init_db(db_path)
    save_regional_exposure_config(
        conn,
        {fund_key: {"日本": 10, "米国": 60, "先進国（日本・米国除く）": 20, "新興国": 10, "その他": 0}},
    )
    conn.close()

    html = _build_settings_html(db_path)

    assert "投信・年金の地域配分" in html
    assert "架空世界投信" in html
    assert "架空年金商品" in html
    for region in ["日本", "米国", "先進国（日本・米国除く）", "新興国", "その他"]:
        assert region in html
    assert 'name="setting_type" value="regional_exposure"' in html
    assert 'value="60"' in html


@pytest.mark.parametrize(
    ("name", "region", "confidence"),
    [
        ("架空 国内株式インデックス", "日本", "high"),
        ("架空 S&P500 インデックス", "米国", "high"),
        ("架空 全米株式インデックス", "米国", "high"),
        ("架空 新興国株式", "新興国", "high"),
        ("架空 オールカントリー", "米国", "estimate"),
        ("架空 外国株式", "米国", "estimate"),
        ("架空 グローバル株式", "米国", "estimate"),
    ],
)
def test_regional_exposure_suggestion_uses_explicit_product_words(name, region, confidence):
    suggestion = suggest_regional_exposure(name)

    assert suggestion is not None
    assert suggestion.allocation[region] > 0
    assert sum(suggestion.allocation.values()) == 100
    assert suggestion.confidence == confidence


def test_regional_exposure_suggestion_leaves_ambiguous_product_unconfigured():
    assert suggest_regional_exposure("架空 バランスファンド") is None


@pytest.mark.parametrize("name", ["架空積立保険", "架空じぶん積立"])
def test_non_market_savings_products_are_not_regional_exposure_targets(name):
    assert not is_regional_exposure_applicable(name)


def test_market_products_with_tsumitate_in_name_remain_targets():
    assert is_regional_exposure_applicable("架空積立外国株式インデックス")


def test_settings_page_prefills_suggestions_and_marks_ambiguous_products(tmp_path):
    db_path, _, _ = _build_test_db(tmp_path)
    conn = init_db(db_path)
    conn.execute(
        "UPDATE snapshot_holdings SET name = ? WHERE symbol_or_code = ?",
        ("架空 S&P500 インデックス", "F001"),
    )
    conn.commit()
    conn.close()

    html = _build_settings_html(db_path)

    assert "1件を自動提案" in html
    assert "商品名に米国または米国指数を明記" in html
    assert re.search(r'name="region_\d+_us" value="100"', html)
    assert "判別できないため確認が必要" in html
