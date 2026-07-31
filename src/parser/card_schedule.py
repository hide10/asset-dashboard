"""MoneyForwardのカード口座詳細から引落予定を抽出する。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup, Tag


@dataclass(frozen=True)
class CardAccountLink:
    """口座一覧から取得した口座詳細へのリンク。"""

    name: str
    href: str


@dataclass(frozen=True)
class ScheduledCardPayment:
    """投資可能額の計算に使うカード引落予定。"""

    due_date: date
    card_name: str
    amount: float
    external_id: str
    withdrawal_account: str = ""
    memo: str = ""


_DATE_RE = re.compile(r"(\d{4})\s*[/-]\s*(\d{1,2})\s*[/-]\s*(\d{1,2})")
_YEN_RE = re.compile(r"([+-−]?)[\s　]*([0-9][0-9,]*)\s*円")


def _normalize_text(value: str) -> str:
    """全角数字やマイナス記号を含むMoneyForward表記を正規化する。"""
    return unicodedata.normalize("NFKC", value or "").replace("−", "-").strip()


def _parse_amount_and_date(value: str) -> tuple[float, date] | None:
    """「-12,345円 (2026/08/10)」形式から金額と日付を返す。"""
    text = _normalize_text(value)
    amount_match = _YEN_RE.search(text)
    date_match = _DATE_RE.search(text)
    if not amount_match or not date_match:
        return None
    try:
        amount = abs(float(amount_match.group(2).replace(",", "")))
        due_date = date(
            int(date_match.group(1)),
            int(date_match.group(2)),
            int(date_match.group(3)),
        )
    except (ValueError, TypeError):
        return None
    if amount <= 0:
        return None
    return amount, due_date


def parse_card_account_links(html: str) -> list[CardAccountLink]:
    """口座一覧HTMLから重複のない口座詳細リンクを返す。

    カード名に「カード」が含まれない登録名（例: 三井住友）もあるため、
    ここでは口座種別を推測せず全口座を返し、詳細ページ側の見出しで絞り込む。
    """
    soup = BeautifulSoup(html, "lxml")
    links: list[CardAccountLink] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href^="/accounts/show/"]'):
        href = str(anchor.get("href", "")).strip()
        name = " ".join(anchor.get_text(" ", strip=True).split())
        if not href or not name or href in seen:
            continue
        seen.add(href)
        links.append(CardAccountLink(name=name, href=href))
    return links


def _schedule_table(soup: BeautifulSoup) -> Tag | None:
    """「引き落とし予定額」列を持つ表を探す。"""
    for table in soup.find_all("table"):
        headers = " ".join(th.get_text(" ", strip=True) for th in table.find_all("th"))
        if "引き落とし予定額" in headers:
            return table
    return None


def parse_card_schedule_html(html: str, account: CardAccountLink) -> list[ScheduledCardPayment]:
    """カード口座詳細HTMLから既知の引落予定だけを抽出する。

    引落額未確定（`-`）の行、ポイント行、日付のない行は対象外にする。
    MoneyForwardは補助カード等を複数行で返すため、金額が入った行は個別に保持する。
    """
    soup = BeautifulSoup(html, "lxml")
    table = _schedule_table(soup)
    if table is None:
        return []

    header_row = table.find("tr")
    if header_row is None:
        return []
    headers = [
        " ".join(cell.get_text(" ", strip=True).split()) for cell in header_row.find_all(["th", "td"], recursive=False)
    ]
    try:
        amount_index = next(i for i, value in enumerate(headers) if "引き落とし予定額" in value)
    except StopIteration:
        return []

    payments: list[ScheduledCardPayment] = []
    rows = table.find("tbody") or table
    for row_index, row in enumerate(rows.find_all("tr", recursive=False)):
        cells = row.find_all("td", recursive=False)
        if amount_index >= len(cells):
            continue
        parsed = _parse_amount_and_date(cells[amount_index].get_text(" ", strip=True))
        if parsed is None:
            continue
        amount, due_date = parsed
        product_name = " ".join(cells[1].get_text(" ", strip=True).split()) if len(cells) > 1 else ""
        if product_name == "ポイント":
            continue
        label = account.name
        if product_name and product_name not in label:
            label = f"{label} / {product_name}"
        payments.append(
            ScheduledCardPayment(
                due_date=due_date,
                card_name=label,
                amount=amount,
                external_id=f"{account.href}#{row_index}",
            )
        )
    return payments
