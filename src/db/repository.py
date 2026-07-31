"""スナップショットの保存・取得を担うリポジトリ層。"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date as date_cls
from datetime import timedelta

from src.asset_classes import normalize_asset_classes
from src.parser.cashflow import CashflowMonth
from src.parser.cf_csv import CfTransaction
from src.parser.normalize import AssetSnapshot


def save_snapshot(conn: sqlite3.Connection, snapshot: AssetSnapshot, raw_path: str) -> None:
    """AssetSnapshotをDBに保存する。同日データがあれば差し替える。"""
    conn.execute(
        "INSERT OR REPLACE INTO snapshots (date, total_asset, by_class_json, raw_path) VALUES (?, ?, ?, ?)",
        (
            snapshot.date,
            snapshot.total_asset,
            json.dumps(normalize_asset_classes(snapshot.by_class), ensure_ascii=False),
            raw_path,
        ),
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


def get_daily_assets(conn: sqlite3.Connection, months: int = 6) -> list[dict]:
    """日次の総資産+資産クラス別データを返す（新しい順にN ヶ月分）。

    Returns: [{"date": "2026-02-15", "total": 21500000, "by_class": {...}}, ...]
    """
    from datetime import date, timedelta

    cutoff = (date.today() - timedelta(days=months * 30)).isoformat()
    rows = conn.execute(
        "SELECT date, total_asset, by_class_json FROM snapshots WHERE date >= ? ORDER BY date ASC",
        (cutoff,),
    ).fetchall()
    return [{"date": r[0], "total": r[1], "by_class": json.loads(r[2])} for r in rows]


def get_latest_stock_codes(conn: sqlite3.Connection) -> list[str]:
    """最新スナップショットで保有している株式コードを返す。"""
    row = conn.execute("SELECT MAX(date) FROM snapshots").fetchone()
    if not row or not row[0]:
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT symbol_or_code
        FROM snapshot_holdings
        WHERE date = ? AND asset_class = '株式（現物）' AND symbol_or_code <> ''
        ORDER BY symbol_or_code
        """,
        (row[0],),
    ).fetchall()
    return [r[0] for r in rows]


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


def get_budgets(conn: sqlite3.Connection) -> dict[str, int]:
    """月間予算を取得する。"""
    raw = get_setting(conn, "monthly_budgets", "{}")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


def save_budgets(conn: sqlite3.Connection, budgets: dict[str, int]) -> None:
    """月間予算を保存する。"""
    save_setting(conn, "monthly_budgets", json.dumps(budgets, ensure_ascii=False))


def calculate_investable_cash(
    conn: sqlite3.Connection,
    as_of: date_cls | None = None,
    snapshot_date: str | None = None,
) -> dict:
    """生活防衛資金と予定支出を控除した投資可能額を返す。"""
    as_of = as_of or date_cls.today()
    if snapshot_date is None:
        latest = conn.execute("SELECT MAX(date) FROM snapshots").fetchone()
        snapshot_date = latest[0] if latest and latest[0] else None

    cash_balance = 0.0
    if snapshot_date:
        row = conn.execute("SELECT by_class_json FROM snapshots WHERE date = ?", (snapshot_date,)).fetchone()
        if row:
            by_class = normalize_asset_classes(json.loads(row[0]))
            cash_balance = max(0.0, float(by_class.get("預金・現金", 0)))

    def non_negative_setting(key: str, default: float) -> float:
        try:
            return max(0.0, float(get_setting(conn, key, str(default)) or default))
        except (TypeError, ValueError):
            return default

    monthly_living_expense = non_negative_setting("monthly_living_expense", 0)
    expense_source = "setting"
    if monthly_living_expense <= 0:
        rows = conn.execute("SELECT expense FROM monthly_cashflows ORDER BY year_month DESC LIMIT 6").fetchall()
        monthly_living_expense = sum(max(0.0, float(row[0])) for row in rows) / len(rows) if rows else 0.0
        expense_source = "history" if rows else "unavailable"

    emergency_months = non_negative_setting("emergency_fund_months", 6)
    horizon_months = int(min(120, non_negative_setting("planned_expense_horizon_months", 12)))
    additional_reserve = non_negative_setting("additional_cash_reserve", 0)
    emergency_fund = monthly_living_expense * emergency_months

    horizon_end = as_of + timedelta(days=horizon_months * 30)
    planned_by_year = get_annual_life_event_expenses(
        conn,
        start_year=as_of.year,
        end_year=horizon_end.year,
        include_children=True,
    )
    planned_expenses = sum(planned_by_year.values())
    required_cash = emergency_fund + planned_expenses + additional_reserve
    raw_investable = cash_balance - required_cash
    return {
        "as_of": as_of.isoformat(),
        "snapshot_date": snapshot_date,
        "cash_balance": cash_balance,
        "monthly_living_expense": monthly_living_expense,
        "monthly_living_expense_source": expense_source,
        "emergency_fund_months": emergency_months,
        "emergency_fund": emergency_fund,
        "planned_expense_horizon_months": horizon_months,
        "planned_expenses": planned_expenses,
        "planned_expenses_by_year": planned_by_year,
        "additional_reserve": additional_reserve,
        "required_cash": required_cash,
        "investable_cash": max(0.0, raw_investable),
        "shortfall": max(0.0, -raw_investable),
        "formula": "cash - emergency_fund - planned_expenses - additional_reserve",
    }


# --- ライフイベント（ライフプラン） ---

EDUCATION_STAGE_RULES = [
    ("kindergarten", "保育園・幼稚園", 3, 5, {"public": 300_000, "private": 500_000}),
    ("elementary", "小学校", 6, 11, {"public": 350_000, "private": 1_600_000}),
    ("junior_high", "中学校", 12, 14, {"public": 500_000, "private": 1_400_000}),
    ("high_school", "高校", 15, 17, {"public": 500_000, "private": 1_000_000}),
    (
        "university",
        "大学",
        18,
        21,
        {
            "public": 1_000_000,
            "private_humanities": 1_500_000,
            "private_science": 2_000_000,
        },
    ),
]

DEFAULT_EDUCATION_PLAN = {
    "kindergarten": "public",
    "elementary": "public",
    "junior_high": "public",
    "high_school": "public",
    "university": "public",
}


def get_life_plan_inflation_rate(conn: sqlite3.Connection, default: float = 0.01) -> float:
    """ライフプランのグローバル物価上昇率を返す。"""
    row = conn.execute("SELECT inflation_rate FROM life_plan_settings WHERE id = 1").fetchone()
    if not row:
        return default
    with contextlib.suppress(ValueError, TypeError):
        return float(row[0])
    return default


def save_life_plan_inflation_rate(conn: sqlite3.Connection, inflation_rate: float) -> None:
    """ライフプランのグローバル物価上昇率を保存する。"""
    rate = max(0.0, min(0.10, float(inflation_rate)))
    conn.execute(
        """
        INSERT INTO life_plan_settings (id, inflation_rate, updated_at)
        VALUES (1, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            inflation_rate = excluded.inflation_rate,
            updated_at = datetime('now')
        """,
        (rate,),
    )
    conn.commit()


def list_life_events(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    """ライフイベント一覧を返す。"""
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT id, event_type, title, amount, start_year, repeat_every_years, end_year, enabled, note
        FROM life_events
        {where}
        ORDER BY start_year ASC, id ASC
        """
    ).fetchall()
    return [
        {
            "id": r[0],
            "event_type": r[1],
            "title": r[2],
            "amount": float(r[3]),
            "start_year": int(r[4]),
            "repeat_every_years": int(r[5]) if r[5] is not None else None,
            "end_year": int(r[6]) if r[6] is not None else None,
            "enabled": bool(r[7]),
            "note": r[8] or "",
        }
        for r in rows
    ]


def create_life_event(
    conn: sqlite3.Connection,
    event_type: str,
    title: str,
    amount: float,
    start_year: int,
    repeat_every_years: int | None = None,
    end_year: int | None = None,
    enabled: bool = True,
    note: str = "",
) -> int:
    """ライフイベントを作成し、id を返す。"""
    cur = conn.execute(
        """
        INSERT INTO life_events
            (event_type, title, amount, start_year, repeat_every_years, end_year, enabled, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            title.strip(),
            float(amount),
            int(start_year),
            int(repeat_every_years) if repeat_every_years else None,
            int(end_year) if end_year else None,
            1 if enabled else 0,
            note.strip(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_life_event(
    conn: sqlite3.Connection,
    event_id: int,
    event_type: str,
    title: str,
    amount: float,
    start_year: int,
    repeat_every_years: int | None = None,
    end_year: int | None = None,
    enabled: bool = True,
    note: str = "",
) -> None:
    """ライフイベントを更新する。"""
    conn.execute(
        """
        UPDATE life_events
        SET event_type = ?, title = ?, amount = ?, start_year = ?, repeat_every_years = ?, end_year = ?, enabled = ?, note = ?
        WHERE id = ?
        """,
        (
            event_type,
            title.strip(),
            float(amount),
            int(start_year),
            int(repeat_every_years) if repeat_every_years else None,
            int(end_year) if end_year else None,
            1 if enabled else 0,
            note.strip(),
            int(event_id),
        ),
    )
    conn.commit()


def delete_life_event(conn: sqlite3.Connection, event_id: int) -> None:
    """ライフイベントを削除する。"""
    conn.execute("DELETE FROM life_events WHERE id = ?", (int(event_id),))
    conn.commit()


def list_children_profiles(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    """子どもプロフィール一覧を返す。"""
    where = "WHERE enabled = 1" if enabled_only else ""
    rows = conn.execute(
        f"""
        SELECT id, name, birth_year, birth_month, education_plan_json, enabled
        FROM children_profiles
        {where}
        ORDER BY birth_year ASC, birth_month ASC, id ASC
        """
    ).fetchall()
    items: list[dict] = []
    for r in rows:
        raw = r[4] or "{}"
        try:
            plan = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            plan = {}
        merged_plan = dict(DEFAULT_EDUCATION_PLAN)
        merged_plan.update({k: v for k, v in plan.items() if isinstance(v, str)})
        items.append(
            {
                "id": int(r[0]),
                "name": r[1],
                "birth_year": int(r[2]),
                "birth_month": int(r[3]),
                "education_plan": merged_plan,
                "enabled": bool(r[5]),
            }
        )
    return items


def create_child_profile(
    conn: sqlite3.Connection,
    name: str,
    birth_year: int,
    birth_month: int,
    education_plan: dict | None = None,
    enabled: bool = True,
) -> int:
    """子どもプロフィールを作成し、id を返す。"""
    plan = dict(DEFAULT_EDUCATION_PLAN)
    if education_plan:
        plan.update({k: v for k, v in education_plan.items() if isinstance(v, str)})
    cur = conn.execute(
        """
        INSERT INTO children_profiles (name, birth_year, birth_month, education_plan_json, enabled)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            name.strip(),
            int(birth_year),
            max(1, min(12, int(birth_month))),
            json.dumps(plan, ensure_ascii=False),
            1 if enabled else 0,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_child_profile(
    conn: sqlite3.Connection,
    child_id: int,
    name: str,
    birth_year: int,
    birth_month: int,
    education_plan: dict | None = None,
    enabled: bool = True,
) -> None:
    """子どもプロフィールを更新する。"""
    plan = dict(DEFAULT_EDUCATION_PLAN)
    if education_plan:
        plan.update({k: v for k, v in education_plan.items() if isinstance(v, str)})
    conn.execute(
        """
        UPDATE children_profiles
        SET name = ?, birth_year = ?, birth_month = ?, education_plan_json = ?, enabled = ?
        WHERE id = ?
        """,
        (
            name.strip(),
            int(birth_year),
            max(1, min(12, int(birth_month))),
            json.dumps(plan, ensure_ascii=False),
            1 if enabled else 0,
            int(child_id),
        ),
    )
    conn.commit()


def delete_child_profile(conn: sqlite3.Connection, child_id: int) -> None:
    """子どもプロフィールを削除する。"""
    conn.execute("DELETE FROM children_profiles WHERE id = ?", (int(child_id),))
    conn.commit()


def build_education_events_for_child(
    child: dict,
    start_year: int,
    end_year: int,
    inflation_rate: float = 0.0,
    base_year: int | None = None,
) -> list[dict]:
    """子どもプロフィールから教育費イベント（年次）を生成する。"""
    if base_year is None:
        base_year = date_cls.today().year

    birth_year = int(child["birth_year"])
    birth_month = int(child.get("birth_month", 4))
    name = (child.get("name") or "").strip() or f"子ども{child.get('id', '')}"
    plan = dict(DEFAULT_EDUCATION_PLAN)
    plan.update(child.get("education_plan") or {})

    events: list[dict] = []
    for year in range(start_year, end_year + 1):
        # 1-3月生まれは学年進行が実質1年早い。
        school_age = year - birth_year + (1 if 1 <= birth_month <= 3 else 0)
        for stage_key, stage_label, min_age, max_age, costs in EDUCATION_STAGE_RULES:
            if not (min_age <= school_age <= max_age):
                continue
            option = plan.get(stage_key, "public")
            base_cost = costs.get(option, costs.get("public", 0))
            amount = float(base_cost) * ((1 + inflation_rate) ** max(0, year - base_year))
            events.append(
                {
                    "event_type": "education",
                    "title": f"{name} 教育費（{stage_label}）",
                    "amount": amount,
                    "start_year": year,
                    "repeat_every_years": None,
                    "end_year": year,
                    "enabled": True,
                    "note": stage_key,
                }
            )
            break
    return events


def expand_life_events_by_year(
    events: list[dict],
    start_year: int,
    end_year: int,
    inflation_rate: float = 0.0,
    base_year: int | None = None,
) -> dict[int, float]:
    """イベントを年次支出マップへ展開する。"""
    if base_year is None:
        base_year = date_cls.today().year

    annual: dict[int, float] = {}
    for ev in events:
        if not ev.get("enabled", True):
            continue
        first_year = int(ev.get("start_year", start_year))
        repeat = ev.get("repeat_every_years")
        repeat_years = int(repeat) if repeat not in (None, 0, "") else None
        until = ev.get("end_year")
        last_year = int(until) if until not in (None, "") else end_year
        last_year = min(last_year, end_year)
        amount_base = max(0.0, float(ev.get("amount", 0.0)))

        if repeat_years is None:
            years = [first_year]
        else:
            years = list(range(first_year, last_year + 1, repeat_years))

        for year in years:
            if year < start_year or year > end_year:
                continue
            amount = amount_base * ((1 + inflation_rate) ** max(0, year - base_year))
            annual[year] = annual.get(year, 0.0) + amount
    return annual


def get_annual_life_event_expenses(
    conn: sqlite3.Connection,
    start_year: int,
    end_year: int,
    include_children: bool = True,
) -> dict[int, float]:
    """DB上の有効イベントを年次支出マップに集約する。"""
    inflation_rate = get_life_plan_inflation_rate(conn)
    base_year = date_cls.today().year

    events = list_life_events(conn, enabled_only=True)
    if include_children:
        for child in list_children_profiles(conn, enabled_only=True):
            events.extend(
                build_education_events_for_child(
                    child=child,
                    start_year=start_year,
                    end_year=end_year,
                    inflation_rate=0.0,
                    base_year=base_year,
                )
            )

    return expand_life_events_by_year(
        events=events,
        start_year=start_year,
        end_year=end_year,
        inflation_rate=inflation_rate,
        base_year=base_year,
    )


# --- CF (家計簿) ---


def _japanese_holidays(year: int) -> set:
    """指定年の日本の祝日を返す。

    対象: 固定日祝日、ハッピーマンデー、春分/秋分の日、振替休日、国民の休日。
    """
    from datetime import date, timedelta

    holidays: set[date] = set()

    # --- 固定日 ---
    fixed = [
        (1, 1),  # 元日
        (2, 11),  # 建国記念の日
        (4, 29),  # 昭和の日
        (5, 3),  # 憲法記念日
        (5, 4),  # みどりの日
        (5, 5),  # こどもの日
        (11, 3),  # 文化の日
        (11, 23),  # 勤労感謝の日
    ]
    for m, d in fixed:
        holidays.add(date(year, m, d))

    # 天皇誕生日: 〜2018は12/23（平成）、2019は空白、2020〜は2/23（令和）
    if year <= 2018:
        holidays.add(date(year, 12, 23))
    elif year >= 2020:
        holidays.add(date(year, 2, 23))

    # 山の日 (2016〜、2020/2021はオリンピック特例で移動)
    if year == 2020:
        holidays.add(date(2020, 8, 10))
    elif year == 2021:
        holidays.add(date(2021, 8, 8))
    elif year >= 2016:
        holidays.add(date(year, 8, 11))

    # --- ハッピーマンデー（第N月曜日）---
    def nth_monday(y: int, m: int, n: int) -> date:
        first = date(y, m, 1)
        # 最初の月曜日の日
        monday = 1 + (7 - first.weekday()) % 7
        return date(y, m, monday + 7 * (n - 1))

    holidays.add(nth_monday(year, 1, 2))  # 成人の日（1月第2月曜）
    holidays.add(nth_monday(year, 9, 3))  # 敬老の日（9月第3月曜）

    # 海の日 (2020/2021はオリンピック特例)
    if year == 2020:
        holidays.add(date(2020, 7, 23))
    elif year == 2021:
        holidays.add(date(2021, 7, 22))
    else:
        holidays.add(nth_monday(year, 7, 3))

    # スポーツの日 (2020/2021はオリンピック特例)
    if year == 2020:
        holidays.add(date(2020, 7, 24))
    elif year == 2021:
        holidays.add(date(2021, 7, 23))
    else:
        holidays.add(nth_monday(year, 10, 2))

    # --- 春分の日・秋分の日（近似式、2000〜2099年対応）---
    vernal = int(20.8431 + 0.242194 * (year - 1980)) - int((year - 1980) / 4)
    autumnal = int(23.2488 + 0.242194 * (year - 1980)) - int((year - 1980) / 4)
    holidays.add(date(year, 3, vernal))
    holidays.add(date(year, 9, autumnal))

    # --- 振替休日（祝日が日曜 → 翌月曜が休み）---
    for h in sorted(holidays.copy()):
        if h.weekday() == 6:  # 日曜
            sub = h + timedelta(days=1)
            while sub in holidays:
                sub += timedelta(days=1)
            holidays.add(sub)

    # --- 国民の休日（前後が祝日の平日）---
    sorted_h = sorted(holidays)
    for i in range(len(sorted_h) - 1):
        between = sorted_h[i] + timedelta(days=1)
        if between + timedelta(days=1) == sorted_h[i + 1] and between not in holidays and between.weekday() < 5:
            holidays.add(between)

    return holidays


def _adjusted_closing_date(year: int, month: int, closing_day: int, holiday_mode: str):
    """指定月の締め日を土日祝に応じて調整した日付を返す。

    holiday_mode:
      "none"   — 変更しない
      "before" — 設定日前の平日
      "after"  — 設定日後の平日
    """
    import calendar
    from datetime import date, timedelta

    max_day = calendar.monthrange(year, month)[1]
    base_day = min(closing_day, max_day)
    d = date(year, month, base_day)

    if holiday_mode == "none":
        return d

    holidays = _japanese_holidays(year)
    # 年をまたぐ場合に備えて隣接年の祝日も取得
    if holiday_mode == "before":
        extra = _japanese_holidays(year - 1) if month == 1 else set()
        all_holidays = holidays | extra
        while d.weekday() >= 5 or d in all_holidays:
            d -= timedelta(days=1)
            if d.year != year:
                all_holidays = _japanese_holidays(d.year) | holidays
    elif holiday_mode == "after":
        extra = _japanese_holidays(year + 1) if month == 12 else set()
        all_holidays = holidays | extra
        while d.weekday() >= 5 or d in all_holidays:
            d += timedelta(days=1)
            if d.year != year:
                all_holidays = _japanese_holidays(d.year) | holidays

    return d


def _fiscal_month_expr(closing_day: int, holiday_mode: str = "none", conn: sqlite3.Connection | None = None) -> str:
    """締め日に応じた fiscal month の SQL 式を返す。

    closing_day=1: 暦月（year_month カラムをそのまま使用）
    closing_day=25: date が 25日以降 → 翌月扱い
      例: 2025-01-25 → '2025-02', 2025-01-24 → '2025-01'

    holiday_mode が "none" 以外の場合、祝日・土日を考慮した
    事前計算済み境界日の CASE 式を生成する。conn を渡すと
    データの実際の年範囲から境界を生成する。
    """
    if closing_day <= 1:
        return "year_month"
    if holiday_mode == "none":
        # 月末日を超えない実効締め日で比較（closing_day=31 で4月なら 30 にクランプ）
        last_day = "CAST(strftime('%d', date, 'start of month', '+1 month', '-1 day') AS INTEGER)"
        return (
            f"CASE WHEN CAST(substr(date,9,2) AS INTEGER) >= min({closing_day}, {last_day}) "
            f"THEN strftime('%Y-%m', date, 'start of month', '+1 month') "
            f"ELSE substr(date,1,7) END"
        )

    # 祝日調整あり: 月ごとに境界日が異なるため、事前計算した CASE を生成
    from datetime import date

    today = date.today()

    # データの実際の年範囲を取得（conn があれば）
    min_year = today.year - 10  # フォールバック
    if conn is not None:
        row = conn.execute("SELECT MIN(substr(date,1,4)) FROM cf_transactions").fetchone()
        if row and row[0]:
            min_year = int(row[0]) - 1  # 前月の締め日が前年になるケースに備えて -1

    boundaries: list[tuple[str, str]] = []
    for y in range(min_year, today.year + 2):
        for m in range(1, 13):
            adj = _adjusted_closing_date(y, m, closing_day, holiday_mode)
            # adj は「y年m月の締め日」= 翌 fiscal month の開始日
            next_m = m + 1
            next_y = y
            if next_m > 12:
                next_m = 1
                next_y = y + 1
            fm = f"{next_y}-{next_m:02d}"
            boundaries.append((adj.isoformat(), fm))
    # 降順にして、最初にマッチした（= 最も新しい境界）が採用される
    boundaries.sort(key=lambda x: x[0], reverse=True)
    cases = " ".join(f"WHEN date >= '{bd}' THEN '{fm}'" for bd, fm in boundaries)
    return f"CASE {cases} ELSE substr(date,1,7) END"


def _current_fiscal_month(closing_day: int, holiday_mode: str = "none", *, _today=None) -> str:
    """現在の fiscal month を返す。

    closing_day=25, 今日=2/18 → まだ2月の期間中なので '2026-02'
    closing_day=25, 今日=2/26 → 3月の期間に入っているので '2026-03'

    holiday_mode="after" で調整済み締め日が翌月にスピルオーバーする場合は
    前月の調整済み締め日を確認し、holiday_mode="before" で翌月の締め日が
    当月に前倒しされる場合は翌月の調整済み締め日も確認する。
    """
    from datetime import date

    today = _today or date.today()
    if closing_day <= 1:
        return today.strftime("%Y-%m")

    if holiday_mode == "none":
        import calendar

        last_day = calendar.monthrange(today.year, today.month)[1]
        if today.day < min(closing_day, last_day):
            return today.strftime("%Y-%m")
    else:
        # "after": 前月の調整済み締め日が翌月にスピルオーバーしている可能性
        prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
        prev_adj = _adjusted_closing_date(prev_year, prev_month, closing_day, holiday_mode)
        if today < prev_adj:
            return f"{prev_year}-{prev_month:02d}"

        adj = _adjusted_closing_date(today.year, today.month, closing_day, holiday_mode)
        if today < adj:
            return today.strftime("%Y-%m")

        # "before": 翌月の調整済み締め日が当月に前倒しされている可能性
        next_year, next_month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
        next_adj = _adjusted_closing_date(next_year, next_month, closing_day, holiday_mode)
        if today >= next_adj:
            if next_month == 12:
                return f"{next_year + 1}-01"
            return f"{next_year}-{next_month + 1:02d}"

    # closing_day 以降 → 翌月の fiscal month
    if today.month == 12:
        return f"{today.year + 1}-01"
    return f"{today.year}-{today.month + 1:02d}"


def _fiscal_month_range(year_month: str, closing_day: int, holiday_mode: str = "none") -> tuple[str, str]:
    """指定 fiscal month の開始日・終了日を返す。

    closing_day=1: 暦月（2025-02-01 〜 2025-02-28）
    closing_day=25: 2025-02 → 2025-01-25 〜 2025-02-24
    holiday_mode で土日祝の調整を反映する。
    """
    import re
    from datetime import date, timedelta

    if not re.fullmatch(r"\d{4}-\d{2}", year_month or ""):
        return "1970-01-01", "1970-01-31"
    year, month = int(year_month[:4]), int(year_month[5:7])
    if not (1 <= month <= 12):
        return "1970-01-01", "1970-01-31"

    if closing_day <= 1:
        start = date(year, month, 1)
        # 翌月1日の前日 = 今月末日
        if month == 12:
            end = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(year, month + 1, 1) - timedelta(days=1)
    elif holiday_mode == "none":
        # 前月の closing_day 〜 今月の closing_day - 1
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1

        import calendar

        # 前月の closing_day（月末日を超えないよう調整）
        max_day_prev = calendar.monthrange(prev_year, prev_month)[1]
        start_day = min(closing_day, max_day_prev)
        start = date(prev_year, prev_month, start_day)

        # 今月の closing_day - 1
        max_day_cur = calendar.monthrange(year, month)[1]
        end_day = min(closing_day - 1, max_day_cur)
        end = date(year, month, end_day)
    else:
        # 祝日調整あり: 前月・今月の調整済み締め日を使う
        if month == 1:
            prev_year, prev_month = year - 1, 12
        else:
            prev_year, prev_month = year, month - 1

        start = _adjusted_closing_date(prev_year, prev_month, closing_day, holiday_mode)
        end = _adjusted_closing_date(year, month, closing_day, holiday_mode) - timedelta(days=1)

    return start.isoformat(), end.isoformat()


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


def get_cf_category_summary(
    conn: sqlite3.Connection, year_month: str, closing_day: int = 1, holiday_mode: str = "none"
) -> dict:
    """指定月の大項目別・中項目別集計、収支合計、高額TOP15を返す。"""
    if closing_day <= 1:
        base_where = "WHERE year_month = ? AND is_transfer = 0 AND is_target = 1"
        params: tuple = (year_month,)
    else:
        start, end = _fiscal_month_range(year_month, closing_day, holiday_mode)
        base_where = "WHERE date >= ? AND date <= ? AND is_transfer = 0 AND is_target = 1"
        params = (start, end)

    # 大項目別支出（amount < 0）
    rows = conn.execute(
        f"""SELECT major_category, SUM(amount) as total
            FROM cf_transactions {base_where} AND amount < 0
            GROUP BY major_category ORDER BY total ASC""",
        params,
    ).fetchall()
    major_categories = [{"name": r[0], "total": abs(r[1])} for r in rows if r[0]]

    # 中項目別支出（大項目ごと）
    minor_rows = conn.execute(
        f"""SELECT major_category, minor_category, SUM(amount) as total
            FROM cf_transactions {base_where} AND amount < 0
            GROUP BY major_category, minor_category ORDER BY major_category, total ASC""",
        params,
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
        params,
    ).fetchone()
    total_expense = abs(totals[0]) if totals[0] else 0
    total_income = totals[1] if totals[1] else 0

    # 高額支出 TOP15
    top_rows = conn.execute(
        f"""SELECT date, description, amount, major_category, minor_category, institution
            FROM cf_transactions {base_where} AND amount < 0
            ORDER BY amount ASC LIMIT 15""",
        params,
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


def get_cf_monthly_trend(
    conn: sqlite3.Connection, months: int = 12, closing_day: int = 1, holiday_mode: str = "none"
) -> list[dict]:
    """月別収入・支出推移を返す（新しい順 → 古い順に並び替え）。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)
    rows = conn.execute(
        f"""SELECT {fm} as fm,
              SUM(CASE WHEN amount < 0 THEN amount ELSE 0 END) as expense,
              SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as income
           FROM cf_transactions
           WHERE is_transfer = 0 AND is_target = 1
           GROUP BY fm
           HAVING fm <= ?
           ORDER BY fm DESC
           LIMIT ?""",
        (cur_fm, months),
    ).fetchall()
    result = [{"year_month": r[0], "expense": abs(r[1]) if r[1] else 0, "income": r[2] if r[2] else 0} for r in rows]
    result.reverse()
    return result


def get_cf_category_trend(
    conn: sqlite3.Connection, months: int = 6, closing_day: int = 1, holiday_mode: str = "none"
) -> dict:
    """カテゴリ別月次推移を返す。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)
    ym_rows = conn.execute(
        f"""SELECT DISTINCT {fm} as fm FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
           GROUP BY fm HAVING fm <= ?
           ORDER BY fm DESC LIMIT ?""",
        (cur_fm, months),
    ).fetchall()
    year_months = [r[0] for r in reversed(ym_rows)]

    if not year_months:
        return {"year_months": [], "categories": [], "by_month": {}, "avg_by_category": {}, "avg_months": 0}

    rows = conn.execute(
        f"""SELECT {fm} as fm, major_category, SUM(amount) as total
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND {fm} IN ({",".join("?" * len(year_months))})
           GROUP BY fm, major_category""",
        year_months,
    ).fetchall()

    cat_totals: dict[str, float] = {}
    for r in rows:
        cat_totals[r[1]] = cat_totals.get(r[1], 0) + abs(r[2])
    categories = sorted(cat_totals, key=lambda c: cat_totals[c], reverse=True)

    by_month: dict[str, dict[str, float]] = {}
    for r in rows:
        by_month.setdefault(r[0], {})[r[1]] = abs(r[2])

    months_used = len(year_months)
    avg_by_category = {cat: cat_totals[cat] / months_used for cat in categories}

    return {
        "year_months": year_months,
        "categories": categories,
        "by_month": by_month,
        "avg_by_category": avg_by_category,
        "avg_months": months_used,
    }


def get_cf_category_details_history(
    conn: sqlite3.Connection, months: int = 6, closing_day: int = 1, holiday_mode: str = "none"
) -> dict:
    """カテゴリ別に、直近Nヶ月の支出明細を返す。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)
    ym_rows = conn.execute(
        f"""SELECT DISTINCT {fm} as fm FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
           GROUP BY fm HAVING fm <= ?
           ORDER BY fm DESC LIMIT ?""",
        (cur_fm, months),
    ).fetchall()
    year_months = [r[0] for r in reversed(ym_rows)]
    if not year_months:
        return {"year_months": [], "categories": [], "by_category": {}}

    rows = conn.execute(
        f"""SELECT {fm} as fm, date, description, amount, major_category, minor_category, institution
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND {fm} IN ({",".join("?" * len(year_months))})
           ORDER BY fm DESC, amount ASC, date ASC""",
        year_months,
    ).fetchall()

    by_category: dict[str, list[dict]] = {}
    category_totals: dict[str, float] = {}
    for r in rows:
        major = r[4] or "未分類"
        amt = abs(float(r[3]))
        by_category.setdefault(major, []).append(
            {
                "year_month": r[0],
                "date": r[1],
                "description": r[2],
                "amount": amt,
                "minor_category": r[5] or "未分類",
                "institution": r[6] or "",
            }
        )
        category_totals[major] = category_totals.get(major, 0.0) + amt

    categories = sorted(category_totals, key=lambda c: category_totals[c], reverse=True)
    return {"year_months": year_months, "categories": categories, "by_category": by_category}


def get_cf_fixed_expenses(
    conn: sqlite3.Connection, months: int = 3, closing_day: int = 1, holiday_mode: str = "none"
) -> dict:
    """固定費候補を検出する。

    固定費 = 契約・自動引き落としで毎月ほぼ同額が出ていく支出。
    （家賃、管理費、保険、通信費、サブスク、光熱費等）
    カフェや美容院のような「習慣的だが裁量的な支出」は変動費。

    判定条件（当月を除く確定月のみで判定）:
    - 金額のブレが10%以内（自動引き落としは同額になる）
    - 「現金・カード」カテゴリは除外（二重計上防止）
    - 確定月に2回以上出現、または確定月+当月で同額なら固定費と判定
    """
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    current_ym = _current_fiscal_month(closing_day, holiday_mode)

    ym_rows = conn.execute(
        f"""SELECT DISTINCT {fm} as fm FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
           GROUP BY fm HAVING fm <= ?
           ORDER BY fm DESC LIMIT ?""",
        (current_ym, months + 1),
    ).fetchall()
    # 当月は途中データなので判定対象から除外
    year_months = [r[0] for r in ym_rows if r[0] != current_ym][:months]
    if len(year_months) < 2:
        return {"fixed": [], "variable_total": 0, "fixed_total": 0, "fixed_ratio": 0, "months_used": len(year_months)}

    # 「現金・カード」はカード引き落とし等で二重計上になるため除外
    _exclude_major = "現金・カード"

    rows = conn.execute(
        f"""SELECT {fm} as fm, major_category, minor_category, SUM(amount)
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND major_category != ?
             AND {fm} IN ({",".join("?" * len(year_months))})
           GROUP BY fm, major_category, minor_category""",
        [_exclude_major] + year_months,
    ).fetchall()

    pair_months: dict[tuple[str, str], dict[str, float]] = {}
    for r in rows:
        key = (r[1], r[2])
        pair_months.setdefault(key, {})[r[0]] = abs(r[3])

    # 当月データも参照用に取得（判定月には含めないが補助判定に使う）
    current_rows = conn.execute(
        f"""SELECT major_category, minor_category, SUM(amount)
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount<0
             AND major_category != ?
             AND {fm}=?
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


def get_cf_income_breakdown(
    conn: sqlite3.Connection, year_month: str, closing_day: int = 1, holiday_mode: str = "none"
) -> dict:
    """収入の中項目別内訳を返す。"""
    if closing_day <= 1:
        where = "WHERE year_month=? AND is_transfer=0 AND is_target=1 AND amount>0"
        params: tuple = (year_month,)
    else:
        start, end = _fiscal_month_range(year_month, closing_day, holiday_mode)
        where = "WHERE date>=? AND date<=? AND is_transfer=0 AND is_target=1 AND amount>0"
        params = (start, end)

    rows = conn.execute(
        f"""SELECT minor_category, SUM(amount) as total
           FROM cf_transactions
           {where}
           GROUP BY minor_category ORDER BY total DESC""",
        params,
    ).fetchall()
    items = [{"name": r[0] or "未分類", "total": r[1]} for r in rows]
    total = sum(i["total"] for i in items)
    return {"items": items, "total": total}


def get_cf_income_trend(
    conn: sqlite3.Connection, months: int = 6, closing_day: int = 1, holiday_mode: str = "none"
) -> list[dict]:
    """月別の収入推移を返す。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)
    rows = conn.execute(
        f"""SELECT {fm} as fm, SUM(amount) as total
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount>0
           GROUP BY fm
           HAVING fm <= ?
           ORDER BY fm DESC LIMIT ?""",
        (cur_fm, months),
    ).fetchall()
    result = [{"year_month": r[0], "income": r[1]} for r in rows]
    result.reverse()
    return result


def get_cf_actual_savings(
    conn: sqlite3.Connection, months: int = 6, closing_day: int = 1, holiday_mode: str = "none"
) -> dict | None:
    """直近N月の実際の平均貯蓄額・貯蓄率を返す。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)
    rows = conn.execute(
        f"""SELECT {fm} as fm,
              SUM(CASE WHEN amount>0 THEN amount ELSE 0 END) as income,
              SUM(CASE WHEN amount<0 THEN amount ELSE 0 END) as expense
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1
           GROUP BY fm
           HAVING fm <= ?
           ORDER BY fm DESC LIMIT ?""",
        (cur_fm, months),
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


def get_cf_available_months(conn: sqlite3.Connection, closing_day: int = 1, holiday_mode: str = "none") -> list[dict]:
    """取引データ存在月リスト＋ダウンロード済み情報を返す。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)

    # 取引がある月 + 取引側のfetched日とカウント（未来の fiscal month を除外）
    tx_rows = conn.execute(
        f"""SELECT {fm} as fm, MAX(fetched) as fetched, COUNT(*) as cnt
           FROM cf_transactions
           GROUP BY fm
           HAVING fm <= ?
           ORDER BY fm DESC""",
        (cur_fm,),
    ).fetchall()
    tx_map = {r[0]: {"fetched": r[1], "count": r[2]} for r in tx_rows}

    # ダウンロード記録（暦月ベースのまま）
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


def get_cf_dividend_history(conn: sqlite3.Connection, closing_day: int = 1, holiday_mode: str = "none") -> dict:
    """配当・分配金の月別・年別実績を返す。"""
    fm = _fiscal_month_expr(closing_day, holiday_mode, conn)
    cur_fm = _current_fiscal_month(closing_day, holiday_mode)
    rows = conn.execute(
        f"""SELECT {fm} as fm, SUM(amount) as total
           FROM cf_transactions
           WHERE is_transfer=0 AND is_target=1 AND amount>0
             AND (minor_category LIKE '%配当%' OR minor_category LIKE '%分配%'
                  OR minor_category LIKE '%利息%')
           GROUP BY fm
           HAVING fm <= ?
           ORDER BY fm ASC""",
        (cur_fm,),
    ).fetchall()
    monthly = [{"year_month": r[0], "amount": r[1]} for r in rows]

    # 年別集計
    yearly: dict[str, int] = {}
    for m in monthly:
        year = m["year_month"][:4]
        yearly[year] = yearly.get(year, 0) + m["amount"]
    annual = [{"year": y, "amount": a} for y, a in sorted(yearly.items())]

    return {"monthly": monthly, "annual": annual}


# --- 投資信託 評価額・取得価額推移 ---


def get_fund_total_history(conn: sqlite3.Connection, months: int = 13) -> list[dict]:
    """投資信託の評価額合計・取得価額合計の時系列データを返す。

    取得価額 = value - unrealized_gain で算出。
    ある日付で一部銘柄でも unrealized_gain が NULL なら、その日の total_cost は NULL とする。

    Args:
        months: 取得期間（月数、1ヶ月=30日換算）。デフォルト13ヶ月（JS側の1年表示+余裕）。

    Returns: [{"date": "2026-02-15", "total_value": 6280000, "total_cost": 5500000}, ...]
    """
    # DB 内の最新日を基準にカットオフを計算
    latest_row = conn.execute("SELECT MAX(date) FROM snapshot_holdings WHERE asset_class = '投資信託'").fetchone()
    if not latest_row or not latest_row[0]:
        return []

    latest_dt = date_cls.fromisoformat(latest_row[0])
    cutoff = (latest_dt - timedelta(days=months * 30)).isoformat()

    rows = conn.execute(
        """SELECT date,
                  SUM(value),
                  CASE WHEN COUNT(*) = COUNT(unrealized_gain)
                       THEN SUM(value - unrealized_gain)
                  END
           FROM snapshot_holdings
           WHERE asset_class = '投資信託' AND date >= ?
           GROUP BY date
           ORDER BY date ASC""",
        (cutoff,),
    ).fetchall()

    return [{"date": r[0], "total_value": r[1], "total_cost": r[2]} for r in rows if r[1] is not None]


def get_holding_history(
    conn: sqlite3.Connection,
    asset_class: str,
    name: str,
    symbol_or_code: str = "",
    months: int = 13,
) -> list[dict]:
    """指定銘柄の評価額・取得価額の時系列データを返す。"""
    code = (symbol_or_code or "").strip()
    latest_row = conn.execute(
        """
        SELECT MAX(date)
        FROM snapshot_holdings
        WHERE asset_class = ?
          AND name = ?
          AND symbol_or_code = ?
        """,
        (asset_class, name, code),
    ).fetchone()
    if not latest_row or not latest_row[0]:
        return []

    latest_dt = date_cls.fromisoformat(latest_row[0])
    cutoff = (latest_dt - timedelta(days=months * 30)).isoformat()

    rows = conn.execute(
        """
        SELECT date,
               SUM(value),
               CASE WHEN COUNT(*) = COUNT(unrealized_gain)
                    THEN SUM(value - unrealized_gain)
               END
        FROM snapshot_holdings
        WHERE asset_class = ?
          AND name = ?
          AND symbol_or_code = ?
          AND date >= ?
        GROUP BY date
        ORDER BY date ASC
        """,
        (asset_class, name, code, cutoff),
    ).fetchall()

    return [{"date": r[0], "total_value": r[1], "total_cost": r[2]} for r in rows if r[1] is not None]
