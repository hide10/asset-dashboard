"""stock_master.py / dividend_fetcher.py のテスト。"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from src.data.stock_master import (
    get_dividend,
    get_sector,
    get_usd_jpy,
    is_us_stock,
)


class TestIsUsStock:
    def test_japanese_stock(self):
        assert is_us_stock("5401") is False

    def test_us_ticker(self):
        assert is_us_stock("AAPL") is True

    def test_single_letter(self):
        assert is_us_stock("V") is True

    def test_five_letters(self):
        assert is_us_stock("GOOGL") is True

    def test_lowercase(self):
        assert is_us_stock("aapl") is False

    def test_empty(self):
        assert is_us_stock("") is False

    def test_none(self):
        assert is_us_stock(None) is False


class TestGetSector:
    def test_japanese_stock_master(self):
        assert get_sector("8053") == "卸売業"

    def test_us_stock_master(self):
        assert get_sector("AAPL") == "テクノロジー"

    def test_unknown_us_stock(self):
        assert get_sector("XYZW") == "米国株"


class TestGetDividend:
    def test_japanese_stock(self):
        assert get_dividend("8053") > 0

    def test_us_stock(self):
        assert get_dividend("AAPL") > 0

    def test_unknown_us_stock(self):
        assert get_dividend("UNKNOWN") == 0.0


class TestGetUsdJpy:
    def test_fallback_when_no_data(self):
        """dividends.json に USD データがなければ 150.0 を返す。"""
        with patch("src.data.stock_master._load_dividends", return_value={}):
            assert get_usd_jpy() == 150.0

    def test_reads_from_dividends(self):
        """dividends.json に usd_jpy が保存されていればそれを返す。"""
        mock_data = {
            "AAPL": {"dps": 1.0, "currency": "USD", "usd_jpy": 155.5},
        }
        with patch("src.data.stock_master._load_dividends", return_value=mock_data):
            assert get_usd_jpy() == 155.5


class TestFetchUsDividend:
    def test_fetch_us_dividend_success(self):
        """Yahoo Finance US API から配当データが取得できること。"""
        from src.data.dividend_fetcher import fetch_us_dividend

        mock_response_data = {
            "quoteSummary": {
                "result": [
                    {
                        "summaryDetail": {
                            "dividendRate": {"raw": 1.04},
                            "trailingAnnualDividendRate": {"raw": 1.03},
                            "exDividendDate": {"fmt": "2026-02-09"},
                        }
                    }
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode()

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with (
            patch("src.data.dividend_fetcher._yahoo_us_session", mock_opener),
            patch("src.data.dividend_fetcher._yahoo_us_crumb", "test_crumb"),
        ):
            dps, ex_date = fetch_us_dividend("AAPL")
            assert dps == 1.04
            assert ex_date == "2026-02-09"

    def test_fetch_us_dividend_etf_trailing(self):
        """ETF: dividendRate=0 の場合に trailingAnnualDividendRate を使うこと。"""
        from src.data.dividend_fetcher import fetch_us_dividend

        mock_response_data = {
            "quoteSummary": {
                "result": [
                    {
                        "summaryDetail": {
                            "dividendRate": {"raw": 0},
                            "trailingAnnualDividendRate": {"raw": 5.44},
                            "exDividendDate": {"fmt": "2026-03-01"},
                        }
                    }
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode()

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with (
            patch("src.data.dividend_fetcher._yahoo_us_session", mock_opener),
            patch("src.data.dividend_fetcher._yahoo_us_crumb", "test_crumb"),
        ):
            dps, ex_date = fetch_us_dividend("VOO")
            assert dps == 5.44

    def test_fetch_us_dividend_no_dividend(self):
        """無配当銘柄の場合 (None, None) を返すこと。"""
        from src.data.dividend_fetcher import fetch_us_dividend

        mock_response_data = {
            "quoteSummary": {
                "result": [
                    {
                        "summaryDetail": {
                            "dividendRate": {},
                            "trailingAnnualDividendRate": {},
                        }
                    }
                ]
            }
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode()

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with (
            patch("src.data.dividend_fetcher._yahoo_us_session", mock_opener),
            patch("src.data.dividend_fetcher._yahoo_us_crumb", "test_crumb"),
        ):
            dps, ex_date = fetch_us_dividend("TSLA")
            assert dps is None
            assert ex_date is None


class TestFetchUsdJpy:
    def test_fetch_usd_jpy_success(self):
        """Yahoo Finance から USD/JPY レートを取得できること。"""
        from src.data.dividend_fetcher import fetch_usd_jpy

        mock_response_data = {"chart": {"result": [{"meta": {"regularMarketPrice": 157.5}}]}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode()

        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp

        with (
            patch("src.data.dividend_fetcher._yahoo_us_session", mock_opener),
            patch("src.data.dividend_fetcher._yahoo_us_crumb", "test_crumb"),
        ):
            rate = fetch_usd_jpy()
            assert rate == 157.5

    def test_fetch_usd_jpy_fallback(self):
        """取得失敗時は 150.0 を返すこと。"""
        from src.data.dividend_fetcher import fetch_usd_jpy

        with (
            patch("src.data.dividend_fetcher._yahoo_us_session", None),
            patch("src.data.dividend_fetcher._yahoo_us_crumb", None),
            patch("src.data.dividend_fetcher._init_yahoo_us_session", side_effect=Exception("network error")),
        ):
            rate = fetch_usd_jpy()
            assert rate == 150.0
