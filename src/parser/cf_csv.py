"""家計簿CSV（/cf）のパーサー。

MoneyForward の /cf/csv からダウンロードした CSV を解析し、
取引明細のリストを返す。

CSV カラム（cp932 エンコーディング想定）:
  計算対象, 日付, 内容, 金額（円）, 保有金融機関, 大項目, 中項目, メモ, 振替, ID
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CfTransaction:
    id: str
    date: str  # YYYY-MM-DD
    year_month: str  # YYYY-MM
    description: str
    amount: int  # 負=支出, 正=収入
    institution: str
    major_category: str
    minor_category: str
    memo: str
    is_transfer: int  # 1=振替
    is_target: int  # 1=計算対象


def _read_csv_text(csv_path: Path) -> str | None:
    """CSV ファイルを複数エンコーディングで読み込む。"""
    for enc in ("cp932", "shift_jis", "utf-8-sig", "utf-8"):
        try:
            return csv_path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return None


def _parse_date(raw: str) -> str:
    """日付文字列を YYYY-MM-DD に正規化する。"""
    # "2026/01/15" or "2026-01-15"
    raw = raw.strip()
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return raw


def _parse_amount(raw: str) -> int:
    """金額文字列を int に変換する。"""
    cleaned = re.sub(r"[円,\s　\"]", "", raw.strip())
    if not cleaned or cleaned == "-":
        return 0
    return int(cleaned)


def parse_cf_csv(csv_path: Path | str) -> list[CfTransaction]:
    """CF CSV ファイルをパースして CfTransaction のリストを返す。

    パース失敗時は空リストを返す（エラーで止めない）。
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        return []

    try:
        text = _read_csv_text(csv_path)
        if text is None:
            print(f"CF CSV エンコーディング検出失敗: {csv_path}")
            return []

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)

        if len(rows) < 2:
            return []

        # ヘッダー行を検出（「計算対象」を含む行）
        header_idx = 0
        for i, row in enumerate(rows):
            if any("計算対象" in cell for cell in row):
                header_idx = i
                break

        results: list[CfTransaction] = []
        for row in rows[header_idx + 1 :]:
            if len(row) < 10:
                continue

            is_target_raw = row[0].strip()
            date_raw = row[1].strip()
            description = row[2].strip()
            amount_raw = row[3].strip()
            institution = row[4].strip()
            major_category = row[5].strip()
            minor_category = row[6].strip()
            memo = row[7].strip()
            is_transfer_raw = row[8].strip()
            tx_id = row[9].strip()

            if not tx_id or not date_raw:
                continue

            date = _parse_date(date_raw)
            year_month = date[:7]  # YYYY-MM

            results.append(
                CfTransaction(
                    id=tx_id,
                    date=date,
                    year_month=year_month,
                    description=description,
                    amount=_parse_amount(amount_raw),
                    institution=institution,
                    major_category=major_category,
                    minor_category=minor_category,
                    memo=memo,
                    is_transfer=1 if is_transfer_raw == "1" else 0,
                    is_target=1 if is_target_raw == "1" else 0,
                )
            )

        return results

    except Exception as e:
        print(f"CF CSVパース失敗: {e}")
        return []
