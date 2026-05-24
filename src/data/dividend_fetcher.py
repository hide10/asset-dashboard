"""配当データを自動取得する（多段フォールバック）。

取得経路の優先順:
    1. Yahoo Finance Japan（`1株配当` の予想値）
    2. minkabu（年間分配金。ETF 等で Yahoo に dps が無いケース向け）
    3. Yahoo Finance Japan で dps が `---` だった銘柄 → 無配確定として 0 を保存

使い方:
    python -m src.data.dividend_fetcher           # 全銘柄を取得
    python -m src.data.dividend_fetcher 5401 9433  # 指定銘柄のみ
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import date
from pathlib import Path

_YAHOO_URL = "https://finance.yahoo.co.jp/quote/{code}.T"
# 旧形式: "dps":"145","dpsDate":"2027/03"
_DPS_RE_LEGACY = re.compile(r'"dps":"(\d+(?:\.\d+)?)","dpsDate":"([^"]+)"')
# 新形式: \"dps\":{\"name\":\"1株配当\",...,\"value\":\"84.00\",\"updateDate\":\"2027/03\",...}
# JSON が HTML 内に文字列としてエスケープ埋め込みされている
_DPS_RE_NEW = re.compile(
    r'\\"dps\\":\{[^{}]*?\\"value\\":\\"(\d+(?:\.\d+)?)\\"'
    r'(?:[^{}]*?\\"updateDate\\":\\"([^"\\]+)\\")?'
)
# value=\"---\" は「予想未定／無配」を示す Yahoo の特殊表現
_DPS_RE_UNDEFINED = re.compile(r'\\"dps\\":\{[^{}]*?\\"value\\":\\"---\\"')

_MINKABU_URL = "https://minkabu.jp/stock/{code}"
# minkabu の銘柄ページの分配金行:
#   <th ...>分配金<span ...>（注6）</span></th>
#   <td ...><span class="fwb">110円</span><span class="fss">（年2回）</span></td>
# 配当金行（株式の場合）も同じ構造。
_MINKABU_DIV_RE = re.compile(
    r"<th[^>]*>(?:分配金|配当金|1株配当)[^<]*(?:<span[^>]*>[^<]*</span>)?</th>"
    r"\s*<td[^>]*>\s*<span[^>]*>([0-9.,]+)円</span>",
    re.DOTALL,
)

_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "dividends.json"


def _http_get(url: str) -> str | None:
    """HTML を取得する。失敗時 None。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8")
    except Exception:
        return None


def _fetch_yahoo_jp(code: str) -> tuple[float | None, str | None, str | None]:
    """Yahoo Finance Japan から1銘柄の年間予想配当を取得する。

    Returns:
        (dps, date, source):
          - 数値が取れた場合 (dps, date, "yahoo_jp")
          - dps=\"---\" の場合 (0.0, None, "yahoo_jp_undefined") — 無配確定として扱う
          - HTML 取得失敗・抽出不可 (None, None, None)
    """
    html = _http_get(_YAHOO_URL.format(code=code))
    if html is None:
        return None, None, None
    m = _DPS_RE_NEW.search(html)
    if m is None:
        m = _DPS_RE_LEGACY.search(html)
    if m is not None:
        return float(m.group(1)), m.group(2), "yahoo_jp"
    # value=\"---\"（無配確定）の検出
    if _DPS_RE_UNDEFINED.search(html):
        return 0.0, None, "yahoo_jp_undefined"
    return None, None, None


def _fetch_minkabu(code: str) -> tuple[float | None, str | None, str | None]:
    """minkabu から年間分配金/配当金を取得する。

    ETF の分配金や、Yahoo に dps が掲載されない銘柄向けのセカンダリ経路。

    Returns:
        (dps, date, source):
          - 取れた場合 (dps, None, "minkabu") — minkabu は権利確定日を出していないため date は None
          - 取れなければ (None, None, None)
    """
    html = _http_get(_MINKABU_URL.format(code=code))
    if html is None:
        return None, None, None
    m = _MINKABU_DIV_RE.search(html)
    if m is None:
        return None, None, None
    try:
        return float(m.group(1).replace(",", "")), None, "minkabu"
    except ValueError:
        return None, None, None


def fetch_dividend(code: str) -> tuple[float | None, str | None, str | None]:
    """多段フォールバックで配当を取得する。

    Returns:
        (dps, date, source):
          - source は "yahoo_jp" | "yahoo_jp_undefined" | "minkabu" | None
          - 全経路失敗時 (None, None, None)
    """
    dps, dps_date, source = _fetch_yahoo_jp(code)
    if dps is not None and source == "yahoo_jp":
        return dps, dps_date, source
    # Yahoo に dps が無い、または dps=\"---\"（無配） → minkabu を試す
    m_dps, m_date, m_source = _fetch_minkabu(code)
    if m_dps is not None:
        return m_dps, m_date, m_source
    # minkabu でも取れない → Yahoo の "---"（無配確定）情報があればそれを採用
    if source == "yahoo_jp_undefined":
        return 0.0, None, source
    return None, None, None


def update_all_dividends(codes: list[str] | None = None) -> dict:
    """全銘柄の配当を取得し dividends.json に保存する。"""
    if codes is None:
        from src.data.stock_master import get_all_codes

        codes = get_all_codes()

    # 既存データを読み込む（マージ用）
    data: dict = {}
    if _JSON_PATH.exists():
        data = json.loads(_JSON_PATH.read_text(encoding="utf-8"))

    today = date.today().isoformat()

    for i, code in enumerate(codes):
        print(f"  [{i + 1}/{len(codes)}] {code} ... ", end="", flush=True)
        try:
            dps, dps_date, source = fetch_dividend(code)
            if dps is not None:
                data[code] = {"dps": dps, "date": dps_date, "fetched": today, "source": source}
                date_str = f" ({dps_date})" if dps_date else ""
                print(f"{dps}円{date_str} [{source}]")
            else:
                # 全経路失敗 → null で保存。UI で「取得エラー」表示
                data[code] = {"dps": None, "date": None, "fetched": today, "source": None}
                print("取得不可（全経路失敗）")
        except Exception as e:
            print(f"エラー: {e}")
        if i < len(codes) - 1:
            time.sleep(1)

    _JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"\n保存: {_JSON_PATH}")

    # 業種情報も同時に取得・キャッシュ
    try:
        from src.data.stock_master import update_sectors

        update_sectors(codes)
    except Exception as e:
        print(f"[sector] 業種取得エラー: {e}")

    return data


if __name__ == "__main__":
    import sys

    codes = sys.argv[1:] if len(sys.argv) > 1 else None
    print("配当データ取得を開始します...\n")
    update_all_dividends(codes)
