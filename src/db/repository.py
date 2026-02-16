"""スナップショットの保存・取得を担うリポジトリ層。"""

from __future__ import annotations

import json
import sqlite3

from src.parser.cashflow import CashflowMonth
from src.parser.cf_csv import CfTransaction
from src.parser.normalize import AssetSnapshot


def save_snapshot(conn: sqlite3.Connection, snapshot: AssetSnapshot, raw_path: str) -> None:
    """AssetSnapshotをDBに保存する。同日データがあれば差し替える。"""
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        (snapshot.date, snapshot.total_asset, json.dumps(snapshot.by_class, ensure_ascii=False), raw_path),
    )
    # 同日の既存データを削除してから再挿入
    conn.execute("DELETE FROM snapshot_accounts WHERE date = ?", (snapshot.date,))
    conn.execute("DELETE FROM snapshot_holdings WHERE date = ?", (snapshot.date,))

    for acc in snapshot.accounts:
        conn.execute(
            "INSERT INTO snapshot_accounts (date, account_name, asset_class, balance, institution) VALUES (?, ?, ?, ?, ?)",
            (snapshot.date, acc.account_name, acc.asset_class, acc.balance, acc.institution),
        )
    for h in snapshot.holdings:
        conn.execute(
            "INSERT INTO snapshot_holdings (date, symbol_or_code, name, quantity, value, asset_class, position, acquisition_price, current_price, unrealized_gain, unrealized_gain_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot.date,
                h.symbol_or_code,
                h.name,
                h.quantity,
                h.value,
                h.asset_class,
                h.position,
                h.acquisition_price,
                h.current_price,
                h.unrealized_gain,
                h.unrealized_gain_pct,
            ),
        )
    conn.commit()


def get_snapshot(conn: sqlite3.Connection, target_date: str) -> dict | None:
    """指定日のスナップショットを取得する。"""
    row = conn.execute(
        "SELECT date, total_asset, by_class_json, raw_path FROM snapshots WHERE date = ?",
        (target_date,),
    ).fetchone()
    if row is None:
        return None
    return {
        "date": row[0],
        "total_asset": row[1],
        "by_class": json.loads(row[2]),
        "raw_path": row[3],
    }


def get_nearest_snapshot(conn: sqlite3.Connection, target_date: str) -> dict | None:
    """指定日に最も近いスナップショットを取得する（同日含む、過去方向優先）。"""
    row = conn.execute(
        "SELECT date, total_asset, by_class_json, raw_path FROM snapshots WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (target_date,),
    ).fetchone()
    if row is None:
        return None
    return {
        "date": row[0],
        "total_asset": row[1],
        "by_class": json.loads(row[2]),
        "raw_path": row[3],
    }


def get_all_total_assets(conn: sqlite3.Connection) -> list[tuple[str, float]]:
    """全日の(date, total_asset)リストを返す（日付昇順）。"""
    rows = conn.execute("SELECT date, total_asset FROM snapshots ORDER BY date ASC").fetchall()
    return rows


def save_cashflows(conn: sqlite3.Connection, months: list[CashflowMonth], fetched_date: str) -> None:
    """月次収支データをDBに保存する。同月データがあれば差し替える。"""
    for m in months:
        conn.execute(
            "INSERT OR REPLACE INTO monthly_cashflows (year_month, income, expense, fetched) VALUES (?, ?, ?, ?)",
            (m.year_month, m.income, m.expense, fetched_date),
        )
    conn.commit()


def get_cashflows(conn: sqlite3.Connection, limit: int = 12) -> list[dict]:
    """月次収支データを新しい順に取得する。"""
    rows = conn.execute(
        "SELECT year_month, income, expense FROM monthly_cashflows ORDER BY year_month DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [{"year_month": r[0], "income": r[1], "expense": r[2]} for r in rows]


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    """設定値を取得する。"""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def save_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    """設定値を保存する。"""
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# --- CF (家計簿) ---


def save_cf_transactions(conn: sqlite3.Connection, transactions: list[CfTransaction], fetched_date: str) -> None:
    """CF取引データをDBに保存する。同IDがあれば差し替える。"""
    for tx in transactions:
        conn.execute(
            """INSERT OR REPLACE INTO cf_transactions
               (id, year_month, date, description, amount, institution,
                major_category, minor_category, memo, is_transfer, is_target, fetched)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                tx.id,
                tx.year_month,
                tx.date,
                tx.description,
                tx.amount,
                tx.institution,
                tx.major_category,
                tx.minor_category,
                tx.memo,
                tx.is_transfer,
                tx.is_target,
                fetched_date,
            ),
        )
    conn.commit()


def save_cf_csv_month(conn: sqlite3.Connection, year_month: str, fetched_date: str, row_count: int) -> None:
    """CSVダウンロード記録を保存する。"""
    conn.execute(
        "INSERT OR REPLACE INTO cf_csv_months (year_month, fetched, row_count) VALUES (?, ?, ?)",
        (year_month, fetched_date, row_count),
    )
    conn.commit()


def get_cf_category_summary(conn: sqlite3.Connection, year_month: str) -> dict:
    """指定月の大項目別・中項目別集計、収支合計、高額TOP15を返す。"""
    # 振替を除外し、計算対象のみ
    base_where = "WHERE year_month = ? AND is_transfer = 0 AND is_target = 1"

    # 大項目別支出（amount < 0）
    rows = conn.execute(
        f"""SELECT major_category, SUM(amount) as total
            FROM cf_transactions {base_where} AND amount < 0
            GROUP BY major_category ORDER BY total ASC""",
        (year_month,),
    ).fetchall()
    major_categories = [{"name": r[0], "total": abs(r[1])} for r in rows if r[0]]

    # 中項目別支出（大項目ごと）
    minor_rows = conn.execute(
        f"""SELECT major_category, minor_category, SUM(amount) as total
            FROM cf_transactions {base_where} AND amount < 0
            GROUP BY major_category, minor_category ORDER BY major_category, total ASC""",
        (year_month,),
    ).fetchall()
    minor_by_major: dict[str, list] = {}
    for r in minor_rows:
        if r[0]:
            minor_by_major.setdefault(r[0], []).append({"name": r[1] or "未分類", "total": abs(r[2])})

    # 収支合計
    totals = conn.execute(
        f"""SELECT
              SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) as expense,
              SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income
            FROM cf_transactions {base_where}""",
        (year_month,),
    ).fetchone()
    total_expense = abs(totals[0]) if totals[0] else 0
    total_income = totals[1] if totals[1] else 0

    # 高額支出 TOP15
    top_rows = conn.execute(
        f"""SELECT date, description, amount, major_category, minor_category, institution
            FROM cf_transactions {base_where} AND amount < 0
            ORDER BY amount ASC LIMIT 15""",
        (year_month,),
    ).fetchall()
    top_expenses = [
        {
            "date": r[0],
            "description": r[1],
            "amount": abs(r[2]),
            "major_category": r[3],
            "minor_category": r[4],
            "institution": r[5],
        }
        for r in top_rows
    ]

    return {
        "year_month": year_month,
        "total_expense": total_expense,
        "total_income": total_income,
        "balance": total_income - total_expense,
        "major_categories": major_categories,
        "minor_by_major": minor_by_major,
        "top_expenses": top_expenses,
    }


def get_cf_monthly_trend(conn: sqlite3.Connection, months: int = 12) -> list[dict]:
    """月別収入・支出推移を返す（新しい順 → 古い順に並び替え）。"""
    rows = conn.execute(
        """SELECT year_month,
              SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) as expense,
              SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income
           FROM cf_transactions
           WHERE is_transfer = 0 AND is_target = 1
           GROUP BY year_month
           ORDER BY year_month DESC
           LIMIT ?""",
        (months,),
    ).fetchall()
    result = [{"year_month": r[0], "expense": abs(r[1]) if r[1] else 0, "income": r[2] if r[2] else 0} for r in rows]
    result.reverse()
    return result


def get_cf_category_trend(conn: sqlite3.Connection, months: int = 6) -> dict:
    """カテゴリ別月次推移を返す。"""
    ym_rows = conn.execute(
        """SELECT DISTINCT year_month FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
           ORDER BY year_month DESC LIMIT ?""",
        (months,),
    ).fetchall()
    year_months = [r[0] for r in reversed(ym_rows)]

    if not year_months:
        return {"year_months": [], "categories": [], "by_month": {}}

    rows = conn.execute(
        """SELECT year_month, major_category, SUM(amount) as total
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND year_month IN ({})
           GROUP BY year_month, major_category""".format(",".join("?" * len(year_months))),
        year_months,
    ).fetchall()

    cat_totals: dict[str, float] = {}
    for r in rows:
        cat_totals[r[1]] = cat_totals.get(r[1], 0) + abs(r[2])
    categories = sorted(cat_totals, key=lambda c: cat_totals[c], reverse=True)

    by_month: dict[str, dict[str, float]] = {}
    for r in rows:
        by_month.setdefault(r[0], {})[r[1]] = abs(r[2])

    return {
        "year_months": year_months,
        "categories": categories,
        "by_month": by_month,
    }


def get_cf_fixed_expenses(conn: sqlite3.Connection, months: int = 3) -> dict:
    """固定費候補を検出する。

    固定費 = 契約・自動引き落としで毎月ほぼ同額が出ていく支出。
    （家賃、管理費、保険、通信費、サブスク、光熱費等）
    カフェや美容院のような「習慣的だが裁量的な支出」は変動費。

    判定条件（当月を除く確定月のみで判定）:
    - 金額のブレが10%以内（自動引き落としは同額になる）
    - 「現金・カード」カテゴリは除外（二重計上防止）
    - 確定月に2回以上出現、または確定月+当月で同額なら固定費と判定
    """
    from datetime import date as _date

    current_ym = _date.today().strftime("%Y-%m")

    ym_rows = conn.execute(
        """SELECT DISTINCT year_month FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
           ORDER BY year_month DESC LIMIT ?""",
        (months + 1,),
    ).fetchall()
    # 当月は途中データなので判定対象から除外
    year_months = [r[0] for r in ym_rows if r[0] != current_ym][:months]
    if len(year_months) < 2:
        return {"fixed": [], "variable_total": 0, "fixed_total": 0, "fixed_ratio": 0, "months_used": len(year_months)}

    # 「現金・カード」はカード引き落とし等で二重計上になるため除外
    _exclude_major = "現金・カード"

    rows = conn.execute(
        """SELECT year_month, major_category, minor_category, SUM(amount)
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND major_category != ?
             AND year_month IN ({})
           GROUP BY year_month, major_category, minor_category""".format(",".join("?" * len(year_months))),
        [_exclude_major] + year_months,
    ).fetchall()

    pair_months: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        key = (r[1], r[2])
        pair_months.setdefault(key, {})[r[0]] = abs(r[3])

    # 当月データも参照用に取得（判定月には含めないが補助判定に使う）
    current_rows = conn.execute(
        """SELECT major_category, minor_category, SUM(amount)
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND major_category != ?
             AND year_month=?
           GROUP BY major_category, minor_category""",
        (
            _exclude_major,
            current_ym,
        ),
    ).fetchall()
    current_map: dict[tuple[str, str], float] = {}
    for r in current_rows:
        current_map[(r[0], r[1])] = abs(r[2])

    fixed = []
    fixed_total = 0
    variable_total = 0

    for (major, minor), month_vals in pair_months.items():
        vals = list(month_vals.values())
        avg = sum(vals) / len(vals)

        # 全値（確定月+当月）を集めてブレを判定
        cur_val = current_map.get((major, minor))
        all_vals = vals + ([cur_val] if cur_val is not None else [])
        all_avg = sum(all_vals) / len(all_vals) if all_vals else 0

        # 固定費判定: 2回以上の出現 + 全値のブレが10%以内
        is_fixed = (
            len(all_vals) >= 2 and all_avg > 0 and max(all_vals) / all_avg <= 1.1 and min(all_vals) / all_avg >= 0.9
        )

        if is_fixed:
            fixed.append(
                {
                    "major": major,
                    "minor": minor or "未分類",
                    "avg_amount": round(avg),
                    "latest": vals[-1] if vals else round(avg),
                }
            )
            fixed_total += round(avg)
        else:
            variable_total += round(avg)

    fixed.sort(key=lambda x: x["avg_amount"], reverse=True)

    return {
        "fixed": fixed,
        "fixed_total": fixed_total,
        "variable_total": variable_total,
        "fixed_ratio": round(fixed_total / (fixed_total + variable_total) * 100)
        if (fixed_total + variable_total)
        else 0,
        "months_used": len(year_months),
    }


def get_cf_income_breakdown(conn: sqlite3.Connection, year_month: str) -> dict:
    """収入の中項目別内訳を返す。"""
    rows = conn.execute(
        """SELECT minor_category, SUM(amount) as total
           FROM cf_transactions
           WHERE year_month=? AND is_transfer=0 AND is_target=1 AND amount>0
           GROUP BY minor_category ORDER BY total DESC""",
        (year_month,),
    ).fetchall()
    items = [{"name": r[0] or "未分類", "total": r[1]} for r in rows]
    total = sum(i["total"] for i in items)
    return {"items": items, "total": total}


def get_cf_income_trend(conn: sqlite3.Connection, months: int = 6) -> list[dict]:
    """月別の収入推移を返す。"""
    rows = conn.execute(
        """SELECT year_month, SUM(amount) as total
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount>0
           GROUP BY year_month ORDER BY year_month DESC LIMIT ?""",
        (months,),
    ).fetchall()
    result = [{"year_month": r[0], "income": r[1]} for r in rows]
    result.reverse()
    return result


def get_cf_actual_savings(conn: sqlite3.Connection, months: int = 6) -> dict | None:
    """直近N月の実際の平均貯蓄額・貯蓄率を返す。"""
    rows = conn.execute(
        """SELECT year_month,
              SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) as income,
              SUM(CASE WHEN amount<0 THEN amount ELSE 0 END) as expense
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1
           GROUP BY year_month ORDER BY year_month DESC LIMIT ?""",
        (months,),
    ).fetchall()
    if not rows:
        return None
    incomes = [r[1] for r in rows]
    expenses = [abs(r[2]) for r in rows]
    avg_income = sum(incomes) / len(incomes)
    avg_expense = sum(expenses) / len(expenses)
    avg_savings = avg_income - avg_expense
    return {
        "avg_income": round(avg_income),
        "avg_expense": round(avg_expense),
        "avg_savings": round(avg_savings),
        "savings_rate": round(avg_savings / avg_income * 100, 1) if avg_income else 0,
        "months_used": len(rows),
    }


def get_cf_available_months(conn: sqlite3.Connection) -> list[dict]:
    """取引データ存在月リスト＋ダウンロード済み情報を返す。"""
    # 取引がある月 + 取引側のfetched日とカウント
    tx_rows = conn.execute(
        """SELECT year_month, MAX(fetched) as fetched, COUNT(*) as cnt
           FROM cf_transactions
           GROUP BY year_month ORDER BY year_month DESC"""
    ).fetchall()
    tx_map = {r[0]: {"fetched": r[1], "count": r[2]} for r in tx_rows}

    # ダウンロード記録
    csv_rows = conn.execute(
        "SELECT year_month, fetched, row_count FROM cf_csv_months ORDER BY year_month DESC"
    ).fetchall()
    csv_map = {r[0]: {"fetched": r[1], "row_count": r[2]} for r in csv_rows}

    all_months = sorted(set(tx_map.keys()) | set(csv_map.keys()), reverse=True)
    result = []
    for ym in all_months:
        has_data = ym in tx_map
        # fetched: csv_months優先、なければtransactionsから補完
        fetched = csv_map.get(ym, {}).get("fetched") or tx_map.get(ym, {}).get("fetched")
        row_count = csv_map.get(ym, {}).get("row_count") or tx_map.get(ym, {}).get("count", 0)
        result.append(
            {
                "year_month": ym,
                "has_data": has_data,
                "fetched": fetched,
                "row_count": row_count,
            }
        )
    return result
