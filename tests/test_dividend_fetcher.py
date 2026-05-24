"""dividend_fetcher の単体テスト（ネットワーク不要）。"""

from __future__ import annotations

from src.data import dividend_fetcher


def _make_fake_get(yahoo_html: str | None, minkabu_html: str | None):
    """`_http_get` を URL ごとに別 HTML を返すフェイクに置き換えるヘルパ。"""

    def fake_get(url: str) -> str | None:
        if "minkabu" in url:
            return minkabu_html
        return yahoo_html

    return fake_get


def _fetch(monkeypatch, yahoo_html=None, minkabu_html=None):
    monkeypatch.setattr(dividend_fetcher, "_http_get", _make_fake_get(yahoo_html, minkabu_html))
    return dividend_fetcher.fetch_dividend("9999")


# --- Yahoo Finance Japan の DPS 抽出 ---


def test_yahoo_new_format(monkeypatch):
    """新形式（JSON エスケープ付き）から dps と updateDate を抽出。"""
    yahoo = (
        'foo \\"dps\\":{\\"name\\":\\"1株配当\\",\\"subText\\":\\"（予想）\\",'
        '\\"value\\":\\"84.00\\",\\"updateDate\\":\\"2027/03\\",'
        '\\"updateDateMeta\\":\\"2027-03-01\\",\\"suffix\\":\\"円\\"} bar'
    )
    dps, dps_date, source = _fetch(monkeypatch, yahoo_html=yahoo)
    assert dps == 84.0
    assert dps_date == "2027/03"
    assert source == "yahoo_jp"


def test_yahoo_legacy_format(monkeypatch):
    """旧形式 ("dps":"...","dpsDate":"...") も引き続き読める。"""
    yahoo = 'something "dps":"145","dpsDate":"2027/03" something'
    dps, dps_date, source = _fetch(monkeypatch, yahoo_html=yahoo)
    assert dps == 145.0
    assert dps_date == "2027/03"
    assert source == "yahoo_jp"


# --- "---" 検知（無配確定） ---


def test_yahoo_undefined_with_no_minkabu(monkeypatch):
    """Yahoo dps=\"---\" + minkabu でも取れない → 無配確定として 0 円。"""
    yahoo = '\\"dps\\":{\\"name\\":\\"1株配当\\",\\"value\\":\\"---\\",\\"updateDate\\":\\"2026/12\\"}'
    minkabu = "<html>no dividend</html>"
    dps, dps_date, source = _fetch(monkeypatch, yahoo_html=yahoo, minkabu_html=minkabu)
    assert dps == 0.0
    assert dps_date is None
    assert source == "yahoo_jp_undefined"


# --- minkabu フォールバック ---


def test_minkabu_fallback_when_yahoo_missing(monkeypatch):
    """Yahoo で dps が見つからず、minkabu に分配金がある → minkabu を採用。"""
    yahoo = "<html>no dps json here</html>"
    minkabu = (
        '<th class="x">分配金<span class="fss">（注6）</span></th>'
        '\n<td class="y"><span class="fwb">110円</span><span class="fss">（年2回）</span></td>'
    )
    dps, dps_date, source = _fetch(monkeypatch, yahoo_html=yahoo, minkabu_html=minkabu)
    assert dps == 110.0
    assert dps_date is None
    assert source == "minkabu"


def test_minkabu_overrides_yahoo_undefined(monkeypatch):
    """Yahoo dps=\"---\" だが minkabu に値がある → minkabu を優先採用。"""
    yahoo = '\\"dps\\":{\\"value\\":\\"---\\"}'
    minkabu = '<th>分配金</th>\n<td><span class="fwb">110円</span>'
    dps, dps_date, source = _fetch(monkeypatch, yahoo_html=yahoo, minkabu_html=minkabu)
    assert dps == 110.0
    assert source == "minkabu"


def test_minkabu_with_comma_value(monkeypatch):
    """minkabu 側がカンマ付き数値（1,234円）でも数値化できる。"""
    yahoo = "<html>none</html>"
    minkabu = '<th>配当金</th>\n<td><span class="fwb">1,234円</span>'
    dps, _, source = _fetch(monkeypatch, yahoo_html=yahoo, minkabu_html=minkabu)
    assert dps == 1234.0
    assert source == "minkabu"


# --- 優先順 ---


def test_yahoo_takes_precedence_over_minkabu(monkeypatch):
    """Yahoo で数値が取れたら minkabu は呼ばれない（呼ばれても結果は Yahoo 優先）。"""
    yahoo = '\\"dps\\":{\\"name\\":\\"1株配当\\",\\"value\\":\\"84.00\\",\\"updateDate\\":\\"2027/03\\"}'
    minkabu = '<th>配当金</th>\n<td><span class="fwb">999円</span>'
    dps, _, source = _fetch(monkeypatch, yahoo_html=yahoo, minkabu_html=minkabu)
    assert dps == 84.0
    assert source == "yahoo_jp"


def test_minkabu_not_called_when_yahoo_succeeds(monkeypatch):
    """Yahoo で成功すれば minkabu の HTTP は走らない（外部リクエスト数を最小化）。"""
    calls: list[str] = []

    def tracking_get(url: str) -> str | None:
        calls.append(url)
        if "yahoo" in url:
            return '\\"dps\\":{\\"value\\":\\"84.00\\",\\"updateDate\\":\\"2027/03\\"}'
        return None

    monkeypatch.setattr(dividend_fetcher, "_http_get", tracking_get)
    dividend_fetcher.fetch_dividend("9999")
    assert any("yahoo" in u for u in calls)
    assert not any("minkabu" in u for u in calls), f"minkabu was called: {calls}"


# --- 全経路失敗 ---


def test_all_paths_fail_returns_none(monkeypatch):
    """Yahoo に dps なし・"---" なし・minkabu にも無し → 全 None。"""
    dps, dps_date, source = _fetch(
        monkeypatch,
        yahoo_html="<html>nothing here</html>",
        minkabu_html="<html>nothing here either</html>",
    )
    assert dps is None
    assert dps_date is None
    assert source is None


# --- get_dividend（呼び出し側）の挙動 ---


def test_get_dividend_returns_none_when_missing(monkeypatch, tmp_path):
    """dividends.json に該当銘柄が無ければ None。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    assert stock_master.get_dividend("9433") is None
    assert stock_master.get_dividend("AAPL") is None


def test_get_dividend_returns_none_when_dps_null(monkeypatch, tmp_path):
    """dividends.json の dps が null（全経路失敗マーカー）→ None。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text(
        '{"1478": {"dps": null, "date": null, "fetched": "2026-05-24", "source": null}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    assert stock_master.get_dividend("1478") is None


def test_get_dividend_returns_zero_when_explicit_zero(monkeypatch, tmp_path):
    """dps: 0（無配確定）はそのまま 0.0 を返す（None と区別する）。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text(
        '{"4755": {"dps": 0, "date": null, "fetched": "2026-05-24", "source": "yahoo_jp_undefined"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    assert stock_master.get_dividend("4755") == 0.0


def test_get_dividend_returns_value_when_present(monkeypatch, tmp_path):
    """通常の数値はそのまま返す。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text(
        '{"9433": {"dps": 84.0, "date": "2027/03", "fetched": "2026-05-24", "source": "yahoo_jp"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    assert stock_master.get_dividend("9433") == 84.0
