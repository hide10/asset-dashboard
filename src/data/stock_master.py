"""銘柄マスタ: 業種分類・配当情報。

データが古くなったら手動で更新する。
配当は年間予想配当（円/株）。
"""

from __future__ import annotations

STOCK_MASTER: dict[str, dict] = {
    "8053": {"name": "住友商事",     "sector": "卸売業",       "dividend": 125},
    "8766": {"name": "東京海上HD",   "sector": "保険業",       "dividend": 159},
    "4502": {"name": "武田薬品",     "sector": "医薬品",       "dividend": 196},
    "8591": {"name": "オリックス",   "sector": "その他金融業", "dividend": 98.6},
    "9433": {"name": "KDDI",         "sector": "情報・通信業", "dividend": 145},
    "6918": {"name": "アバールデータ", "sector": "電気機器",   "dividend": 80},
    "1928": {"name": "積水ハウス",   "sector": "建設業",       "dividend": 129},
    "7762": {"name": "シチズン時計", "sector": "精密機器",     "dividend": 46},
    "5401": {"name": "日本製鉄",     "sector": "鉄鋼",         "dividend": 160},
    "9432": {"name": "NTT",          "sector": "情報・通信業", "dividend": 5.2},
    "8200": {"name": "リンガーハット", "sector": "小売業",     "dividend": 15},
    "1478": {"name": "iS高配当ETF",  "sector": "ETF",          "dividend": 500},
    "4755": {"name": "楽天グループ", "sector": "サービス業",   "dividend": 4.5},
}


def get_sector(code: str) -> str:
    """銘柄コードから業種を返す。未登録なら'その他'。"""
    info = STOCK_MASTER.get(code)
    return info["sector"] if info else "その他"


def get_dividend(code: str) -> float:
    """銘柄コードから年間予想配当（円/株）を返す。未登録なら0。"""
    info = STOCK_MASTER.get(code)
    return info["dividend"] if info else 0.0
