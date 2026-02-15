"""Yahoo Finance Japan から配当データを自動取得する。

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
_DPS_RE = re.compile(r'"dps":"(\d+(?:\.\d+)?)","dpsDate":"([^"]+)"')
_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "dividends.json"


def fetch_dividend(code: str) -> tuple[float | None, str | None]:
    """Yahoo Finance Japan から1銘柄の年間予想配当を取得する。

    Returns:
        (dps, dps_date) — ETF等で未検出の場合は (None, None)
    """
    url = _YAHOO_URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")
    m = _DPS_RE.search(html)
    if m is None:
        return None, None
    return float(m.group(1)), m.group(2)


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
            dps, dps_date = fetch_dividend(code)
            if dps is not None:
                data[code] = {"dps": dps, "date": dps_date, "fetched": today}
                print(f"{dps}円 ({dps_date})")
            else:
                data[code] = {"dps": 0, "date": None, "fetched": today}
                print("0円（ETF等: dps未検出）")
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
