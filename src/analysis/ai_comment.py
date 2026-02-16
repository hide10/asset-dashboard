"""Gemini APIを使ったAI分析コメント生成。

Gemini 2.5 Flash 無料枠（250リクエスト/日）を使用。
この機能は最大2回/日（dashboard + lifeplan）で十分に収まる。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from src.db.repository import get_cashflows, get_setting
from src.db.schema import init_db

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
    from src.data.stock_master import get_dividend, get_sector

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
    for h in holdings:
        name, code, asset_class, value, quantity = h
        if asset_class == "株式（現物）" and code:
            sector = get_sector(code)
            sector_totals[sector] = sector_totals.get(sector, 0) + value
            if quantity:
                dps = get_dividend(code)
                total_dividend += dps * quantity

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
        predictions, params = predict_no_contribution(db_path, risk_value, safe_value)
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
        predictions_c, _ = predict_with_contribution(db_path, risk_value, safe_value, monthly_contribution)
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
    """ダッシュボード・ライフプラン両方のAIコメントを生成・保存する。

    同じ日付+ページのコメントが既存なら再生成しない。
    """
    api_key = _get_api_key(db_path)
    if not api_key:
        print("[ai] APIキー未設定 — AI分析スキップ")
        return

    conn = init_db(db_path)
    row = conn.execute("SELECT date FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        conn.close()
        print("[ai] スナップショットなし — AI分析スキップ")
        return
    date = row[0]

    for page, build_prompt in [
        ("dashboard", lambda: _build_dashboard_prompt(db_path, date)),
        ("lifeplan", lambda: _build_lifeplan_prompt(db_path, date)),
    ]:
        existing = get_comment(conn, date, page)
        if existing:
            print(f"[ai] {page} コメント既存 ({date}) — スキップ")
            continue

        prompt = build_prompt()
        if not prompt:
            print(f"[ai] {page} プロンプト生成失敗 — スキップ")
            continue

        try:
            comment = _call_gemini(api_key, prompt)
            save_comment(conn, date, page, comment)
            print(f"[ai] {page} コメント生成・保存完了 ({date})")
        except Exception as e:
            print(f"[ai] {page} コメント生成失敗: {e}")

    conn.close()
