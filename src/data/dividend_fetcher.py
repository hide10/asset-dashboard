"""Yahoo Finance から配当データを自動取得する。

日本株: Yahoo Finance Japan からスクレイピング
米国株: Yahoo Finance US の quoteSummary API（crumb認証）

使い方:
    python -m src.data.dividend_fetcher              # 全銘柄（日本株+米国株）
    python -m src.data.dividend_fetcher 5401 9433    # 指定銘柄（日本株）
    python -m src.data.dividend_fetcher AAPL MSFT    # 指定銘柄（米国株）
"""

from __future__ import annotations

import contextlib
import gzip
import json
import logging
import re
import time
import urllib.request
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

_YAHOO_JP_URL = "https://finance.yahoo.co.jp/quote/{code}.T"
_DPS_RE = re.compile(r'"dps":"(\d+(?:\.\d+)?)","dpsDate":"([^"]+)"')
_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "dividends.json"

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


# ---------------------------------------------------------------------------
# 日本株
# ---------------------------------------------------------------------------


def fetch_dividend(code: str) -> tuple[float | None, str | None]:
    """Yahoo Finance Japan から1銘柄の年間予想配当を取得する。

    Returns:
        (dps, dps_date) — ETF等で未検出の場合は (None, None)
    """
    url = _YAHOO_JP_URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")
    m = _DPS_RE.search(html)
    if m is None:
        return None, None
    return float(m.group(1)), m.group(2)


# ---------------------------------------------------------------------------
# 米国株 (Yahoo Finance US — crumb 認証)
# ---------------------------------------------------------------------------

_yahoo_us_session: urllib.request.OpenerDirector | None = None
_yahoo_us_crumb: str | None = None


def _init_yahoo_us_session() -> tuple[urllib.request.OpenerDirector, str]:
    """Yahoo Finance US の cookie + crumb を取得する。"""
    global _yahoo_us_session, _yahoo_us_crumb
    if _yahoo_us_session and _yahoo_us_crumb:
        return _yahoo_us_session, _yahoo_us_crumb

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    # cookie を取得（404でもOK）
    with contextlib.suppress(Exception):
        opener.open(urllib.request.Request("https://fc.yahoo.com", headers={"User-Agent": _UA}), timeout=5)
    # crumb を取得
    crumb_req = urllib.request.Request("https://query2.finance.yahoo.com/v1/test/getcrumb", headers={"User-Agent": _UA})
    resp = opener.open(crumb_req, timeout=10)
    crumb = resp.read().decode("utf-8")

    _yahoo_us_session = opener
    _yahoo_us_crumb = crumb
    return opener, crumb


def fetch_us_dividend(ticker: str) -> tuple[float | None, str | None]:
    """Yahoo Finance US から1銘柄の年間配当（USD）を取得する。

    dividendRate（予想）を優先し、なければ trailingAnnualDividendRate（実績）を使う。

    Returns:
        (dps_usd, ex_dividend_date) — 取得失敗時は (None, None)
    """
    opener, crumb = _init_yahoo_us_session()
    url = f"https://query2.finance.yahoo.com/v10/finance/quoteSummary/{ticker}?modules=summaryDetail&crumb={crumb}"
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    resp = opener.open(req, timeout=10)
    raw = resp.read()
    with contextlib.suppress(Exception):
        raw = gzip.decompress(raw)
    data = json.loads(raw)

    results = data.get("quoteSummary", {}).get("result") or []
    if not results:
        logger.warning("[dividend] %s: quoteSummary result が空", ticker)
        return None, None
    detail = results[0].get("summaryDetail", {})
    # dividendRate（予想）を優先、ETF等では trailingAnnualDividendRate にフォールバック
    dps = detail.get("dividendRate", {}).get("raw", 0) or detail.get("trailingAnnualDividendRate", {}).get("raw", 0)
    ex_date = detail.get("exDividendDate", {}).get("fmt")
    return (dps or None, ex_date) if dps else (None, None)


def fetch_usd_jpy() -> float:
    """Yahoo Finance から USD/JPY レートを取得する。取得失敗時は 150.0 を返す。"""
    try:
        opener, crumb = _init_yahoo_us_session()
        url = f"https://query2.finance.yahoo.com/v8/finance/chart/USDJPY=X?range=1d&interval=1d&crumb={crumb}"
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        resp = opener.open(req, timeout=10)
        raw = resp.read()
        with contextlib.suppress(Exception):
            raw = gzip.decompress(raw)
        data = json.loads(raw)
        rate = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
        return float(rate)
    except Exception as e:
        logger.warning("[dividend] USD/JPY 取得失敗（150.0にフォールバック）: %s", e)
        return 150.0


# ---------------------------------------------------------------------------
# 統合
# ---------------------------------------------------------------------------


def update_all_dividends(codes: list[str] | None = None) -> dict:
    """全銘柄の配当を取得し dividends.json に保存する。

    codes が指定されない場合は STOCK_MASTER + US_STOCK_MASTER の全銘柄を取得する。
    """
    from src.data.stock_master import get_all_codes, get_all_us_codes, is_us_stock

    if codes is None:
        codes = get_all_codes() + get_all_us_codes()

    jp_codes = [c for c in codes if not is_us_stock(c)]
    us_codes = [c for c in codes if is_us_stock(c)]

    # 既存データを読み込む（マージ用）
    data: dict = {}
    if _JSON_PATH.exists():
        data = json.loads(_JSON_PATH.read_text(encoding="utf-8"))

    today = date.today().isoformat()

    # 日本株
    for i, code in enumerate(jp_codes):
        print(f"  [JP {i + 1}/{len(jp_codes)}] {code} ... ", end="", flush=True)
        try:
            dps, dps_date = fetch_dividend(code)
            if dps is not None:
                data[code] = {"dps": dps, "date": dps_date, "fetched": today, "currency": "JPY"}
                print(f"{dps}円 ({dps_date})")
            else:
                data[code] = {"dps": 0, "date": None, "fetched": today, "currency": "JPY"}
                print("0円（ETF等: dps未検出）")
        except Exception as e:
            print(f"エラー: {e}")
        if i < len(jp_codes) - 1:
            time.sleep(1)

    # 米国株
    if us_codes:
        usd_jpy = fetch_usd_jpy()
        print(f"\n  USD/JPY: {usd_jpy:.2f}")

        for i, ticker in enumerate(us_codes):
            print(f"  [US {i + 1}/{len(us_codes)}] {ticker} ... ", end="", flush=True)
            try:
                dps, ex_date = fetch_us_dividend(ticker)
                if dps is not None:
                    data[ticker] = {
                        "dps": dps,
                        "date": ex_date,
                        "fetched": today,
                        "currency": "USD",
                        "usd_jpy": usd_jpy,
                    }
                    dps_jpy = dps * usd_jpy
                    print(f"${dps:.2f} (≈{dps_jpy:,.0f}円, {ex_date or 'N/A'})")
                else:
                    data[ticker] = {"dps": 0, "date": None, "fetched": today, "currency": "USD", "usd_jpy": usd_jpy}
                    print("$0（配当なし）")
            except Exception as e:
                print(f"エラー: {e}")
            if i < len(us_codes) - 1:
                time.sleep(0.5)

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n保存: {_JSON_PATH}")

    # 業種情報も同時に取得・キャッシュ（日本株のみ）
    if jp_codes:
        try:
            from src.data.stock_master import update_sectors

            update_sectors(jp_codes)
        except Exception as e:
            print(f"[sector] 業種取得エラー: {e}")

    return data


if __name__ == "__main__":
    import sys

    codes = sys.argv[1:] if len(sys.argv) > 1 else None
    print("配当データ取得を開始します...\n")
    update_all_dividends(codes)
