"""dividend_fetcher の正規表現テスト（ネットワーク不要）。"""

from __future__ import annotations

from src.data import dividend_fetcher


def _fake_fetch(html: str, monkeypatch) -> tuple[float | None, str | None]:
    """fetch_dividend を urllib.request をモックして実行する。"""

    class _FakeResp:
        def __init__(self, body: str) -> None:
            self._body = body.encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self) -> bytes:
            return self._body

    def fake_urlopen(req, timeout=10):
        return _FakeResp(html)

    monkeypatch.setattr(dividend_fetcher.urllib.request, "urlopen", fake_urlopen)
    return dividend_fetcher.fetch_dividend("9999")


def test_fetch_dividend_new_format(monkeypatch):
    """新形式（JSON エスケープ付き）から dps と updateDate を抽出できる。"""
    html = (
        'foo bar \\"dps\\":{\\"name\\":\\"1株配当\\",\\"subText\\":\\"（予想）\\",'
        '\\"value\\":\\"84.00\\",\\"updateDate\\":\\"2027/03\\",'
        '\\"updateDateMeta\\":\\"2027-03-01\\",\\"suffix\\":\\"円\\"} baz'
    )
    dps, dps_date = _fake_fetch(html, monkeypatch)
    assert dps == 84.0
    assert dps_date == "2027/03"


def test_fetch_dividend_legacy_format(monkeypatch):
    """旧形式 ("dps":"...","dpsDate":"...") も引き続き読める。"""
    html = 'something "dps":"145","dpsDate":"2027/03" something'
    dps, dps_date = _fake_fetch(html, monkeypatch)
    assert dps == 145.0
    assert dps_date == "2027/03"


def test_fetch_dividend_missing_returns_none(monkeypatch):
    """ETF などで dps が掲載されていないページでは None を返す。"""
    html = "<html>no dividend data here</html>"
    dps, dps_date = _fake_fetch(html, monkeypatch)
    assert dps is None
    assert dps_date is None


def test_get_dividend_returns_none_when_missing(monkeypatch, tmp_path):
    """dividends.json に該当銘柄が無ければ None（ハードコードへのフォールバックは廃止）。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    # STOCK_MASTER に登録されている銘柄でも、dividends.json に無ければ None
    assert stock_master.get_dividend("9433") is None
    assert stock_master.get_dividend("AAPL") is None


def test_get_dividend_returns_none_when_dps_null(monkeypatch, tmp_path):
    """dividends.json で dps が null（取得不可マーカー）の場合は None を返す。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text('{"1478": {"dps": null, "date": null, "fetched": "2026-05-24"}}', encoding="utf-8")
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    assert stock_master.get_dividend("1478") is None


def test_get_dividend_returns_value_when_present(monkeypatch, tmp_path):
    """dividends.json に数値が入っていればそれを返す。"""
    from src.data import stock_master

    cache = tmp_path / "dividends.json"
    cache.write_text(
        '{"9433": {"dps": 84.0, "date": "2027/03", "fetched": "2026-05-24"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(stock_master, "_DIVIDENDS_JSON", cache)
    monkeypatch.setattr(stock_master, "_dividend_cache", None)
    assert stock_master.get_dividend("9433") == 84.0
