from __future__ import annotations

import json
from datetime import date

from src.data import stock_master
from src.web.server import _needs_dividend_update


def _use_sector_cache(monkeypatch, tmp_path, data: str = "{}"):
    path = tmp_path / "sectors.json"
    path.write_text(data, encoding="utf-8")
    monkeypatch.setattr(stock_master, "_SECTORS_JSON", path)
    monkeypatch.setattr(stock_master, "_sector_cache", None)
    return path


def test_current_holdings_use_formal_sector_names(monkeypatch, tmp_path):
    _use_sector_cache(monkeypatch, tmp_path)

    assert stock_master.get_sector("8593") == "その他金融業"
    assert stock_master.get_sector("8316") == "銀行業"
    assert stock_master.get_sector("1478") == "ETF"


def test_formal_other_sector_names_are_accepted_from_cache(monkeypatch, tmp_path):
    _use_sector_cache(monkeypatch, tmp_path, '{"1001": "その他製品", "1002": "その他金融業"}')

    assert stock_master.get_sector("1001") == "その他製品"
    assert stock_master.get_sector("1002") == "その他金融業"


def test_us_sector_cache_remains_supported(monkeypatch, tmp_path):
    _use_sector_cache(monkeypatch, tmp_path, '{"AAPL": "情報技術"}')

    assert stock_master.get_sector("AAPL") == "情報技術"


def test_unknown_stock_is_explicitly_unclassified_without_network(monkeypatch, tmp_path):
    _use_sector_cache(monkeypatch, tmp_path)
    monkeypatch.setattr(stock_master, "_fetch_sector", lambda code: (_ for _ in ()).throw(AssertionError(code)))

    assert stock_master.get_sector("9999") == stock_master.UNCLASSIFIED_SECTOR


def test_update_sectors_accepts_only_jpx_33_and_preserves_existing(monkeypatch, tmp_path):
    path = _use_sector_cache(monkeypatch, tmp_path, '{"9998": "銀行業"}')
    fetched = {"9997": "その他製品", "9998": None, "9999": "独自カテゴリ"}
    monkeypatch.setattr(stock_master, "_fetch_sector", fetched.get)

    result = stock_master.update_sectors(["9997", "9998", "9999"])

    assert result == {"9998": "銀行業", "9997": "その他製品"}
    assert "独自カテゴリ" not in path.read_text(encoding="utf-8")


def test_dividend_update_requires_every_current_holding(tmp_path):
    path = tmp_path / "dividends.json"
    path.write_text(
        json.dumps({"8316": {"fetched": date.today().isoformat()}}),
        encoding="utf-8",
    )

    assert not _needs_dividend_update(["8316"], path)
    assert _needs_dividend_update(["8316", "8593"], path)
