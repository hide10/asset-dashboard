"""rawデータから正規化された資産データを抽出するパーサ。

raw保存されたHTML/JSONを解析し、構造化データを返す。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, Tag


@dataclass
class AccountData:
    account_name: str
    asset_class: str
    balance: float
    institution: str = ""


@dataclass
class HoldingData:
    symbol_or_code: str
    name: str
    value: float
    quantity: float | None = None
    acquisition_price: float | None = None  # 平均取得単価
    current_price: float | None = None      # 現在値/基準価額
    asset_class: str = ""
    position: int = 0  # テーブル内の行位置（同名銘柄の区別用）


@dataclass
class AssetSnapshot:
    date: str  # YYYY-MM-DD
    total_asset: float
    by_class: dict[str, float]
    accounts: list[AccountData] = field(default_factory=list)
    holdings: list[HoldingData] = field(default_factory=list)


def _parse_yen(text: str) -> float:
    """「1,234,567円」や「-1,234円」のような文字列をfloatに変換する。"""
    cleaned = re.sub(r"[円,\s　]", "", text.strip())
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def _parse_number(text: str) -> float | None:
    """数値文字列をfloatに変換する。空やハイフンはNone。"""
    cleaned = re.sub(r"[,\s　ポイント口]", "", text.strip())
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_date_from_dirname(raw_dir: Path) -> str:
    """rawディレクトリ名（2026-02-13_225758）から日付を抽出する。"""
    dirname = raw_dir.name
    match = re.match(r"(\d{4}-\d{2}-\d{2})", dirname)
    if match:
        return match.group(1)
    raise ValueError(f"ディレクトリ名から日付を抽出できません: {dirname}")


# --- 資産クラス別テーブル ID → 表示名 マッピング ---
SECTION_MAP = {
    "portfolio_det_depo": "預金・現金・暗号資産",
    "portfolio_det_eq": "株式（現物）",
    "portfolio_det_mf": "投資信託",
    "portfolio_det_re": "不動産",
    "portfolio_det_pns": "年金",
    "portfolio_det_po": "ポイント・マイル",
}


def _parse_total_asset(soup: BeautifulSoup) -> float:
    """資産総額を取得する。"""
    box = soup.find("div", class_="heading-radius-box")
    if box:
        text = box.get_text()
        return _parse_yen(text.replace("資産総額：", ""))
    raise ValueError("資産総額が見つかりません")


def _parse_by_class(soup: BeautifulSoup) -> dict[str, float]:
    """資産の内訳テーブルからクラス別金額を取得する。"""
    result: dict[str, float] = {}
    summary_section = soup.find("section", class_="bs-total-assets")
    if not summary_section:
        return result

    table = summary_section.find("table", class_="table-bordered")
    if not table:
        return result

    for row in table.find_all("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) >= 2:
            link = cells[0].find("a")
            if link:
                class_name = link.get_text(strip=True)
                amount = _parse_yen(cells[1].get_text())
                result[class_name] = amount
    return result


def _parse_depo_section(section: Tag) -> list[AccountData]:
    """預金・現金・暗号資産セクションをパースする。"""
    accounts: list[AccountData] = []
    table = section.find("table", class_="table-depo")
    if not table:
        return accounts

    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 3:
            name = cells[0].get_text(strip=True)
            balance = _parse_yen(cells[1].get_text())
            institution = cells[2].get_text(strip=True)
            accounts.append(AccountData(
                account_name=name,
                asset_class="預金・現金・暗号資産",
                balance=balance,
                institution=institution,
            ))
    return accounts


def _parse_eq_section(section: Tag) -> list[HoldingData]:
    """株式（現物）セクションをパースする。"""
    holdings: list[HoldingData] = []
    table = section.find("table", class_="table-eq")
    if not table:
        return holdings

    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 6:
            code = cells[0].get_text(strip=True)
            name = cells[1].get_text(strip=True)
            quantity = _parse_number(cells[2].get_text())
            acquisition_price = _parse_number(cells[3].get_text()) if len(cells) > 3 else None
            current_price = _parse_number(cells[4].get_text()) if len(cells) > 4 else None
            value = _parse_yen(cells[5].get_text())
            holdings.append(HoldingData(
                symbol_or_code=code,
                name=name,
                value=value,
                quantity=quantity,
                acquisition_price=acquisition_price,
                current_price=current_price,
                asset_class="株式（現物）",
            ))
    return holdings


def _parse_mf_section(section: Tag) -> list[HoldingData]:
    """投資信託セクションをパースする。"""
    holdings: list[HoldingData] = []
    table = section.find("table", class_="table-mf")
    if not table:
        return holdings

    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 5:
            name = cells[0].get_text(strip=True)
            quantity = _parse_number(cells[1].get_text())
            acquisition_price = _parse_number(cells[2].get_text()) if len(cells) > 2 else None
            current_price = _parse_number(cells[3].get_text()) if len(cells) > 3 else None
            value = _parse_yen(cells[4].get_text())
            holdings.append(HoldingData(
                symbol_or_code="",
                name=name,
                value=value,
                quantity=quantity,
                acquisition_price=acquisition_price,
                current_price=current_price,
                asset_class="投資信託",
            ))
    return holdings


def _parse_simple_section(section: Tag, asset_class: str, table_class: str) -> list[HoldingData]:
    """不動産・年金など、名称+現在価値 の単純なテーブルをパースする。"""
    holdings: list[HoldingData] = []
    table = section.find("table", class_=table_class)
    if not table:
        return holdings

    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 3:
            name = cells[0].get_text(strip=True)
            value = _parse_yen(cells[2].get_text())
            holdings.append(HoldingData(
                symbol_or_code="",
                name=name,
                value=value,
                asset_class=asset_class,
            ))
    return holdings


def _parse_point_section(section: Tag) -> list[HoldingData]:
    """ポイント・マイルセクションをパースする。"""
    holdings: list[HoldingData] = []
    table = section.find("table", class_="table-pns")
    if not table:
        return holdings

    tbody = table.find("tbody")
    if not tbody:
        return holdings

    for row in tbody.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) >= 5:
            name = cells[0].get_text(strip=True)
            quantity = _parse_number(cells[2].get_text())
            value = _parse_yen(cells[4].get_text())
            holdings.append(HoldingData(
                symbol_or_code="",
                name=name,
                value=value,
                quantity=quantity,
                asset_class="ポイント・マイル",
            ))
    return holdings


def parse_raw(raw_dir: Path) -> AssetSnapshot:
    """rawディレクトリを解析してAssetSnapshotを返す。"""
    html_path = raw_dir / "asset.html"
    if not html_path.exists():
        raise FileNotFoundError(f"HTMLファイルが見つかりません: {html_path}")

    html_content = html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_content, "lxml")

    date = _extract_date_from_dirname(raw_dir)
    total_asset = _parse_total_asset(soup)
    by_class = _parse_by_class(soup)

    accounts: list[AccountData] = []
    holdings: list[HoldingData] = []

    # 預金・現金・暗号資産
    depo = soup.find("section", id="portfolio_det_depo")
    if depo:
        accounts.extend(_parse_depo_section(depo))

    # 株式（現物）
    eq = soup.find("section", id="portfolio_det_eq")
    if eq:
        holdings.extend(_parse_eq_section(eq))

    # 投資信託
    mf = soup.find("section", id="portfolio_det_mf")
    if mf:
        holdings.extend(_parse_mf_section(mf))

    # 不動産
    re_section = soup.find("section", id="portfolio_det_re")
    if re_section:
        holdings.extend(_parse_simple_section(re_section, "不動産", "table-re"))

    # 年金
    pns = soup.find("section", id="portfolio_det_pns")
    if pns:
        holdings.extend(_parse_simple_section(pns, "年金", "table-pns"))

    # ポイント・マイルは除外

    # by_classからもポイント・マイルを除外し、総資産を再計算
    by_class.pop("ポイント・マイル", None)
    total_asset = sum(by_class.values())

    # 資産クラス内の行位置を付与（同名銘柄の区別用）
    class_counters: dict[str, int] = {}
    for h in holdings:
        idx = class_counters.get(h.asset_class, 0)
        h.position = idx
        class_counters[h.asset_class] = idx + 1

    return AssetSnapshot(
        date=date,
        total_asset=total_asset,
        by_class=by_class,
        accounts=accounts,
        holdings=holdings,
    )
