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
from html import unescape
from pathlib import Path

_YAHOO_URL = "https://finance.yahoo.co.jp/quote/{code}.T"
_DPS_RE = re.compile(r'"dps"\s*:\s*(?:"(?P<dps_str>[\d,]+(?:\.\d+)?)"|(?P<dps_num>[\d,]+(?:\.\d+)?))')
_DPS_DATE_RE = re.compile(r'"dpsDate"\s*:\s*"(?P<date>[^"]+)"')
_DPS_TEXT_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s*円\s*\(([^)]+)\)")
_JSON_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "dividends.json"


def _parse_number(value: str) -> float:
    return float(value.replace(",", ""))


def _parse_dividend_html(html: str) -> tuple[float | None, str | None]:
    """Yahoo Finance Japan の銘柄ページ HTML から年間予想配当を抽出する。"""
    m = _DPS_RE.search(html)
    if m:
        date_match = _DPS_DATE_RE.search(html)
        return _parse_number(m.group("dps_str") or m.group("dps_num")), date_match.group("date") if date_match else None

    text = unescape(re.sub(r"<[^>]+>", "\n", html))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if "1株配当" not in line:
            continue
        window = "\n".join(lines[i : i + 12])
        if "会社予想" not in window and "予想" not in window:
            continue
        text_match = _DPS_TEXT_RE.search(window)
        if text_match:
            return _parse_number(text_match.group(1)), text_match.group(2).strip()
    return None, None


def _fallback_dividend_entry(code: str, fetched: str) -> dict | None:
    """自動取得できない場合に、銘柄マスタの値で安全に補完する。"""
    from src.data.stock_master import STOCK_MASTER, US_STOCK_MASTER, is_us_stock

    master = US_STOCK_MASTER.get(code) if is_us_stock(code) else STOCK_MASTER.get(code)
    if not master:
        return None
    dps = float(master.get("dividend") or 0)
    if dps <= 0:
        return None
    return {"dps": dps, "date": None, "fetched": fetched, "source": "stock_master_fallback"}


def _keep_or_fallback(data: dict, code: str, fetched: str) -> str:
    existing = data.get(code, {})
    existing_dps = float(existing.get("dps") or 0)
    fallback = _fallback_dividend_entry(code, fetched)
    if existing_dps > 0 and existing.get("source") != "stock_master_fallback":
        return f"{existing_dps}円（dps未検出: 既存値を保持）"
    if fallback:
        data[code] = fallback
        return f"{fallback['dps']}円（dps未検出: 銘柄マスタ値で補完）"

    data[code] = {"dps": 0, "date": None, "fetched": fetched, "source": "not_found"}
    return "0円（dps未検出）"


def fetch_dividend(code: str) -> tuple[float | None, str | None]:
    """Yahoo Finance Japan から1銘柄の年間予想配当を取得する。

    Returns:
        (dps, dps_date) — ETF等で未検出の場合は (None, None)
    """
    url = _YAHOO_URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        html = resp.read().decode("utf-8")
    return _parse_dividend_html(html)


def _get_portfolio_stock_codes() -> list[str]:
    """DB のポートフォリオから株式銘柄コードを取得する。"""
    from src.db.schema import DEFAULT_DB_PATH

    if not DEFAULT_DB_PATH.exists():
        return []
    import sqlite3

    conn = sqlite3.connect(str(DEFAULT_DB_PATH))
    try:
        rows = conn.execute(
            """SELECT DISTINCT symbol_or_code FROM snapshot_holdings
               WHERE asset_class = '株式（現物）'
                 AND symbol_or_code != ''
                 AND date = (SELECT MAX(date) FROM snapshots)"""
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:
        return []
    finally:
        conn.close()


def update_all_dividends(codes: list[str] | None = None) -> dict:
    """全銘柄の配当を取得し dividends.json に保存する。

    codes 未指定時は STOCK_MASTER + ポートフォリオ（DB）の和集合を処理する。
    """
    if codes is None:
        from src.data.stock_master import get_all_codes

        master_codes = get_all_codes()
        portfolio_codes = _get_portfolio_stock_codes()
        # マスタ優先、ポートフォリオにしかない銘柄を末尾に追加
        seen = set(master_codes)
        codes = list(master_codes)
        for c in portfolio_codes:
            if c not in seen:
                codes.append(c)
                seen.add(c)

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
                print(_keep_or_fallback(data, code, today))
        except Exception as e:
            print(f"エラー: {e} / {_keep_or_fallback(data, code, today)}")
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
