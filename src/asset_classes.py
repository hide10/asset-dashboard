"""資産クラス名の正規化ルール。"""

from __future__ import annotations

CASH_ASSET_CLASS = "預金・現金"
LEGACY_CASH_ASSET_CLASS = "預金・現金・暗号資産"
_LEGACY_CASH_ASSET_CLASSES = {LEGACY_CASH_ASSET_CLASS}
STOCK_ASSET_CLASS = "株式（現物）"
LEGACY_STOCK_ASSET_CLASS = "株式(現物)"


def normalize_asset_class(asset_class: str) -> str:
    """廃止済みの資産クラス名を現在の表示名へ統一する。"""
    if asset_class in _LEGACY_CASH_ASSET_CLASSES:
        return CASH_ASSET_CLASS
    if asset_class == LEGACY_STOCK_ASSET_CLASS:
        return STOCK_ASSET_CLASS
    return asset_class


def normalize_asset_classes(values: dict[str, float]) -> dict[str, float]:
    """資産クラス別金額のキーを正規化し、同一クラスは合算する。"""
    normalized: dict[str, float] = {}
    for asset_class, value in values.items():
        canonical = normalize_asset_class(asset_class)
        normalized[canonical] = normalized.get(canonical, 0) + value
    return normalized
