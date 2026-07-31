"""銘柄マスタ: 業種分類。

配当は data/dividends.json（自動取得）のみを参照する。
取得できない銘柄は None を返し、UI 側で「取得エラー」表示する。
（ハードコードフォールバックは古くなる/誤情報になりやすいため廃止）

日本株の業種は東証33業種の正式名称に限定し、
data/sectors.json（自動取得）→ STOCK_MASTER の順でフォールバックする。
"""

from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

_DIVIDENDS_JSON = Path(__file__).resolve().parent.parent.parent / "data" / "dividends.json"
_SECTORS_JSON = Path(__file__).resolve().parent.parent.parent / "data" / "sectors.json"
_dividend_cache: dict | None = None
_sector_cache: dict | None = None

JPX_33_SECTORS = {
    "水産・農林業",
    "鉱業",
    "建設業",
    "食料品",
    "繊維製品",
    "パルプ・紙",
    "化学",
    "医薬品",
    "石油・石炭製品",
    "ゴム製品",
    "ガラス・土石製品",
    "鉄鋼",
    "非鉄金属",
    "金属製品",
    "機械",
    "電気機器",
    "輸送用機器",
    "精密機器",
    "その他製品",
    "電気・ガス業",
    "陸運業",
    "海運業",
    "空運業",
    "倉庫・運輸関連業",
    "情報・通信業",
    "卸売業",
    "小売業",
    "銀行業",
    "証券、商品先物取引業",
    "保険業",
    "その他金融業",
    "不動産業",
    "サービス業",
}
UNCLASSIFIED_SECTOR = "未分類（取得失敗）"

# 業種分類のみ保持。配当はハードコードせず dividends.json（自動取得）のみを参照。
STOCK_MASTER: dict[str, dict] = {
    "8053": {"name": "住友商事", "sector": "卸売業"},
    "8766": {"name": "東京海上HD", "sector": "保険業"},
    "4502": {"name": "武田薬品", "sector": "医薬品"},
    "8591": {"name": "オリックス", "sector": "その他金融業"},
    "8593": {"name": "三菱HCキャピタル", "sector": "その他金融業"},
    "8316": {"name": "三井住友フィナンシャルG", "sector": "銀行業"},
    "9433": {"name": "KDDI", "sector": "情報・通信業"},
    "6918": {"name": "アバールデータ", "sector": "電気機器"},
    "1928": {"name": "積水ハウス", "sector": "建設業"},
    "7762": {"name": "シチズン時計", "sector": "精密機器"},
    "5401": {"name": "日本製鉄", "sector": "鉄鋼"},
    "9432": {"name": "NTT", "sector": "情報・通信業"},
    "8200": {"name": "リンガーハット", "sector": "小売業"},
    "1478": {"name": "iS高配当ETF", "sector": "ETF"},
    "4755": {"name": "楽天グループ", "sector": "サービス業"},
}

# 米国株マスター（ティッカーシンボル → 業種）
US_STOCK_MASTER: dict[str, dict] = {
    "AAPL": {"name": "Apple", "sector": "テクノロジー"},
    "MSFT": {"name": "Microsoft", "sector": "テクノロジー"},
    "GOOGL": {"name": "Alphabet", "sector": "テクノロジー"},
    "GOOG": {"name": "Alphabet C", "sector": "テクノロジー"},
    "AMZN": {"name": "Amazon", "sector": "消費財"},
    "NVDA": {"name": "NVIDIA", "sector": "テクノロジー"},
    "META": {"name": "Meta Platforms", "sector": "テクノロジー"},
    "TSLA": {"name": "Tesla", "sector": "自動車"},
    "JPM": {"name": "JPMorgan Chase", "sector": "金融"},
    "V": {"name": "Visa", "sector": "金融"},
    "JNJ": {"name": "Johnson & Johnson", "sector": "ヘルスケア"},
    "PG": {"name": "Procter & Gamble", "sector": "消費財"},
    "KO": {"name": "Coca-Cola", "sector": "消費財"},
    "PEP": {"name": "PepsiCo", "sector": "消費財"},
    "HD": {"name": "Home Depot", "sector": "小売"},
    "VZ": {"name": "Verizon", "sector": "通信"},
    "T": {"name": "AT&T", "sector": "通信"},
    "XOM": {"name": "Exxon Mobil", "sector": "エネルギー"},
    "VTI": {"name": "Vanguard Total Stock ETF", "sector": "米国ETF"},
    "VOO": {"name": "Vanguard S&P 500 ETF", "sector": "米国ETF"},
    "VYM": {"name": "Vanguard High Div ETF", "sector": "米国ETF"},
    "SPYD": {"name": "SPDR S&P 500 High Div ETF", "sector": "米国ETF"},
    "QQQ": {"name": "Invesco QQQ Trust", "sector": "米国ETF"},
}

_YAHOO_URL = "https://finance.yahoo.co.jp/quote/{code}.T"
_SECTOR_RE = re.compile(r'"sectorName":"([^"]+)"')


def is_us_stock(code: str) -> bool:
    """米国株ティッカーシンボルかどうかを判定する。"""
    if not code:
        return False
    # 日本株は4桁数字、米国株はアルファベット
    return bool(re.match(r"^[A-Z]{1,5}$", code))


def _fetch_sector(code: str) -> str | None:
    """Yahoo Finance Japan から銘柄の業種を取得する。

    取得できない場合は None を返す。
    """
    url = _YAHOO_URL.format(code=code)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8")
        m = _SECTOR_RE.search(html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _load_sectors() -> dict:
    """sectors.json をキャッシュ付きで読み込む。"""
    global _sector_cache
    if _sector_cache is None:
        if _SECTORS_JSON.exists():
            _sector_cache = json.loads(_SECTORS_JSON.read_text(encoding="utf-8"))
        else:
            _sector_cache = {}
    return _sector_cache


def _save_sectors(data: dict) -> None:
    """sectors.json に保存する。"""
    _SECTORS_JSON.parent.mkdir(parents=True, exist_ok=True)
    _SECTORS_JSON.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_sector(code: str) -> str:
    """銘柄コードから業種を返す。

    日本株: sectors.json → STOCK_MASTER → '未分類（取得失敗）'
    米国株: sectors.json → US_STOCK_MASTER → '米国株'

    表示処理中に外部通信は行わない。キャッシュ更新は update_sectors() が担う。
    """
    # 1. sectors.json キャッシュ
    sectors = _load_sectors()

    # 米国株は東証33業種の検証対象外。既存の米国セクター分類を維持する。
    if is_us_stock(code):
        cached_sector = sectors.get(code)
        if isinstance(cached_sector, str) and cached_sector:
            return cached_sector
        us_info = US_STOCK_MASTER.get(code)
        return us_info["sector"] if us_info else "米国株"

    cached_sector = sectors.get(code)
    if cached_sector in JPX_33_SECTORS or cached_sector == "ETF":
        return cached_sector

    # 2. STOCK_MASTER ハードコード（日本株）
    info = STOCK_MASTER.get(code)
    if info:
        return info["sector"]

    return UNCLASSIFIED_SECTOR


def update_sectors(codes: list[str]) -> dict:
    """指定銘柄の東証33業種を取得し、検証後にキャッシュする。

    取得失敗や正式分類外の値では、既存キャッシュを上書きしない。
    """
    import time

    sectors = _load_sectors()
    for i, code in enumerate(codes):
        if is_us_stock(code) or STOCK_MASTER.get(code, {}).get("sector") == "ETF":
            continue
        sector = _fetch_sector(code)
        if sector in JPX_33_SECTORS:
            sectors[code] = sector
            print(f"  [sector] {code}: {sector}")
        elif sector:
            print(f"  [sector] {code}: 正式分類外のため不採用 ({sector})")
        else:
            print(f"  [sector] {code}: 取得不可（既存分類を維持）")
        if i < len(codes) - 1:
            time.sleep(0.5)

    _save_sectors(sectors)
    return sectors


def _load_dividends() -> dict:
    """dividends.json をキャッシュ付きで読み込む。"""
    global _dividend_cache
    if _dividend_cache is None:
        if _DIVIDENDS_JSON.exists():
            _dividend_cache = json.loads(_DIVIDENDS_JSON.read_text(encoding="utf-8"))
        else:
            _dividend_cache = {}
    return _dividend_cache


def get_dividend(code: str) -> float | None:
    """銘柄コードから年間予想配当を返す。

    日本株: 円/株、米国株: USD/株。
    dividends.json に登録された数値のみを返し、未取得・取得失敗・null の場合は
    None を返す（UI 側で「取得エラー」表示するため）。
    """
    divs = _load_dividends()
    cached = divs.get(code)
    if cached is None:
        return None
    dps = cached.get("dps")
    if dps is None:
        return None
    try:
        return float(dps)
    except (TypeError, ValueError):
        return None


def get_all_codes() -> list[str]:
    """STOCK_MASTER に登録された全銘柄コードを返す（日本株のみ）。"""
    return list(STOCK_MASTER.keys())


def get_all_us_codes() -> list[str]:
    """US_STOCK_MASTER に登録された全ティッカーを返す。"""
    return list(US_STOCK_MASTER.keys())
