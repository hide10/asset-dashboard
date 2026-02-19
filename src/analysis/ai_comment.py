"""Gemini APIを使ったAI分析コメント生成。

Gemini 2.5 Flash 無料枠（250リクエスト/日）を使用。
この機能は最大3回/日（dashboard + lifeplan + cf）で十分に収まる。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime

from src.db.repository import (
    get_budgets,
    get_cashflows,
    get_cf_available_months,
    get_cf_category_summary,
    get_cf_fixed_expenses,
    get_cf_monthly_trend,
    get_setting,
)
from src.db.schema import init_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# APIキー取得
# ---------------------------------------------------------------------------


def _get_api_key(db_path: str) -> str | None:
    """APIキーを取得する。環境変数 > DB settings の順で探す。"""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    conn = init_db(db_path)
    key = get_setting(conn, "gemini_api_key")
    conn.close()
    return key


# ---------------------------------------------------------------------------
# DB操作
# ---------------------------------------------------------------------------


def save_comment(conn: sqlite3.Connection, date: str, page: str, comment: str) -> None:
    """AIコメントをDBに保存する。"""
    conn.execute(
        "INSERT OR REPLACE INTO ai_comments (date, page, comment, created_at) VALUES (?, ?, ?, ?)",
        (date, page, comment, datetime.now().isoformat()),
    )
    conn.commit()


def get_comment(conn: sqlite3.Connection, date: str, page: str) -> str | None:
    """保存済みAIコメントを取得する。"""
    row = conn.execute(
        "SELECT comment FROM ai_comments WHERE date = ? AND page = ?",
        (date, page),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# プロンプト生成
# ---------------------------------------------------------------------------


def _build_dashboard_prompt(db_path: str, date: str) -> str:
    """ダッシュボード用の分析プロンプトを組み立てる。"""
    from src.analysis.compare import get_all_comparisons
    from src.data.stock_master import get_dividend, get_sector, is_us_stock

    conn = init_db(db_path)

    row = conn.execute("SELECT total_asset, by_class_json FROM snapshots WHERE date = ?", (date,)).fetchone()
    if not row:
        conn.close()
        return ""
    total_asset = row[0]
    by_class = json.loads(row[1])

    class_lines = []
    for cls, amt in by_class.items():
        ratio = amt / total_asset * 100 if total_asset else 0
        class_lines.append(f"  {cls}: {amt:,.0f}円 ({ratio:.1f}%)")

    comparisons = get_all_comparisons(db_path, date)
    comp_lines = []
    for comp in comparisons:
        if comp.total_diff is not None:
            sign = "+" if comp.total_diff >= 0 else ""
            ratio_str = f"{sign}{comp.total_ratio:.2f}%" if comp.total_ratio is not None else ""
            comp_lines.append(f"  {comp.label}: {sign}{comp.total_diff:,.0f}円 ({ratio_str})")
            if comp.by_class_diff:
                sorted_diffs = sorted(comp.by_class_diff.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
                for cls_name, diff in sorted_diffs:
                    s = "+" if diff >= 0 else ""
                    comp_lines.append(f"    {cls_name}: {s}{diff:,.0f}円")

    holdings = conn.execute(
        "SELECT name, symbol_or_code, asset_class, value, quantity FROM snapshot_holdings WHERE date = ? ORDER BY value DESC",
        (date,),
    ).fetchall()
    sector_totals: dict[str, float] = {}
    total_dividend = 0.0
    usd_jpy = 150.0
    for h in holdings:
        name, code, asset_class, value, quantity = h
        if asset_class == "株式（現物）" and code:
            sector = get_sector(code)
            sector_totals[sector] = sector_totals.get(sector, 0) + value
            if quantity:
                dps = get_dividend(code)
                dps_jpy = dps * usd_jpy if is_us_stock(code) else dps
                total_dividend += dps_jpy * quantity

    sector_sorted = sorted(sector_totals.items(), key=lambda x: x[1], reverse=True)[:5]
    sector_lines = [f"  {sec}: {amt:,.0f}円" for sec, amt in sector_sorted]

    stock_total = sum(sector_totals.values())
    div_yield = (total_dividend / stock_total * 100) if stock_total > 0 else 0

    conn.close()

    data_text = f"""【資産データ ({date})】
総資産: {total_asset:,.0f}円

■ 資産クラス別内訳:
{chr(10).join(class_lines)}

■ 変動:
{chr(10).join(comp_lines) if comp_lines else "  比較データなし"}

■ 業種別内訳（上位5件）:
{chr(10).join(sector_lines) if sector_lines else "  株式データなし"}

■ 年間配当予測: {total_dividend:,.0f}円（利回り {div_yield:.2f}%）"""

    return f"""{data_text}

あなたはファイナンシャルアドバイザーです。上記の資産データを分析し、3〜4文で簡潔にコメントしてください。
ポートフォリオの特徴、変動の要因、改善点などに触れてください。日本語で回答してください。"""


def _build_lifeplan_prompt(db_path: str, date: str) -> str:
    """ライフプラン用の分析プロンプトを組み立てる。"""
    from src.prediction.montecarlo import RISK_CLASSES, predict_no_contribution, predict_with_contribution

    conn = init_db(db_path)

    row = conn.execute("SELECT total_asset, by_class_json FROM snapshots WHERE date = ?", (date,)).fetchone()
    if not row:
        conn.close()
        return ""
    total_asset = row[0]
    by_class = json.loads(row[1])

    # 月次資産推移
    rows = conn.execute("SELECT date, total_asset FROM snapshots ORDER BY date ASC").fetchall()
    monthly_end: dict[str, float] = {}
    for date_str, total in rows:
        ym = date_str[:7]
        monthly_end[ym] = total
    monthly_totals = sorted(monthly_end.items())

    trend_lines = []
    for ym, total in monthly_totals[-6:]:  # 直近6ヶ月
        trend_lines.append(f"  {ym}: {total:,.0f}円")

    # 月次収支
    cashflows = get_cashflows(conn, limit=6)
    cashflows.reverse()
    cf_lines = []
    for cf in cashflows:
        net = cf["income"] - cf["expense"]
        sign = "+" if net >= 0 else ""
        cf_lines.append(
            f"  {cf['year_month']}: 収入{cf['income']:,.0f}円 / 支出{cf['expense']:,.0f}円 / 収支{sign}{net:,.0f}円"
        )

    conn.close()

    # 成長予測
    risk_value = sum(v for cls, v in by_class.items() if cls in RISK_CLASSES)
    safe_value = total_asset - risk_value

    pred_lines = []
    try:
        predictions, params = predict_no_contribution(
            db_path, risk_value, safe_value, years_list=[1, 3, 5], simulations=2000
        )
        for p in predictions:
            pred_lines.append(f"  {p.years}年後: P10={p.p10:,.0f}円 / P50={p.p50:,.0f}円 / P90={p.p90:,.0f}円")
    except Exception:
        pass

    monthly_contribution = 50000.0
    try:
        conn2 = init_db(db_path)
        mc_str = get_setting(conn2, "monthly_contribution", "50000")
        monthly_contribution = float(mc_str) if mc_str else 50000.0
        conn2.close()
    except Exception:
        pass

    pred_c_lines = []
    try:
        predictions_c, _ = predict_with_contribution(
            db_path, risk_value, safe_value, monthly_contribution, years_list=[1, 3, 5], simulations=2000
        )
        for p in predictions_c:
            pred_c_lines.append(f"  {p.years}年後: P10={p.p10:,.0f}円 / P50={p.p50:,.0f}円 / P90={p.p90:,.0f}円")
    except Exception:
        pass

    data_text = f"""【ライフプランデータ ({date})】
現在の総資産: {total_asset:,.0f}円

■ 月次資産推移（直近6ヶ月）:
{chr(10).join(trend_lines) if trend_lines else "  データなし"}

■ 月次収支（直近6ヶ月）:
{chr(10).join(cf_lines) if cf_lines else "  データなし"}

■ 成長予測（追加投資なし）:
{chr(10).join(pred_lines) if pred_lines else "  データ不足"}

■ 成長予測（月額{monthly_contribution:,.0f}円積立込み）:
{chr(10).join(pred_c_lines) if pred_c_lines else "  データ不足"}"""

    return f"""{data_text}

あなたはファイナンシャルプランナーです。上記のライフプランデータを分析し、3〜4文で簡潔にコメントしてください。
資産推移のトレンド、収支バランス、将来の見通しなどに触れてください。日本語で回答してください。"""


def _build_cf_prompt(db_path: str, year_month: str) -> str:
    """家計簿分析用のプロンプトを組み立てる。"""
    conn = init_db(db_path)
    try:
        closing_day = int(get_setting(conn, "closing_day", "1") or "1")
        holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"
        summary = get_cf_category_summary(conn, year_month, closing_day=closing_day, holiday_mode=holiday_mode)
        trend = get_cf_monthly_trend(conn, months=6, closing_day=closing_day, holiday_mode=holiday_mode)
        fixed = get_cf_fixed_expenses(conn, months=3, closing_day=closing_day, holiday_mode=holiday_mode)
        budgets = get_budgets(conn)
    finally:
        conn.close()

    if not summary:
        return ""

    # カテゴリ別支出
    cat_lines = []
    for c in summary["major_categories"]:
        budget_info = ""
        if budgets.get(c["name"]):
            pct = c["total"] / budgets[c["name"]] * 100
            budget_info = f" (予算{budgets[c['name']]:,.0f}円, 消化率{pct:.0f}%)"
        cat_lines.append(f"  {c['name']}: {c['total']:,.0f}円{budget_info}")

    # 月別推移
    trend_lines = []
    for t in trend[-6:]:
        net = t["income"] - t["expense"]
        sign = "+" if net >= 0 else ""
        trend_lines.append(
            f"  {t['year_month']}: 収入{t['income']:,.0f}円 / 支出{t['expense']:,.0f}円 / 収支{sign}{net:,.0f}円"
        )

    # 固定費
    fixed_lines = [f"  {f['major']}/{f['minor']}: {f['avg_amount']:,.0f}円" for f in fixed.get("fixed", [])[:5]]

    # 高額支出
    top_lines = [
        f"  {t['date'][5:]}: {t['description']} {t['amount']:,.0f}円" for t in summary.get("top_expenses", [])[:5]
    ]

    data_text = f"""【家計簿データ ({year_month})】
■ 支出合計: {summary["total_expense"]:,.0f}円
■ 収入合計: {summary["total_income"]:,.0f}円
■ 収支: {summary["balance"]:+,.0f}円

■ カテゴリ別支出:
{chr(10).join(cat_lines) if cat_lines else "  データなし"}

■ 月別推移（直近6ヶ月）:
{chr(10).join(trend_lines) if trend_lines else "  データなし"}

■ 固定費（上位5件）:
{chr(10).join(fixed_lines) if fixed_lines else "  データなし"}
固定費率: {fixed.get("fixed_ratio", 0)}%

■ 高額支出（上位5件）:
{chr(10).join(top_lines) if top_lines else "  データなし"}"""

    return f"""{data_text}

あなたは家計アドバイザーです。上記の家計簿データを分析し、3〜4文で簡潔にコメントしてください。
支出の傾向、前月との比較、予算の消化状況、改善ポイントなどに触れてください。日本語で回答してください。"""


# ---------------------------------------------------------------------------
# Gemini API呼び出し
# ---------------------------------------------------------------------------


def _call_gemini(api_key: str, prompt: str) -> str:
    """Gemini APIを呼び出してテキストを取得する。"""
    from google import genai

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text


# ---------------------------------------------------------------------------
# メインエントリポイント
# ---------------------------------------------------------------------------


def generate_comments(db_path: str) -> None:
    """ダッシュボード・ライフプラン・家計簿のAIコメントを生成・保存する。

    同じ日付+ページのコメントが既存なら再生成しない。
    """
    api_key = _get_api_key(db_path)
    if not api_key:
        logger.info("[ai] APIキー未設定 — AI分析スキップ")
        return

    conn = init_db(db_path)
    try:
        row = conn.execute("SELECT date FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
        if not row:
            logger.info("[ai] スナップショットなし — AI分析スキップ")
            return
        date = row[0]

        # CF の最新 fiscal month を取得（締め日設定を反映）
        closing_day = int(get_setting(conn, "closing_day", "1") or "1")
        holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"
        cf_available = get_cf_available_months(conn, closing_day=closing_day, holiday_mode=holiday_mode)
        cf_with_data = [m for m in cf_available if m.get("has_data")]
        cf_ym = cf_with_data[0]["year_month"] if cf_with_data else None

        targets: list[tuple[str, str, object]] = [
            ("dashboard", date, lambda: _build_dashboard_prompt(db_path, date)),
            ("lifeplan", date, lambda: _build_lifeplan_prompt(db_path, date)),
        ]
        if cf_ym:
            targets.append(("cf", cf_ym, lambda: _build_cf_prompt(db_path, cf_ym)))

        for page, key, build_prompt in targets:
            existing = get_comment(conn, key, page)
            if existing:
                logger.info("[ai] %s コメント既存 (%s) — スキップ", page, key)
                continue

            prompt = build_prompt()
            if not prompt:
                logger.info("[ai] %s プロンプト生成失敗 — スキップ", page)
                continue

            try:
                comment = _call_gemini(api_key, prompt)
                save_comment(conn, key, page, comment)
                logger.info("[ai] %s コメント生成・保存完了 (%s)", page, key)
            except Exception as e:
                logger.error("[ai] %s コメント生成失敗: %s", page, e)
    finally:
        conn.close()
