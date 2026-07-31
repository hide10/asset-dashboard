"""商品名から地域配分の入力候補を作る。

候補は設定画面で確認できる初期値であり、商品固有の保有明細を公開コードへ
埋め込まない。判別できない商品は無理に配分せず未設定のまま返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REGIONS = ("日本", "米国", "先進国（日本・米国除く）", "新興国", "その他")
MARKET_PRODUCT_WORDS = (
    "株",
    "債券",
    "ファンド",
    "投信",
    "インデックス",
    "index",
    "reit",
    "全世界",
    "米国",
    "国内",
    "海外",
    "外国",
)


@dataclass(frozen=True)
class RegionalExposureSuggestion:
    allocation: dict[str, float]
    basis: str
    confidence: str


def _allocation(japan: float = 0, us: float = 0, developed: float = 0, emerging: float = 0, other: float = 0):
    return dict(zip(REGIONS, (japan, us, developed, emerging, other), strict=True))


def suggest_regional_exposure(name: str, code: str = "") -> RegionalExposureSuggestion | None:
    """名称・コードに地域が明記された商品だけ、地域配分候補を返す。"""
    text = re.sub(r"[\s　・_\-]+", "", f"{name} {code}").lower()

    # 複数地域を含む語を単一地域より先に判定する。
    if any(word in text for word in ("全世界", "オールカントリー", "allcountry", "acwi")):
        return RegionalExposureSuggestion(
            _allocation(japan=5, us=65, developed=20, emerging=10),
            "全世界株式型の概算初期値（構成比は変動します）",
            "estimate",
        )
    if "先進国" in text:
        return RegionalExposureSuggestion(
            _allocation(us=75, developed=25),
            "先進国型の概算初期値（日本を除く代表的指数を想定）",
            "estimate",
        )
    if "新興国" in text or "emerging" in text:
        return RegionalExposureSuggestion(_allocation(emerging=100), "商品名に新興国を明記", "high")
    if any(word in text for word in ("米国", "s&p500", "sp500", "nasdaq", "nyダウ", "ダウ30")):
        return RegionalExposureSuggestion(_allocation(us=100), "商品名に米国または米国指数を明記", "high")
    if any(word in text for word in ("国内", "日本株", "topix", "日経225", "nikkei225")):
        return RegionalExposureSuggestion(_allocation(japan=100), "商品名に国内または日本株指数を明記", "high")
    if any(word in text for word in ("世界", "グローバル", "global")):
        return RegionalExposureSuggestion(
            _allocation(japan=5, us=65, developed=20, emerging=10),
            "世界・グローバル型の概算初期値（構成比は変動します）",
            "estimate",
        )
    if any(word in text for word in ("外国", "海外")):
        return RegionalExposureSuggestion(
            _allocation(us=75, developed=25),
            "外国・海外型の概算初期値（日本を除く先進国中心を想定）",
            "estimate",
        )
    return None


def is_regional_exposure_applicable(name: str) -> bool:
    """市場運用ではない円建て積立・保険商品を地域配分から除外する。"""
    text = re.sub(r"[\s　・_\-]+", "", name).lower()
    if "保険" in text:
        return False
    return "積立" not in text or any(word in text for word in MARKET_PRODUCT_WORDS)
