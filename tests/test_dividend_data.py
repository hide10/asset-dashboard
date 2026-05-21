import json

from src.data import dividend_fetcher, stock_master


def test_parse_dividend_html_current_yahoo_text_format():
    html = """
    <html><body>
      <a><span>1株配当</span><span>（会社予想）</span></a>
      <span>用語</span>
      <span>140.00</span><span>円</span><span>(<!-- -->2026/03<!-- -->)</span>
      <span>PER （会社予想）</span>
    </body></html>
    """

    assert dividend_fetcher._parse_dividend_html(html) == (140.0, "2026/03")


def test_parse_dividend_html_json_format():
    html = '{"dps": "125.5", "dpsDate": "2026/03"}'

    assert dividend_fetcher._parse_dividend_html(html) == (125.5, "2026/03")


def test_get_dividend_falls_back_when_cached_zero_is_missing(monkeypatch, tmp_path):
    path = tmp_path / "dividends.json"
    path.write_text(json.dumps({"8053": {"dps": 0, "date": None, "fetched": "2026-04-22"}}), encoding="utf-8")
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", path)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)

    assert stock_master.get_dividend("8053") == 125


def test_get_dividend_keeps_explicit_zero_with_date(monkeypatch, tmp_path):
    path = tmp_path / "dividends.json"
    path.write_text(json.dumps({"8053": {"dps": 0, "date": "2026/03", "fetched": "2026-04-22"}}), encoding="utf-8")
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", path)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)

    assert stock_master.get_dividend("8053") == 0


def test_get_dividend_recomputes_stock_master_fallback_from_current_master(monkeypatch, tmp_path):
    path = tmp_path / "dividends.json"
    path.write_text(
        json.dumps({"4755": {"dps": 4.5, "date": None, "fetched": "2026-04-22", "source": "stock_master_fallback"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", path)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)

    assert stock_master.get_dividend("4755") == 0


def test_get_dividend_recomputes_etf_stock_master_fallback_from_current_master(monkeypatch, tmp_path):
    path = tmp_path / "dividends.json"
    path.write_text(
        json.dumps({"1478": {"dps": 500.0, "date": None, "fetched": "2026-04-22", "source": "stock_master_fallback"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", path)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)

    assert stock_master.get_dividend("1478") == 110.0


def test_keep_or_fallback_does_not_keep_stale_stock_master_fallback():
    data = {"4755": {"dps": 4.5, "date": None, "fetched": "2026-04-22", "source": "stock_master_fallback"}}

    message = dividend_fetcher._keep_or_fallback(data, "4755", "2026-04-22")

    assert message == "0円（dps未検出）"
    assert data["4755"] == {"dps": 0, "date": None, "fetched": "2026-04-22", "source": "not_found"}


def test_keep_or_fallback_keeps_existing_real_fetch_before_master_fallback():
    data = {"8053": {"dps": 140.0, "date": "2026/03", "fetched": "2026-04-22"}}

    message = dividend_fetcher._keep_or_fallback(data, "8053", "2026-04-23")

    assert message == "140.0円（dps未検出: 既存値を保持）"
    assert data["8053"] == {"dps": 140.0, "date": "2026/03", "fetched": "2026-04-22"}
