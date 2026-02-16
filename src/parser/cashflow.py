"""月次収支データのパーサー。

raw保存された monthly.html を解析し、月ごとの収入・支出を抽出する。

マネーフォワードの月次収支テーブル構造:
  - テーブル id="monthly_list"
  - ヘッダー <th id="js-th-N">YYYY/MM/DD〜</th> で各列の期間開始日
  - <tr class="in_sum"> 収入合計行
  - <tr class="out_sum"> 支出合計行
  - 各セルは「349,328円」形式
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup


@dataclass
class CashflowMonth:
    year_month: str  # YYYY-MM
    income: float
    expense: float


def _parse_yen(text: str) -> float:
    """「1,234,567円」や「-1,234円」のような文字列をfloatに変換する。"""
    cleaned = re.sub(r"[円,\s　]", "", text.strip())
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def parse_monthly(raw_dir: Path) -> list[CashflowMonth]:
    """月次収支HTMLをパースして CashflowMonth のリストを返す。

    パース失敗時は空リストを返す（エラーで止めない）。
    """
    html_path = raw_dir / "monthly.html"
    if not html_path.exists():
        return []

    try:
        html_content = html_path.read_text(encoding="utf-8")
        soup = BeautifulSoup(html_content, "lxml")

        table = soup.find("table", id="monthly_list")
        if not table:
            print("月次収支テーブル(#monthly_list)が見つかりません")
            return []

        # ヘッダーから期間ラベルを取得: "2025/08/25〜" → "2025-08"
        header_row = table.find("tr")
        if not header_row:
            return []

        ths = header_row.find_all("th")
        year_months: list[str] = []
        for th in ths:
            text = th.get_text(strip=True)
            m = re.search(r"(\d{4})/(\d{1,2})/\d{1,2}", text)
            if m:
                year_months.append(f"{m.group(1)}-{int(m.group(2)):02d}")

        if not year_months:
            print("月次収支ヘッダーから日付を抽出できません")
            return []

        # 収入合計行 (class="in_sum")
        incomes: list[float] = []
        in_row = table.find("tr", class_="in_sum")
        if in_row:
            tds = in_row.find_all("td", class_="number")
            incomes = [_parse_yen(td.get_text()) for td in tds]

        # 支出合計行 (class="out_sum")
        expenses: list[float] = []
        out_row = table.find("tr", class_="out_sum")
        if out_row:
            tds = out_row.find_all("td", class_="number")
            expenses = [abs(_parse_yen(td.get_text())) for td in tds]

        # 組み立て
        results: list[CashflowMonth] = []
        for i, ym in enumerate(year_months):
            income = incomes[i] if i < len(incomes) else 0.0
            expense = expenses[i] if i < len(expenses) else 0.0
            results.append(
                CashflowMonth(
                    year_month=ym,
                    income=income,
                    expense=expense,
                )
            )

        return results

    except Exception as e:
        print(f"月次収支パース失敗: {e}")
        return []
