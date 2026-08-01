"""シンプルなWebダッシュボード。

標準ライブラリのみで動作する。
使い方: python -m src.web.server
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import html as html_mod
import json
import logging
import math
import os
import sqlite3
import threading
import time
import unicodedata
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from src.analysis.ai_comment import generate_comments, get_comment
from src.analysis.compare import ComparisonResult, get_all_comparisons
from src.analysis.metrics import concentration_top_n, daily_volatility, max_drawdown
from src.data.stock_master import get_dividend, get_sector, is_us_stock
from src.db.repository import (
    ALLOCATION_LABELS,
    ALLOCATION_PRESETS,
    build_education_events_for_child,
    calculate_allocation_scenario,
    calculate_investable_cash,
    create_child_profile,
    create_life_event,
    create_scheduled_card_payment,
    delete_child_profile,
    delete_life_event,
    disable_scheduled_card_payment,
    get_allocation_context,
    get_annual_life_event_expenses,
    get_budgets,
    get_cashflows,
    get_cf_actual_savings,
    get_cf_available_months,
    get_cf_category_details_history,
    get_cf_category_summary,
    get_cf_category_trend,
    get_cf_dividend_history,
    get_cf_fixed_expenses,
    get_cf_income_breakdown,
    get_cf_income_trend,
    get_cf_monthly_trend,
    get_daily_assets,
    get_fund_total_history,
    get_holding_history,
    get_latest_portfolio_snapshot,
    get_latest_stock_codes,
    get_life_plan_inflation_rate,
    get_portfolio_regional_exposure,
    get_regional_exposure_config,
    get_regional_exposure_holdings,
    get_setting,
    list_children_profiles,
    list_life_events,
    list_scheduled_card_payments,
    save_budgets,
    save_cf_csv_month,
    save_cf_transactions,
    save_life_plan_inflation_rate,
    save_regional_exposure_config,
    save_setting,
    update_child_profile,
    update_life_event,
)
from src.db.schema import get_connection, init_db
from src.prediction.montecarlo import (
    RISK_CLASSES,
    PredictionRange,
    SimulatorResult,
    classify_pension_holdings,
    predict_no_contribution,
    predict_with_contribution,
    run_lifecycle_simulation,
)

logger = logging.getLogger(__name__)

DB_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "assets.db"

_update_state = {"running": False, "version": 0}
_update_lock = threading.Lock()

_SCHEDULER_CHECK_INTERVAL = 60  # 秒
_SCHEDULER_DEFAULT_TIME = "07:00"


def _get_portfolio_context(db_path: str) -> dict | None:
    """別プロセスとの連携に必要な最小限のポートフォリオ情報を返す。"""
    conn = get_connection(db_path)
    try:
        context = get_latest_portfolio_snapshot(conn)
        if context is not None:
            regional = get_portfolio_regional_exposure(conn)
            investable = calculate_investable_cash(
                conn,
                as_of=datetime.strptime(context["as_of"], "%Y-%m-%d").date(),
                snapshot_date=context["as_of"],
            )
    finally:
        conn.close()
    if context is None:
        return None

    sector_totals: dict[str, float] = {}
    for holding in context["holdings"]:
        if holding["asset_class"] != "株式（現物）":
            continue
        sector = get_sector(holding["code"])
        sector_totals[sector] = sector_totals.get(sector, 0) + holding["value"]
    context["sector_totals"] = sector_totals
    context["regional_exposure"] = {
        "by_region": regional["by_region"],
        "configured_value": regional["configured_value"],
        "unconfigured_value": regional["unconfigured_value"],
    }
    context["investable_cash"] = investable["investable_cash"]
    context["investable_detail"] = investable
    return context


def _h(s: str) -> str:
    """HTML エスケープのショートカット。"""
    return html_mod.escape(str(s))


def _holding_history_key(asset_class: str, code: str, name: str) -> str:
    """保有銘柄履歴のキーを返す。"""
    parts = [
        unicodedata.normalize("NFKC", asset_class or "").strip(),
        unicodedata.normalize("NFKC", code or "").strip(),
        unicodedata.normalize("NFKC", name or "").strip(),
    ]
    return "|".join(parts)


def _screener_detail_url(base_url: str, code: str | None) -> str | None:
    """実行時設定されたスクリーナーURLへ、日本株コードだけを渡す。"""
    parsed = urlparse(base_url)
    value = str(code or "").strip()
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if len(value) == 4 and value.isdigit():
        value = f"{value}0"
    elif not (len(value) == 5 and value.isdigit()):
        return None
    return f"{base_url.rstrip('/')}/?stock={quote(value)}"


def _is_top_expense_excluded(item: dict) -> bool:
    """高額支出TOPから除外する項目を判定する。"""
    text = " ".join(
        [
            str(item.get("description", "")),
            str(item.get("major_category", "")),
            str(item.get("minor_category", "")),
            str(item.get("institution", "")),
        ]
    )
    normalized = unicodedata.normalize("NFKC", text).lower().replace(" ", "")

    # 積立・資産形成系（家計改善の観点ではノイズになりやすい）
    investment_like = [
        "積立",
        "つみたて",
        "投信",
        "投資信託",
        "nisa",
        "ideco",
        "個人年金",
        "変額",
        "終身",
        "学資",
        "サワカミ",
        "さわかみ",
        "明治安田",
        "明治安田生命",
        "メイジヤスダ",
        "メイジヤスダセイメイ",
        "meijiyasuda",
        "年金積立",
        "個人年金保険",
    ]
    if any(unicodedata.normalize("NFKC", k).lower().replace(" ", "") in normalized for k in investment_like):
        return True

    # マンション管理費・修繕積立等（固定の自動引落）
    condo_fixed_like = ["管理費", "カンリヒ", "修繕", "修繕積立", "修繕積立金"]
    return any(unicodedata.normalize("NFKC", k).lower().replace(" ", "") in normalized for k in condo_fixed_like)


# --- 共通 JS: 円グラフ描画・ツールチップ ---
_ESC_JS = """function esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }"""

_PIE_JS = """
const tooltip = document.getElementById('pie-tooltip');
function fmt(v) { return v.toLocaleString('ja-JP', {maximumFractionDigits:0}); }

function hitTest(e, canvas, cx, cy, r, chartData) {
  const rect = canvas.getBoundingClientRect();
  const mx = e.clientX - rect.left, my = e.clientY - rect.top;
  const dx = mx - cx, dy = my - cy;
  if (dx * dx + dy * dy > r * r) return null;
  let angle = Math.atan2(dy, dx);
  const offset = -Math.PI / 2;
  angle = angle - offset;
  if (angle < 0) angle += 2 * Math.PI;
  const total = chartData.reduce((s, d) => s + d.value, 0);
  let cumAngle = 0;
  for (const d of chartData) {
    const sl = (d.value / total) * 2 * Math.PI;
    if (angle >= cumAngle && angle < cumAngle + sl) {
      return { label: d.label, value: d.value, pct: (d.value / total * 100).toFixed(1), details: d.details || [] };
    }
    cumAngle += sl;
  }
  return null;
}

function attachTooltip(canvas, cx, cy, r, chartData) {
  canvas.addEventListener('mousemove', e => {
    const hit = hitTest(e, canvas, cx, cy, r, chartData);
    if (hit) {
      let html = '<strong>' + esc(hit.label) + '</strong>　' + fmt(hit.value) + ' 円（' + hit.pct + '%）';
      if (hit.details.length > 0) {
        html += '<div style="margin-top:5px;border-top:1px solid rgba(255,255,255,0.2);padding-top:5px">';
        const show = hit.details.slice(0, 8);
        show.forEach(item => {
          html += '<div style="display:flex;justify-content:space-between;gap:16px">'
            + '<span>' + esc(item.name) + '</span><span>' + fmt(item.value) + ' 円</span></div>';
        });
        if (hit.details.length > 8) html += '<div style="color:rgba(255,255,255,0.6)">…他 ' + (hit.details.length - 8) + ' 件</div>';
        html += '</div>';
      }
      tooltip.innerHTML = html;
      tooltip.classList.add('show');
      requestAnimationFrame(() => {
        const tw = tooltip.offsetWidth, th2 = tooltip.offsetHeight;
        tooltip.style.left = Math.min(e.clientX + 14, window.innerWidth - tw - 16) + 'px';
        tooltip.style.top = Math.min(e.clientY - 10, window.innerHeight - th2 - 16) + 'px';
      });
      canvas.style.cursor = 'pointer';
    } else {
      tooltip.classList.remove('show');
      canvas.style.cursor = '';
    }
  });
  canvas.addEventListener('mouseleave', () => {
    tooltip.classList.remove('show');
    canvas.style.cursor = '';
  });
}

function drawPieChart(canvasId, legendId, chartData, size) {
  const c = document.getElementById(canvasId);
  if (!c || chartData.length === 0) return;
  const x = c.getContext('2d');
  const w = size / 2, h = size / 2, rad = w - 10;
  let angle = -Math.PI / 2;
  const t = chartData.reduce((s, d) => s + d.value, 0);
  chartData.forEach(d => {
    const sl = (d.value / t) * 2 * Math.PI;
    x.beginPath(); x.moveTo(w, h); x.arc(w, h, rad, angle, angle + sl);
    x.closePath(); x.fillStyle = d.color; x.fill();
    angle += sl;
  });
  if (legendId) {
    const leg = document.getElementById(legendId);
    chartData.forEach(d => {
      const li = document.createElement('li');
      li.innerHTML = '<span class="dot" style="background:' + esc(d.color) + '"></span>' + esc(d.label);
      leg.appendChild(li);
    });
  }
  attachTooltip(c, w, h, rad, chartData);
}

// テーブル行ホバーツールチップ
document.querySelectorAll('.has-tip').forEach(row => {
  row.addEventListener('mousemove', e => {
    const details = JSON.parse(row.dataset.details || '[]');
    const label = row.dataset.label || '';
    if (details.length === 0) return;
    let html = '<strong>' + esc(label) + '</strong>';
    html += '<div style="margin-top:5px;border-top:1px solid rgba(255,255,255,0.2);padding-top:5px">';
    const show = details.slice(0, 8);
    show.forEach(item => {
      html += '<div style="display:flex;justify-content:space-between;gap:16px">'
        + '<span>' + esc(item.name) + '</span><span>' + fmt(item.value) + ' 円</span></div>';
    });
    if (details.length > 8) html += '<div style="color:rgba(255,255,255,0.6)">…他 ' + (details.length - 8) + ' 件</div>';
    html += '</div>';
    tooltip.innerHTML = html;
    tooltip.classList.add('show');
    requestAnimationFrame(() => {
      const tw = tooltip.offsetWidth, th2 = tooltip.offsetHeight;
      tooltip.style.left = Math.min(e.clientX + 14, window.innerWidth - tw - 16) + 'px';
      tooltip.style.top = Math.min(e.clientY - 10, window.innerHeight - th2 - 16) + 'px';
    });
  });
  row.addEventListener('mouseleave', () => {
    tooltip.classList.remove('show');
  });
});
"""

# --- 共通 CSS: ナビゲーションツールバー ---
_NAV_CSS = """
  .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
  .nav-toolbar { display: flex; gap: 0; }
  .nav-toolbar a {
    color: #636e72; text-decoration: none; font-size: 0.85rem; font-weight: 600;
    padding: 7px 16px; border: 1px solid #dfe6e9; background: #fff;
  }
  .nav-toolbar a:first-child { border-radius: 6px 0 0 6px; }
  .nav-toolbar a:last-child { border-radius: 0 6px 6px 0; }
  .nav-toolbar a:not(:first-child) { border-left: none; }
  .nav-toolbar a:hover { background: #f1f2f6; color: #2d3436; }
  .nav-toolbar a.active { background: #2881D7; color: #fff; border-color: #2881D7; }
  .nav-toolbar a.active + a { border-left-color: #2881D7; }
"""

_DEMO_BANNER = """<div style="background:#DF3727;color:#fff;text-align:center;padding:6px 0;font-size:0.8rem;font-weight:700;letter-spacing:0.1em">DEMO MODE — 表示データはすべてダミーです</div>"""


def _nav_html(active: str) -> str:
    """ナビゲーションツールバーのHTMLを返す。"""
    pages = [
        ("/", "ダッシュボード"),
        ("/allocation", "資産配分"),
        ("/cf", "家計簿分析"),
        ("/plan", "ライフプラン"),
        ("/simulator", "シミュレーター"),
        ("/settings", "設定"),
    ]
    links = []
    for path, label in pages:
        cls = ' class="active"' if path == active else ""
        links.append(f'<a href="{path}"{cls}>{label}</a>')
    return '<div class="nav-toolbar">' + "".join(links) + "</div>"


# --- 共通 CSS: 折りたたみ ---
_COLLAPSE_CSS = """
  .collapse-btn {
    background: none; border: none; cursor: pointer; font-size: 0.9rem;
    color: #b2bec3; padding: 2px 6px; line-height: 1; flex-shrink: 0;
  }
  .collapse-btn:hover { color: #636e72; }
  [data-card-id].collapsed > .card-body { display: none; }
  [data-card-id].collapsed { padding-bottom: 8px; }
"""

_RESPONSIVE_CSS = """
  @media (max-width: 768px) {
    .container { padding: 10px; }
    .grid { gap: 12px; }
    .card, .card.full { width: 100% !important; }
    .page-header { flex-direction: column; gap: 8px; align-items: flex-start; }
    .nav-toolbar a { padding: 6px 10px; font-size: 0.78rem; }
    .summary-cards { flex-direction: column; }
    .summary-card { min-width: auto; }
    .pie-wrap { flex-direction: column; align-items: center; }
    table { font-size: 0.8rem; }
    .card-body { overflow-x: auto; }
    .compare-cards { flex-direction: column; }
    .compare-card { min-width: auto; }
    h1 { font-size: 1.2rem; }
    canvas { max-width: 100%; height: auto !important; }
  }
"""

# --- 共通 JS: 折りたたみ ---
_COLLAPSE_JS = """
// 折りたたみ
(function() {
  const saved = JSON.parse(localStorage.getItem('collapsed_cards') || '{}');
  document.querySelectorAll('[data-card-id]').forEach(card => {
    const id = card.dataset.cardId;
    const defaultCollapsed = card.dataset.defaultCollapsed === 'true';
    const body = card.querySelector('.card-body');
    const btn = card.querySelector('.collapse-btn');
    if (!body || !btn) return;
    const isCollapsed = Object.prototype.hasOwnProperty.call(saved, id) ? !!saved[id] : defaultCollapsed;
    if (isCollapsed) {
      card.classList.add('collapsed');
      btn.textContent = '\\u25B6';
    } else {
      card.classList.remove('collapsed');
      btn.textContent = '\\u25BC';
    }
    btn.addEventListener('click', () => {
      const nextCollapsed = card.classList.toggle('collapsed');
      btn.textContent = nextCollapsed ? '\\u25B6' : '\\u25BC';
      const s = JSON.parse(localStorage.getItem('collapsed_cards') || '{}');
      s[id] = nextCollapsed;
      localStorage.setItem('collapsed_cards', JSON.stringify(s));
    });
  });
})();
"""


def _get_dates(db_path: str) -> list[str]:
    """利用可能な日付一覧を返す（新しい順）。"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT date FROM snapshots ORDER BY date DESC").fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _get_data(db_path: str, date: str | None = None) -> dict:
    conn = get_connection(db_path)
    try:
        if date is None:
            row = conn.execute("SELECT date FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
            if not row:
                return {}
            date = row[0]

        row = conn.execute("SELECT total_asset, by_class_json FROM snapshots WHERE date = ?", (date,)).fetchone()
        if not row:
            return {}

        total_asset = row[0]
        by_class = json.loads(row[1])

        accounts = [
            {"name": r[0], "asset_class": r[1], "balance": r[2], "institution": r[3]}
            for r in conn.execute(
                "SELECT account_name, asset_class, balance, institution FROM snapshot_accounts WHERE date = ? ORDER BY balance DESC",
                (date,),
            ).fetchall()
        ]

        holdings = [
            {
                "name": r[0],
                "code": r[1],
                "asset_class": r[2],
                "value": r[3],
                "quantity": r[4],
                "position": r[5],
                "acquisition_price": r[6],
                "current_price": r[7],
                "unrealized_gain": r[8],
                "unrealized_gain_pct": r[9],
            }
            for r in conn.execute(
                "SELECT name, symbol_or_code, asset_class, value, quantity, position, acquisition_price, current_price, unrealized_gain, unrealized_gain_pct FROM snapshot_holdings WHERE date = ? ORDER BY asset_class, value DESC",
                (date,),
            ).fetchall()
        ]

        fund_total_history = get_fund_total_history(conn)
        holding_histories = {}
        for h in holdings:
            history = get_holding_history(conn, h["asset_class"], h["name"], h["code"])
            if not history:
                continue
            key = _holding_history_key(h["asset_class"], h["code"], h["name"])
            latest = history[-1]
            holding_histories[key] = {
                "key": key,
                "name": h["name"],
                "code": h["code"],
                "asset_class": h["asset_class"],
                "history": history,
                "latest_value": latest["total_value"],
                "latest_cost": latest.get("total_cost"),
            }
        investable_cash = calculate_investable_cash(
            conn,
            as_of=datetime.strptime(date, "%Y-%m-%d").date(),
            snapshot_date=date,
        )
    finally:
        conn.close()

    # 業種別集計（株式のみ）
    sector_totals: dict[str, float] = {}
    for h in holdings:
        if h["asset_class"] == "株式（現物）" and h["code"]:
            sector = get_sector(h["code"])
            sector_totals[sector] = sector_totals.get(sector, 0) + h["value"]
    # 金額順ソート
    sector_totals = dict(sorted(sector_totals.items(), key=lambda x: x[1], reverse=True))

    # 配当予測（株式のみ）
    # get_dividend は取得できない銘柄に対して None を返す。集計には含めず、
    # 一覧の「配当/株」「年間配当」「利回り」列に「取得エラー」を表示する。
    usd_jpy = 150.0  # 米国株配当の円換算レート
    dividends: list[dict] = []
    total_dividend = 0.0
    dividend_error_count = 0
    for h in holdings:
        if h["asset_class"] == "株式（現物）" and h["code"] and h["quantity"]:
            dps = get_dividend(h["code"])
            if dps is None:
                dividend_error_count += 1
                dividends.append(
                    {
                        "code": h["code"],
                        "name": h["name"],
                        "quantity": h["quantity"],
                        "dps": None,
                        "annual": None,
                        "current_yield": None,
                        "acq_yield": None,
                        "error": True,
                    }
                )
                continue
            # 米国株の配当は USD → JPY に変換
            dps_jpy = dps * usd_jpy if is_us_stock(h["code"]) else dps
            annual = dps_jpy * h["quantity"]
            total_dividend += annual
            if dps > 0:
                cur_price = h.get("current_price")
                acq_price = h.get("acquisition_price")
                current_yield = (dps / cur_price * 100) if cur_price else None
                acq_yield = (dps / acq_price * 100) if acq_price else None
                dividends.append(
                    {
                        "code": h["code"],
                        "name": h["name"],
                        "quantity": h["quantity"],
                        "dps": dps_jpy,
                        "annual": annual,
                        "current_yield": current_yield,
                        "acq_yield": acq_yield,
                        "error": False,
                    }
                )
    # 取得エラー銘柄は末尾に並べる
    dividends.sort(key=lambda x: (x.get("error", False), -(x["annual"] or 0)))

    # 配当利回り別内訳（低配当0-2% / 中配当2-4% / 高配当4%超）
    yield_breakdown: dict[str, float] = {"低配当 (0-2%)": 0, "中配当 (2-4%)": 0, "高配当 (4%超)": 0}
    for d in dividends:
        if d.get("error"):
            continue
        cy = d.get("current_yield")
        if cy is None:
            continue
        # この銘柄の評価額を逆算: annual / (current_yield/100)
        stock_value = d["annual"] / (cy / 100) if cy > 0 else 0
        if cy < 2:
            yield_breakdown["低配当 (0-2%)"] += stock_value
        elif cy < 4:
            yield_breakdown["中配当 (2-4%)"] += stock_value
        else:
            yield_breakdown["高配当 (4%超)"] += stock_value

    # 業種別配当内訳（取得エラー銘柄は配当合算から除外）
    sector_dividends: dict[str, dict] = {}
    for h in holdings:
        if h["asset_class"] == "株式（現物）" and h["code"] and h["quantity"]:
            sector = get_sector(h["code"])
            dps = get_dividend(h["code"])
            if sector not in sector_dividends:
                sector_dividends[sector] = {"value": 0, "dividend": 0}
            sector_dividends[sector]["value"] += h["value"]
            if dps is not None:
                dps_jpy = dps * usd_jpy if is_us_stock(h["code"]) else dps
                sector_dividends[sector]["dividend"] += dps_jpy * h["quantity"]
    # 加重利回りを計算
    for sec_data in sector_dividends.values():
        sec_data["yield"] = (sec_data["dividend"] / sec_data["value"] * 100) if sec_data["value"] > 0 else 0

    # ボラティリティ指標
    try:
        vol = daily_volatility(db_path, days=30)
    except Exception:
        vol = None
    try:
        mdd = max_drawdown(db_path)
    except Exception:
        mdd = None
    try:
        conc = concentration_top_n(db_path, date, n=5)
    except Exception:
        conc = None

    # 比較データ
    comparisons = get_all_comparisons(db_path, date)

    return {
        "date": date,
        "total_asset": total_asset,
        "by_class": by_class,
        "accounts": accounts,
        "holdings": holdings,
        "sector_totals": sector_totals,
        "dividends": dividends,
        "total_dividend": total_dividend,
        "dividend_error_count": dividend_error_count,
        "yield_breakdown": yield_breakdown,
        "sector_dividends": sector_dividends,
        "volatility": vol,
        "max_drawdown": mdd,
        "concentration": conc,
        "comparisons": comparisons,
        "fund_total_history": fund_total_history,
        "holding_histories": holding_histories,
        "investable_cash": investable_cash,
    }


def _get_allocation_data(db_path: str, custom_allocation: dict[str, float] | None = None) -> dict:
    """投資可能額と配分シナリオを画面表示用に組み立てる。"""
    conn = get_connection(db_path)
    try:
        context = get_allocation_context(conn)
    finally:
        conn.close()
    if not context:
        return {}

    scenarios = [
        calculate_allocation_scenario(context, preset["allocation"], name=preset["name"])
        for preset in ALLOCATION_PRESETS
    ]
    custom = custom_allocation or dict(ALLOCATION_PRESETS[1]["allocation"])
    custom_scenario = calculate_allocation_scenario(context, custom, name="カスタム")
    return {
        "context": context,
        "presets": scenarios,
        "custom": custom_scenario,
    }


def _demo_allocation_data(custom_allocation: dict[str, float] | None = None) -> dict:
    """デモ資産から配分シナリオを組み立てる。"""
    demo = _demo_data()
    stock_holdings = [holding for holding in demo["holdings"] if holding["asset_class"] == "株式（現物）"]
    us_stock = sum(holding["value"] for holding in stock_holdings if holding["code"] and str(holding["code"]).isalpha())
    current_values = {
        "cash": float(demo["by_class"].get("預金・現金", 0)),
        "fund": float(demo["by_class"].get("投資信託", 0)),
        "jp_stock": float(demo["by_class"].get("株式（現物）", 0)) - us_stock,
        "us_stock": float(us_stock),
    }
    current_values["other"] = float(demo["total_asset"]) - sum(current_values.values())
    context = {
        "as_of": demo["date"],
        "total_asset": float(demo["total_asset"]),
        "current_values": current_values,
        "investable_cash": float(demo["investable_cash"]["investable_cash"]),
        "investable_detail": demo["investable_cash"],
    }
    scenarios = [
        calculate_allocation_scenario(context, preset["allocation"], name=preset["name"])
        for preset in ALLOCATION_PRESETS
    ]
    custom = custom_allocation or dict(ALLOCATION_PRESETS[1]["allocation"])
    return {
        "context": context,
        "presets": scenarios,
        "custom": calculate_allocation_scenario(context, custom, name="カスタム"),
    }


def _build_allocation_html(data: dict, error: str | None = None) -> str:
    """余剰資金の配分比較ページを生成する。"""
    if not data:
        return "<html><body><h1>資産データがありません</h1></body></html>"
    context = data["context"]
    scenarios = data["presets"]
    custom = data["custom"]
    labels = {**ALLOCATION_LABELS, "other": "その他資産"}

    scenario_cards = ""
    for scenario in scenarios:
        allocation_text = " / ".join(
            f"{ALLOCATION_LABELS[key]} {value:g}%" for key, value in scenario["allocation"].items()
        )
        amount_rows = "".join(
            f"<tr><td>{ALLOCATION_LABELS[key]}</td><td>{scenario['allocation'][key]:g}%</td>"
            f"<td>{scenario['allocation_amounts'][key]:,.0f}円</td></tr>"
            for key in ALLOCATION_LABELS
        )
        scenario_cards += f"""
        <section class="scenario-card">
          <h3>{_h(scenario["name"])}</h3>
          <p class="scenario-summary">{allocation_text}</p>
          <table><thead><tr><th>配分先</th><th>比率</th><th>金額</th></tr></thead><tbody>{amount_rows}</tbody></table>
        </section>"""

    custom_inputs = "".join(
        f'<label>{label}<input type="number" name="{key}" min="0" max="100" step="1" '
        f'value="{custom["allocation"][key]:g}" required><span>%</span></label>'
        for key, label in ALLOCATION_LABELS.items()
    )
    post_rows = "".join(
        f"<tr><td>{labels[key]}</td><td>{context['current_values'].get(key, 0):,.0f}円</td>"
        f"<td>{custom['post_values'][key]:,.0f}円</td><td>{custom['post_ratios'][key]:.1f}%</td></tr>"
        for key in ("cash", "fund", "jp_stock", "us_stock", "other")
    )
    error_html = f'<div class="error">{_h(error)}</div>' if error else ""
    investable = context["investable_cash"]
    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>資産配分シナリオ</title><style>
* {{ box-sizing:border-box }} body {{ margin:0;background:#f5f6fa;color:#2d3436;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.6 }}
.container {{ max-width:1100px;margin:auto;padding:20px }} {_NAV_CSS}
.hero,.card,.scenario-card {{ background:#fff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,.08) }}
.hero {{ margin:18px 0 }} .amount {{ color:#0F7F30;font-size:2rem;font-weight:800 }}
.scenarios {{ display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:14px 0 20px }}
.scenario-card h3 {{ margin:0 0 4px }} .scenario-summary {{ color:#636e72;font-size:.78rem;min-height:42px }}
table {{ width:100%;border-collapse:collapse;font-size:.86rem }} th,td {{ padding:7px;border-bottom:1px solid #eee;text-align:right }} th:first-child,td:first-child {{ text-align:left }}
.card {{ margin-bottom:20px }} .allocation-inputs {{ display:grid;grid-template-columns:repeat(4,1fr);gap:10px }}
.allocation-inputs label {{ font-size:.82rem;font-weight:600 }} .allocation-inputs input {{ width:calc(100% - 24px);padding:8px;border:1px solid #dfe6e9;border-radius:6px;margin-top:4px }}
.btn {{ display:inline-block;margin-top:14px;padding:9px 18px;border:0;border-radius:7px;background:#2881D7;color:#fff;font-weight:700;cursor:pointer;text-decoration:none }}
.btn.secondary {{ background:#636e72;margin-left:8px }} .error {{ background:#DF3727;color:#fff;padding:10px 14px;border-radius:8px;margin:12px 0 }}
@media(max-width:760px) {{ .scenarios {{ grid-template-columns:1fr }} .allocation-inputs {{ grid-template-columns:1fr 1fr }} .page-header {{ align-items:flex-start }} .nav-toolbar {{ flex-wrap:wrap }} }}
</style></head><body><div class="container">
<div class="page-header"><h1>余剰資金の配分を比較</h1>{_nav_html("/allocation")}</div>
<div class="hero"><div>現在の投資可能額</div><div class="amount">{investable:,.0f}円</div>
<p>生活防衛資金・予定支出・カード引き落とし予定を確保した後、この範囲で現金・投資信託・日本株・米国株を比較します。</p></div>
<div class="card" data-card-id="allocation-scenarios"><h2>プリセット比較</h2><div class="scenarios">{scenario_cards}</div></div>
<div class="card"><h2>自分で比率を調整</h2>{error_html}
<form method="GET" action="/allocation" data-testid="allocation-custom-form"><div class="allocation-inputs">{custom_inputs}</div>
<button class="btn" type="submit">この比率で比較</button><button class="btn secondary" type="button" onclick="copyAllocationPrompt(this)">AIに相談するデータをコピー</button></form></div>
<div class="card"><h2>購入後の構成</h2><table><thead><tr><th>区分</th><th>現在</th><th>購入後</th><th>購入後比率</th></tr></thead><tbody>{post_rows}</tbody></table></div>
</div><script>
async function copyAllocationPrompt(btn) {{ const r=await fetch('/api/ai-prompt?type=allocation'); const t=await r.text(); await navigator.clipboard.writeText(t); const old=btn.textContent; btn.textContent='コピーしました'; setTimeout(()=>btn.textContent=old,1500); }}
</script></body></html>"""


def _avg_yield_html(dividends: list[dict]) -> str:
    """配当加重平均利回りの HTML 片を返す。"""
    total_value = 0.0
    total_div = 0.0
    for d in dividends:
        if d.get("error"):
            continue
        if d.get("current_yield") is not None and d["annual"] and d["annual"] > 0:
            # 銘柄の評価額 = dps / (current_yield/100) * quantity
            # 簡易的に annual / (current_yield/100) で株式部分の評価額を逆算
            stock_value = d["annual"] / (d["current_yield"] / 100)
            total_value += stock_value
            total_div += d["annual"]
    if total_value > 0:
        avg_yield = total_div / total_value * 100
        return f"　加重平均利回り {avg_yield:.2f}%"
    return ""


def _fund_total_card_html(fund_total_history: list[dict]) -> str:
    """投資信託 評価額・取得価額推移カードの HTML を返す。"""
    if not fund_total_history:
        return ""

    latest = fund_total_history[-1]
    total_value = latest["total_value"]
    total_cost = latest.get("total_cost")

    # サマリー行
    value_str = f"¥{total_value:,.0f}"
    summary_html = f'<span style="font-size:1.5em;font-weight:bold">{value_str}</span>'

    if total_cost is not None and total_cost > 0:
        gain = total_value - total_cost
        gain_pct = gain / total_cost * 100
        gain_color = "#e17055" if gain >= 0 else "#0984e3"
        sign = "+" if gain >= 0 else ""
        summary_html += f"""
        <div style="margin-top:8px;display:flex;gap:24px;flex-wrap:wrap">
          <div><span style="color:#888">取得価額</span> ¥{total_cost:,.0f}</div>
          <div><span style="color:#888">評価損益</span>
            <span style="color:{gain_color};font-weight:bold">{sign}¥{gain:,.0f} ({gain_pct:.2f}%)</span>
          </div>
        </div>"""

    # 期間切り替えボタン
    range_btns = """<div style="text-align:right;margin-bottom:8px">
      <button class="ft-range-btn active" data-days="90" style="padding:2px 10px;margin:0 2px;border:1px solid #ddd;border-radius:4px;background:#fff;cursor:pointer">3ヶ月</button>
      <button class="ft-range-btn" data-days="180" style="padding:2px 10px;margin:0 2px;border:1px solid #ddd;border-radius:4px;background:#fff;cursor:pointer">6ヶ月</button>
      <button class="ft-range-btn" data-days="365" style="padding:2px 10px;margin:0 2px;border:1px solid #ddd;border-radius:4px;background:#fff;cursor:pointer">1年</button>
    </div>"""

    return f"""<div class="card full" data-card-id="dash-fund-total">
      <div class="card-header">
        <h2>投資信託 評価額・取得価額推移</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div style="margin-bottom:12px">{summary_html}</div>
        {range_btns}
        <canvas id="fund-total-chart" height="260"></canvas>
      </div>
    </div>"""


def _holding_detail_card_html(holding_histories: dict[str, dict]) -> str:
    """個別銘柄の評価額・取得価額推移カードの HTML を返す。"""
    if not holding_histories:
        return ""

    return """<div class="card full holding-detail-card" data-card-id="dash-holding-detail" id="holding-detail-card" style="display:none">
      <div class="card-header">
        <h2>個別銘柄 評価額・取得価額推移</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div class="holding-detail-head">
          <div>
            <div class="holding-detail-name" id="holding-detail-name">-</div>
            <div class="holding-detail-meta" id="holding-detail-meta"></div>
          </div>
          <div class="holding-detail-summary" id="holding-detail-summary"></div>
        </div>
        <div class="holding-detail-range">
          <button class="holding-range-btn active" data-days="90" type="button">3ヶ月</button>
          <button class="holding-range-btn" data-days="180" type="button">6ヶ月</button>
          <button class="holding-range-btn" data-days="365" type="button">1年</button>
        </div>
        <canvas id="holding-detail-chart" height="260"></canvas>
      </div>
    </div>"""


def _build_html(
    data: dict,
    dates: list[str],
    skip_update: bool = False,
    ai_comment: str | None = None,
    demo: bool = False,
    session_expired: str | None = None,
    last_fetch_at: str | None = None,
    next_run_at: str | None = None,
    screener_base_url: str = "",
) -> str:
    if not data:
        return "<html><body><h1>データがありません</h1></body></html>"

    fetch_parts = []
    if last_fetch_at:
        fetch_parts.append(f"最終取得: {last_fetch_at}")
    if next_run_at:
        fetch_parts.append(f"次回自動取得: {next_run_at}")
    fetch_status_html = (
        f'<div style="font-size:0.78rem;color:#b2bec3;margin-bottom:12px">{" ／ ".join(fetch_parts)}</div>'
        if fetch_parts
        else ""
    )

    date = data["date"]
    total = data["total_asset"]
    by_class = data["by_class"]
    accounts = data["accounts"]
    holdings = data["holdings"]
    sector_totals = data.get("sector_totals", {})
    dividends = data.get("dividends", [])
    total_dividend = data.get("total_dividend", 0)
    dividend_error_count = data.get("dividend_error_count", 0)
    yield_breakdown = data.get("yield_breakdown", {})
    sector_dividends = data.get("sector_dividends", {})
    vol = data.get("volatility")
    mdd = data.get("max_drawdown")
    conc = data.get("concentration")
    comparisons = data.get("comparisons", [])
    fund_total_history = data.get("fund_total_history", [])
    holding_histories = data.get("holding_histories", {})
    investable_cash = data.get("investable_cash")

    # 日付セレクタ
    date_options = ""
    for d in dates:
        sel = " selected" if d == date else ""
        date_options += f'<option value="{d}"{sel}>{d}</option>'

    # クラス別の内訳詳細を構築
    colors = ["#2881D7", "#DF3727", "#FCAD4C", "#0F7F30", "#008986", "#9C39B6"]
    class_details: dict[str, list] = {}
    for cls in by_class:
        details = []
        if cls == "預金・現金":
            for a in accounts:
                if a["asset_class"] == cls:
                    lbl = (
                        f"{a['institution']} / {a['name']}"
                        if a["institution"] and a["institution"] != a["name"]
                        else a["name"]
                    )
                    details.append({"name": lbl, "value": a["balance"]})
        else:
            for h in holdings:
                if h["asset_class"] == cls:
                    details.append({"name": h["name"], "value": h["value"]})
        details.sort(key=lambda x: x["value"], reverse=True)
        class_details[cls] = details

    # クラス別 rows（ホバー詳細付き）
    class_rows = ""
    for i, (cls, amt) in enumerate(by_class.items()):
        ratio = amt / total * 100 if total else 0
        color = colors[i % len(colors)]
        details_attr = _h(json.dumps(class_details[cls], ensure_ascii=False))
        class_rows += f"""
        <tr class="has-tip" data-details="{details_attr}" data-label="{_h(cls)}">
          <td><span class="dot" style="background:{color}"></span>{_h(cls)}</td>
          <td class="num">{amt:,.0f}円</td>
          <td class="num">{ratio:.1f}%</td>
          <td><div class="bar" style="width:{ratio * 2}px;background:{color}"></div></td>
        </tr>"""

    # 円グラフ用データ
    pie_data = json.dumps(
        [
            {"label": cls, "value": amt, "color": colors[i % len(colors)], "details": class_details[cls]}
            for i, (cls, amt) in enumerate(by_class.items())
        ],
        ensure_ascii=False,
    )

    # 口座 rows
    acc_rows = ""
    for a in accounts:
        label = f"{a['institution']} / {a['name']}" if a["institution"] and a["institution"] != a["name"] else a["name"]
        acc_rows += f'<tr><td>{label}</td><td class="num">{a["balance"]:,.0f}円</td></tr>'

    # 銘柄 rows (クラス別グループ + 前日比/前月比/前年比)
    # 比較データから差分ルックアップを構築: (asset_class, name, current_value) -> diff
    diff_lookups = []  # [daily_lookup, monthly_lookup, yearly_lookup]
    for comp in comparisons:
        lookup: dict[tuple, float] = {}
        if comp.total_diff is not None:
            for hd in comp.holding_diffs:
                key = (hd.get("asset_class", ""), hd["name"], hd["current"])
                lookup[key] = hd["diff"]
        diff_lookups.append(lookup)

    # 比較期間のヘッダーラベル (前日比は常に表示、残りは comparisons に応じて動的)
    comp_headers = "".join(f'<th class="num">{comp.label}</th>' for comp in comparisons)
    hold_col_count = 3 + len(comparisons)  # 銘柄 + 評価額 + 損益 + 比較列

    hold_rows = ""
    current_class = None
    for h in holdings:
        if h["asset_class"] != current_class:
            current_class = h["asset_class"]
            hold_rows += f'<tr class="group-header"><td colspan="{hold_col_count}">{current_class}</td></tr>'
        code = f'<span class="code">{h["code"]}</span> ' if h["code"] else ""
        qty = f' <span class="qty">x{h["quantity"]:,.0f}</span>' if h["quantity"] else ""
        # 評価損益セル
        ug = h.get("unrealized_gain")
        ugp = h.get("unrealized_gain_pct")
        if ug is not None and ug != 0:
            ug_sign = "+" if ug >= 0 else ""
            ug_css = "plus" if ug >= 0 else "minus"
            ugp_str = f" ({ugp:+.1f}%)" if ugp is not None else ""
            gain_cell = f'<td class="num {ug_css}">{ug_sign}{ug:,.0f}円{ugp_str}</td>'
        else:
            gain_cell = '<td class="num diff-zero">-</td>'
        # 各比較期間の差分セル
        diff_cells = ""
        diff_key = (h["asset_class"], h["name"], h["value"])
        for lookup in diff_lookups:
            d = lookup.get(diff_key)
            if d is not None and d != 0:
                sign = "+" if d >= 0 else ""
                css = "plus" if d >= 0 else "minus"
                diff_cells += f'<td class="num {css}">{sign}{d:,.0f}</td>'
            else:
                diff_cells += '<td class="num diff-zero">-</td>'
        history_key = _holding_history_key(h["asset_class"], h["code"], h["name"])
        if history_key in holding_histories:
            name_html = (
                f'<button type="button" class="holding-link" data-holding-key="{_h(history_key)}">'
                f"{code}{_h(h['name'])}{qty}</button>"
            )
        else:
            name_html = f"{code}{_h(h['name'])}{qty}"
        detail_url = _screener_detail_url(screener_base_url, h["code"])
        if h["asset_class"] == "株式（現物）" and detail_url:
            name_html += (
                f' <a class="screener-detail-link" href="{_h(detail_url)}" target="_blank" rel="noreferrer">財務</a>'
            )
        hold_rows += f'<tr><td>{name_html}</td><td class="num">{h["value"]:,.0f}円</td>{gain_cell}{diff_cells}</tr>'

    # 業種別円グラフ用データ
    sector_colors = [
        "#2881D7",
        "#DF3727",
        "#FCAD4C",
        "#0F7F30",
        "#008986",
        "#9C39B6",
        "#FF5266",
        "#80BD45",
        "#FF689A",
        "#1FBBDB",
        "#FD9441",
        "#6C5CE7",
        "#00B894",
    ]
    # 業種別円グラフ（銘柄詳細付き）
    sector_holdings: dict[str, list] = data.get("_sector_holdings", {})
    if not sector_holdings:
        for h in holdings:
            if h["asset_class"] == "株式（現物）" and h["code"]:
                sec = get_sector(h["code"])
                sector_holdings.setdefault(sec, []).append({"name": h["name"], "value": h["value"]})
    sector_pie_data = json.dumps(
        [
            {
                "label": sec,
                "value": amt,
                "color": sector_colors[i % len(sector_colors)],
                "details": sorted(sector_holdings.get(sec, []), key=lambda x: x["value"], reverse=True),
            }
            for i, (sec, amt) in enumerate(sector_totals.items())
        ],
        ensure_ascii=False,
    )

    stock_total = sum(sector_totals.values())
    sector_rows = ""
    for i, (sec, amt) in enumerate(sector_totals.items()):
        ratio = amt / stock_total * 100 if stock_total else 0
        color = sector_colors[i % len(sector_colors)]
        sd = sector_dividends.get(sec, {})
        sec_div = sd.get("dividend", 0)
        sec_yield = sd.get("yield", 0)
        sec_details = sorted(sector_holdings.get(sec, []), key=lambda x: x["value"], reverse=True)
        details_attr = _h(json.dumps(sec_details, ensure_ascii=False))
        sector_rows += f"""
        <tr class="has-tip" data-details="{details_attr}" data-label="{_h(sec)}">
          <td><span class="dot" style="background:{color}"></span>{_h(sec)}</td>
          <td class="num">{amt:,.0f}円</td>
          <td class="num">{ratio:.1f}%</td>
          <td class="num">{sec_div:,.0f}円</td>
          <td class="num">{sec_yield:.2f}%</td>
        </tr>"""

    # 配当予測 rows
    div_rows = ""
    for d in dividends:
        if d.get("error"):
            err = '<span class="div-error">取得エラー</span>'
            div_rows += f'<tr class="div-err-row"><td><span class="code">{_h(d["code"])}</span> {_h(d["name"])}</td>'
            div_rows += f'<td class="num">{d["quantity"]:,.0f}</td>'
            div_rows += f'<td class="num" colspan="4">{err}</td></tr>'
            continue
        cur_y = f"{d['current_yield']:.2f}%" if d.get("current_yield") is not None else "-"
        acq_y = f"{d['acq_yield']:.2f}%" if d.get("acq_yield") is not None else "-"
        div_rows += f'<tr><td><span class="code">{_h(d["code"])}</span> {_h(d["name"])}</td>'
        div_rows += f'<td class="num">{d["quantity"]:,.0f}</td>'
        div_rows += f'<td class="num">{d["dps"]:,.1f}円</td>'
        div_rows += f'<td class="num">{d["annual"]:,.0f}円</td>'
        div_rows += f'<td class="num">{cur_y}</td>'
        div_rows += f'<td class="num">{acq_y}</td></tr>'

    # 配当利回り別円グラフデータ（銘柄詳細付き）
    yield_colors = ["#80BD45", "#FCAD4C", "#DF3727"]
    yield_details: dict[str, list] = {"低配当 (0-2%)": [], "中配当 (2-4%)": [], "高配当 (4%超)": []}
    for d in dividends:
        cy = d.get("current_yield")
        if cy is None:
            continue
        stock_value = d["annual"] / (cy / 100) if cy > 0 else 0
        item = {"name": d["name"], "value": stock_value}
        if cy < 2:
            yield_details["低配当 (0-2%)"].append(item)
        elif cy < 4:
            yield_details["中配当 (2-4%)"].append(item)
        else:
            yield_details["高配当 (4%超)"].append(item)
    for v in yield_details.values():
        v.sort(key=lambda x: x["value"], reverse=True)
    yield_pie_data = json.dumps(
        [
            {"label": label, "value": amt, "color": yield_colors[i], "details": yield_details.get(label, [])}
            for i, (label, amt) in enumerate(yield_breakdown.items())
            if amt > 0
        ],
        ensure_ascii=False,
    )
    yield_total = sum(yield_breakdown.values())
    yield_breakdown_rows = ""
    for i, (label, amt) in enumerate(yield_breakdown.items()):
        if amt > 0:
            ratio = amt / yield_total * 100 if yield_total else 0
            color = yield_colors[i]
            yield_breakdown_rows += f"""
            <tr>
              <td><span class="dot" style="background:{color}"></span>{label}</td>
              <td class="num">{amt:,.0f}円</td>
              <td class="num">{ratio:.1f}%</td>
            </tr>"""

    # リスク指標カード HTML
    risk_cards_html = ""
    if vol is not None:
        risk_cards_html += f"""
    <div class="compare-card">
      <h3>ボラティリティ（年率）</h3>
      <div class="diff" style="color:#2d3436">{vol * 100:.1f}%</div>
      <div class="compare-date">直近30日</div>
    </div>"""
    else:
        risk_cards_html += """
    <div class="compare-card">
      <h3>ボラティリティ（年率）</h3>
      <div class="no-data">データ蓄積中</div>
    </div>"""

    if mdd is not None:
        risk_cards_html += f"""
    <div class="compare-card">
      <h3>最大ドローダウン</h3>
      <div class="diff minus">-{mdd:.1f}%</div>
      <div class="compare-date">全期間</div>
    </div>"""
    else:
        risk_cards_html += """
    <div class="compare-card">
      <h3>最大ドローダウン</h3>
      <div class="no-data">データ蓄積中</div>
    </div>"""

    if conc is not None and conc.get("concentration_pct", 0) > 0:
        risk_cards_html += f"""
    <div class="compare-card">
      <h3>上位5銘柄集中度</h3>
      <div class="diff" style="color:#2d3436">{conc["concentration_pct"]:.1f}%</div>
      <div class="compare-date">総資産に対する割合</div>
    </div>"""
    else:
        risk_cards_html += """
    <div class="compare-card">
      <h3>上位5銘柄集中度</h3>
      <div class="no-data">データ蓄積中</div>
    </div>"""

    # 比較カード HTML 生成
    compare_cards_html = ""
    for comp in comparisons:
        if comp.total_diff is not None:
            sign = "+" if comp.total_diff >= 0 else ""
            css = "plus" if comp.total_diff >= 0 else "minus"
            ratio_str = f"{sign}{comp.total_ratio:.2f}%" if comp.total_ratio is not None else ""
            # クラス別差分
            class_diff_html = ""
            if comp.by_class_diff:
                for cls_name, diff in sorted(comp.by_class_diff.items(), key=lambda x: abs(x[1]), reverse=True):
                    s = "+" if diff >= 0 else ""
                    c = "plus" if diff >= 0 else "minus"
                    class_diff_html += f'<div class="class-diff {c}">{cls_name} {s}{diff:,.0f}</div>'
            compare_cards_html += f"""
    <div class="compare-card">
      <h3>{comp.label}</h3>
      <div class="diff {css}">{sign}{comp.total_diff:,.0f}円</div>
      <div class="ratio {css}">{ratio_str}</div>
      <div class="compare-date">vs {comp.compare_date}</div>
      {class_diff_html}
    </div>"""
        else:
            compare_cards_html += f"""
    <div class="compare-card">
      <h3>{comp.label}</h3>
      <div class="no-data">データ不足</div>
    </div>"""

    investable_cash_html = ""
    if investable_cash:
        investable = float(investable_cash.get("investable_cash", 0))
        cash_balance = float(investable_cash.get("cash_balance", 0))
        emergency_fund = float(investable_cash.get("emergency_fund", 0))
        planned_expenses = float(investable_cash.get("planned_expenses", 0))
        scheduled_card_payment_total = float(investable_cash.get("scheduled_card_payment_total", 0))
        additional_reserve = float(investable_cash.get("additional_reserve", 0))
        shortfall = float(investable_cash.get("shortfall", 0))
        scheduled_card_payment_count = len(investable_cash.get("scheduled_card_payments", []))
        status_html = (
            f'<span style="color:#DF3727">必要額に {shortfall:,.0f}円不足</span>'
            if shortfall > 0
            else "投資先を比較できる上限額"
        )
        investable_cash_html = f"""
  <div class="card full" data-card-id="dash-investable-cash">
    <div class="card-header">
      <h2>投資可能額</h2>
      <button class="collapse-btn">&#x25BC;</button>
    </div>
    <div class="card-body">
      <div style="font-size:1.8rem;font-weight:700;color:#0F7F30">{investable:,.0f}円</div>
      <div style="font-size:0.82rem;color:#636e72;margin-bottom:12px">{status_html}</div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;font-size:0.85rem">
        <span>預金・現金 {cash_balance:,.0f}円</span>
        <span>− 生活防衛資金 {emergency_fund:,.0f}円</span>
        <span>− 予定支出 {planned_expenses:,.0f}円</span>
        {f"<span>− カード引落予定 {scheduled_card_payment_total:,.0f}円</span>" if scheduled_card_payment_total > 0 else ""}
        <span>− 追加確保 {additional_reserve:,.0f}円</span>
      </div>
      {f'<div style="margin-top:8px;font-size:0.78rem;color:#636e72">カード引落予定: {scheduled_card_payment_count}件（設定した計画期間内）</div>' if scheduled_card_payment_count else ""}
      <div style="margin-top:10px"><a href="/settings#investable-cash">計算条件を変更</a></div>
    </div>
  </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%232881D7'/><path d='M50 5A45 45 0 0 1 95 50L50 50Z' fill='%23FCAD4C'/><path d='M50 5A45 45 0 0 0 10.2 72.5L50 50Z' fill='%230F7F30'/></svg>">
<title>資産ダッシュボード - {date}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f6fa; color: #2d3436; line-height: 1.6; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
  .header {{ display: flex; align-items: baseline; gap: 16px; margin-bottom: 4px; }}
  h1 {{ font-size: 1.5rem; }}
  .date-picker {{ display: flex; align-items: center; gap: 8px; margin-bottom: 20px; }}
  .date-picker label {{ color: #636e72; font-size: 0.9rem; }}
  .date-picker select {{
    font-size: 0.9rem; padding: 4px 8px; border: 1px solid #dfe6e9;
    border-radius: 6px; background: #fff; cursor: pointer;
  }}
  .date-picker .nav-btn {{
    background: #fff; border: 1px solid #dfe6e9; border-radius: 6px;
    padding: 4px 10px; cursor: pointer; font-size: 0.9rem; color: #2d3436;
  }}
  .date-picker .nav-btn:hover {{ background: #f1f2f6; }}
  .date-picker .nav-btn:disabled {{ color: #b2bec3; cursor: default; background: #fff; }}
  .total {{ font-size: 1.4rem; font-weight: 700; color: #636e72; margin-bottom: 24px; }}
  .total strong {{ color: #2d3436; font-size: 1.8rem; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 20px; align-items: flex-start; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); width: calc(50% - 10px); }}
  .card h2 {{ font-size: 1.1rem; margin-bottom: 12px; color: #2d3436; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid #dfe6e9; color: #636e72; font-weight: 600; }}
  td {{ padding: 6px; border-bottom: 1px solid #f1f2f6; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .bar {{ height: 16px; border-radius: 3px; min-width: 2px; }}
  .group-header td {{ background: #f8f9fa; font-weight: 600; color: #636e72; padding: 10px 6px 6px; font-size: 0.85rem; }}
  .code {{ color: #636e72; font-size: 0.8rem; }}
  .qty {{ color: #636e72; font-size: 0.8rem; }}
  .full {{ width: 100%; }}
  .pie-wrap canvas {{ max-width: 280px; }}
  canvas {{ margin: 0 auto; display: block; }}
  .pie-wrap {{ display: flex; align-items: center; gap: 20px; position: relative; }}
  .pie-tooltip {{
    position: fixed; pointer-events: none; z-index: 9999;
    background: rgba(45,52,54,0.92); color: #fff; border-radius: 8px;
    padding: 8px 14px; font-size: 0.82rem; line-height: 1.5;
    white-space: nowrap; opacity: 0; transition: opacity 0.15s ease;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }}
  .pie-tooltip.show {{ opacity: 1; }}
  .has-tip {{ cursor: pointer; }}
  .has-tip:hover {{ background: #f0f4ff; }}
  .pie-legend {{ font-size: 0.85rem; }}
  .pie-legend li {{ list-style: none; margin-bottom: 4px; }}
  .dividend-total {{ font-size: 1.8rem; font-weight: 700; color: #0F7F30; margin-bottom: 2px; }}
  .dividend-total span {{ font-size: 0.9rem; color: #636e72; font-weight: 400; }}
  .dividend-monthly {{ font-size: 0.9rem; color: #636e72; margin-bottom: 8px; }}
  .dividend-warning {{ background: #fff3e0; border-left: 3px solid #FCAD4C; padding: 8px 12px; font-size: 0.85rem; color: #8d6e2c; border-radius: 4px; margin-bottom: 8px; }}
  .div-error {{ color: #999; font-style: italic; font-size: 0.85rem; }}
  .div-err-row td {{ background: #fafafa; }}
  .compare-cards {{ display: flex; gap: 12px; margin-bottom: 20px; }}
  .compare-card {{ flex: 1; background: #fff; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; }}
  .compare-card h3 {{ font-size: 0.85rem; color: #636e72; margin-bottom: 6px; font-weight: 600; }}
  .compare-card .diff {{ font-size: 1.3rem; font-weight: 700; }}
  .compare-card .ratio {{ font-size: 0.85rem; margin-top: 2px; }}
  .compare-card .compare-date {{ font-size: 0.75rem; color: #b2bec3; margin-top: 4px; }}
  .class-diff {{ font-size: 0.7rem; margin-top: 2px; }}
  .plus {{ color: #e74c3c; }}
  .minus {{ color: #2881D7; }}
  .diff-zero {{ color: #dfe6e9; }}
  .no-data {{ color: #b2bec3; font-size: 0.9rem; }}
  .info-btn {{
    width: 20px; height: 20px; border-radius: 50%; border: 1.5px solid #b2bec3;
    background: transparent; color: #636e72; font-size: 0.7rem; font-weight: 700;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
    flex-shrink: 0; vertical-align: middle; margin-left: 4px;
  }}
  .info-btn:hover {{ background: #f1f2f6; border-color: #636e72; }}
  .info-panel {{
    display: none; background: #f8f9fa; border-radius: 8px; padding: 12px 14px;
    font-size: 0.8rem; color: #636e72; line-height: 1.7; margin-bottom: 12px;
    border: 1px solid #dfe6e9;
  }}
  .info-panel.show {{ display: block; }}
  .info-panel strong {{ color: #2d3436; }}
  .info-panel ul {{ margin: 4px 0 4px 18px; }}
  .info-panel li {{ margin-bottom: 2px; }}
  .section-header {{ display: flex; align-items: center; gap: 6px; margin-bottom: 8px; }}
  .section-header h3 {{ font-size: 0.95rem; color: #636e72; font-weight: 600; margin: 0; }}
  .card-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
  .card-header h2 {{ margin-bottom: 0; }}
  .hold-table th {{ white-space: nowrap; }}
  .hold-table td:nth-child(n+3) {{ font-size: 0.82rem; }}
  .holding-link {{
    display: inline-flex; align-items: baseline; gap: 4px;
    border: 0; background: transparent; padding: 0; margin: 0;
    color: #1f5fbf; cursor: pointer; font: inherit; text-align: left;
  }}
  .holding-link:hover {{ color: #174a94; text-decoration: underline; }}
  .holding-link.active {{ color: #0f7f30; font-weight: 700; }}
  .screener-detail-link {{ margin-left:6px;font-size:.72rem;color:#0f7f30;text-decoration:none; }}
  .screener-detail-link:hover {{ text-decoration:underline; }}
  .holding-detail-card .card-body {{ overflow-x: auto; }}
  .holding-detail-head {{
    display: flex; justify-content: space-between; gap: 16px; margin-bottom: 12px; flex-wrap: wrap;
  }}
  .holding-detail-name {{ font-size: 1.15rem; font-weight: 700; color: #2d3436; }}
  .holding-detail-meta {{ font-size: 0.85rem; color: #636e72; margin-top: 2px; }}
  .holding-detail-summary {{ font-size: 0.95rem; color: #2d3436; }}
  .holding-detail-summary .label {{ color: #888; margin-right: 4px; }}
  .holding-detail-range {{ text-align: right; margin-bottom: 8px; }}
  .holding-range-btn {{
    padding: 2px 10px; margin: 0 2px; border: 1px solid #ddd; border-radius: 4px;
    background: #fff; cursor: pointer;
  }}
  .holding-range-btn.active {{ background: #edf4ff; border-color: #a9c7f7; color: #1f5fbf; }}
  {_NAV_CSS}
  .ai-comment-card {{
    background: linear-gradient(135deg, #f8f9ff 0%, #f0f4ff 100%);
    border: 1px solid #d4ddee; border-radius: 12px;
    padding: 16px 20px; margin-bottom: 20px;
    display: flex; align-items: flex-start; gap: 12px;
    font-size: 0.9rem; line-height: 1.7;
  }}
  .ai-icon {{
    background: #2881D7; color: #fff; font-size: 0.7rem; font-weight: 700;
    padding: 3px 6px; border-radius: 4px; flex-shrink: 0; margin-top: 2px;
  }}
  #reload-banner {{
    display: none; position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
    background: #0F7F30; color: #fff; padding: 10px 20px;
    align-items: center; justify-content: center; gap: 12px;
    font-size: 0.9rem; font-weight: 600;
  }}
  #reload-banner button {{
    background: #fff; color: #0F7F30; border: none; border-radius: 6px;
    padding: 4px 14px; font-size: 0.85rem; font-weight: 600; cursor: pointer;
  }}
  #reload-banner button:hover {{ background: #f1f2f6; }}
  {_COLLAPSE_CSS}
  {_RESPONSIVE_CSS}
</style>
</head>
<body>
<div id="reload-banner">
  データが更新されました
  <button onclick="location.reload()">再読み込み</button>
</div>
<div class="container">
  <div class="page-header">
    <h1>資産ダッシュボード</h1>
    {_nav_html("/")}
  </div>
  {
        '<div style="background:#DF3727;color:#fff;padding:10px 16px;border-radius:8px;margin-bottom:12px;font-size:0.85rem;font-weight:600">&#x26A0; セッション切れ — データの自動更新に失敗しました。<code style="background:rgba(255,255,255,0.2);padding:2px 6px;border-radius:4px;font-size:0.82rem">python -m src.scraper.login</code> で再ログインしてください。</div>'
        if session_expired
        else ""
    }
  <div class="date-picker">
    <button class="nav-btn" id="prev-btn" title="前の日">&larr;</button>
    <select id="date-select" onchange="location.href='/?date='+this.value">
      {date_options}
    </select>
    <button class="nav-btn" id="next-btn" title="次の日">&rarr;</button>
    <label>({len(dates)}日分のデータ)</label>
  </div>
  {fetch_status_html}
  <div class="total">現在の総資産: <strong>{total:,.0f}</strong> 円 <span style="font-size:0.85rem;color:#b2bec3">({
        date
    }時点)</span></div>
  {
        f'<div class="ai-comment-card"><div class="ai-icon">AI</div><div class="ai-text">{ai_comment}</div></div>'
        if ai_comment
        else ""
    }

  <div class="compare-cards">
    {compare_cards_html}
  </div>
  <div class="section-header">
    <h3>リスク指標</h3>
    <button class="info-btn" onclick="document.getElementById('risk-info').classList.toggle('show')" title="リスク指標について">?</button>
  </div>
  <div class="info-panel" id="risk-info">
    <ul>
      <li><strong>ボラティリティ（年率）</strong> — 資産額の日々の変動幅を年率に換算した数値です。数値が大きいほど資産の値動きが激しいことを意味します。一般的に 10% 以下なら安定的、20% を超えるとハイリスクとされます。</li>
      <li><strong>最大ドローダウン</strong> — 過去の最高値から最も大きく下落した割合です。「最悪の場合にどれだけ資産が減ったか」を示します。-5% なら最高値から5%下がった期間があったということです。</li>
      <li><strong>上位5銘柄集中度</strong> — 総資産のうち上位5つの保有銘柄が占める割合です。集中度が高い（例: 50%超）と特定銘柄の値動きに資産全体が左右されやすくなります。</li>
    </ul>
  </div>
  <div class="compare-cards" style="margin-bottom:20px">
    {risk_cards_html}
  </div>
  {investable_cash_html}
  <div class="grid">
    <div class="card" data-card-id="dash-class">
      <div class="card-header">
        <h2>資産クラス別内訳</h2>
        <button class="info-btn" onclick="document.getElementById('class-info').classList.toggle('show')" title="資産クラスについて">?</button>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="info-panel" id="class-info">
        <ul>
          <li><strong>資産クラス</strong> — 資産を性質ごとにグループ分けしたものです。値動きの異なるクラスに分散することでリスクを抑えられます。</li>
          <li><strong>株式（現物）</strong> — 個別企業の株式。値動きが大きい（ハイリスク・ハイリターン）。</li>
          <li><strong>投資信託</strong> — 複数の株式や債券をまとめた商品。個別株より分散が効いています。</li>
          <li><strong>預金・現金</strong> — 元本保証。インフレ時は実質目減りするリスクがあります。</li>
        </ul>
      </div>
      <div class="pie-wrap">
        <canvas id="pie" width="220" height="220"></canvas>
        <ul class="pie-legend" id="legend"></ul>
      </div>
      <table style="margin-top:16px">
        {class_rows}
      </table>
      </div>
    </div>

    <div class="card" data-card-id="dash-accounts">
      <div class="card-header">
        <h2>口座一覧 ({len(accounts)})</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <table>
        <tr><th>口座</th><th class="num">残高</th></tr>
        {acc_rows}
      </table>
      </div>
    </div>

    <div class="card" data-card-id="dash-sector">
      <div class="card-header">
        <h2>株式 業種別内訳</h2>
        <button class="info-btn" onclick="document.getElementById('sector-info').classList.toggle('show')" title="業種別内訳について">?</button>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="info-panel" id="sector-info">
        <ul>
          <li><strong>業種別内訳</strong> — 保有株式を東証の業種分類ごとに集計したものです。特定の業種に偏りすぎていないかを確認し、分散投資の参考にします。</li>
          <li><strong>利回り</strong> — 各業種の年間配当合計 &divide; 評価額。業種ごとの配当効率の比較に使えます。</li>
        </ul>
      </div>
      <div class="pie-wrap">
        <canvas id="sector-pie" width="220" height="220"></canvas>
        <ul class="pie-legend" id="sector-legend"></ul>
      </div>
      <table style="margin-top:16px">
        <tr><th>業種</th><th class="num">評価額</th><th class="num">比率</th><th class="num">年間配当</th><th class="num">利回り</th></tr>
        {sector_rows}
      </table>
      </div>
    </div>

    <div class="card" data-card-id="dash-dividend">
      <div class="card-header">
        <h2>年間配当予測</h2>
        <button class="info-btn" onclick="document.getElementById('div-info').classList.toggle('show')" title="配当予測について">?</button>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="info-panel" id="div-info">
        <ul>
          <li><strong>年間配当予測</strong> — 各銘柄の「予想1株配当 &times; 保有株数」を合計した金額です。実際の配当は業績により増減します。</li>
          <li><strong>加重平均利回り</strong> — 保有株式全体の配当利回りを評価額で加重平均した数値です。ポートフォリオ全体の配当効率を示します。</li>
          <li><strong>配当利回り</strong> — 1株配当 &divide; 現在の株価 &times; 100。株価に対して年間どれだけ配当がもらえるかの指標です。</li>
          <li><strong>取得利回り</strong> — 1株配当 &divide; 取得時の株価 &times; 100。自分の買値に対する配当の割合で、投資効率の評価に使います。</li>
          <li><strong>利回り別内訳</strong> — 低配当(0-2%)・中配当(2-4%)・高配当(4%超)の3区間で保有株式を分類したものです。</li>
        </ul>
      </div>
      <div class="dividend-total">{total_dividend:,.0f}<span> 円/年</span></div>
      <div class="dividend-monthly">月平均 {total_dividend / 12:,.0f}円{_avg_yield_html(dividends)}</div>
      {
        f'<div class="dividend-warning">⚠ {dividend_error_count} 銘柄の配当を取得できませんでした（一覧の「取得エラー」行を参照）。集計には含まれていません。</div>'
        if dividend_error_count > 0
        else ""
    }
      {
        f'''<div style="margin:16px 0;display:flex;align-items:center;gap:16px">
        <canvas id="yield-pie" width="140" height="140"></canvas>
        <table style="font-size:0.85rem;width:auto">
          {yield_breakdown_rows}
        </table>
      </div>'''
        if yield_total > 0
        else ""
    }
      <table style="margin-top:12px">
        <tr><th>銘柄</th><th class="num">保有数</th><th class="num">配当/株</th><th class="num">年間配当</th><th class="num">利回り</th><th class="num">取得利回り</th></tr>
        {div_rows}
      </table>
      </div>
    </div>

    <div class="card full" data-card-id="dash-holdings">
      <div class="card-header">
        <h2>保有銘柄 ({len(holdings)})</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <table class="hold-table">
        <tr><th>銘柄</th><th class="num">評価額</th><th class="num">損益</th>{comp_headers}</tr>
        {hold_rows}
      </table>
      </div>
    </div>

    {_holding_detail_card_html(holding_histories)}

    {_fund_total_card_html(fund_total_history)}
  </div>
</div>
<div class="pie-tooltip" id="pie-tooltip"></div>

<script>
{_ESC_JS}
{_PIE_JS}

const data = {pie_data};
drawPieChart('pie', 'legend', data, 220);

const sectorData = {sector_pie_data};
drawPieChart('sector-pie', 'sector-legend', sectorData, 220);

const yieldData = {yield_pie_data};
drawPieChart('yield-pie', null, yieldData, 140);

const holdingHistoryMap = {json.dumps(holding_histories, ensure_ascii=False)};
const holdingDetailCard = document.getElementById('holding-detail-card');
const holdingDetailName = document.getElementById('holding-detail-name');
const holdingDetailMeta = document.getElementById('holding-detail-meta');
const holdingDetailSummary = document.getElementById('holding-detail-summary');
const holdingDetailCanvas = document.getElementById('holding-detail-chart');
const holdingRangeBtns = document.querySelectorAll('.holding-range-btn');
const holdingLinks = document.querySelectorAll('.holding-link');
let selectedHoldingKey = holdingLinks[0]?.dataset.holdingKey || null;
let holdingRange = 90;

function formatHoldingSummary(record) {{
  const totalValue = record.latest_value || 0;
  const totalCost = record.latest_cost;
  let html = '<span class="label">評価額</span>¥' + totalValue.toLocaleString('ja-JP');
  if (totalCost != null && totalCost > 0) {{
    const gain = totalValue - totalCost;
    const gainPct = gain / totalCost * 100;
    const sign = gain >= 0 ? '+' : '';
    const css = gain >= 0 ? 'plus' : 'minus';
    html += ' <span class="label">取得価額</span>¥' + totalCost.toLocaleString('ja-JP');
    html += ' <span class="label">評価損益</span><span class="' + css + '">' + sign + '¥'
      + gain.toLocaleString('ja-JP') + ' (' + gainPct.toFixed(2) + '%)</span>';
  }}
  return html;
}}

function drawHoldingDetailChart() {{
  if (!selectedHoldingKey || !holdingHistoryMap[selectedHoldingKey] || !holdingDetailCanvas || !holdingDetailCard) return;
  const record = holdingHistoryMap[selectedHoldingKey];
  const allData = record.history || [];
  if (!allData.length) return;

  const now = new Date(allData[allData.length - 1].date);
  const cutoff = new Date(now);
  cutoff.setDate(cutoff.getDate() - holdingRange);
  const filtered = allData.filter(d => new Date(d.date) >= cutoff);
  if (!filtered.length) return;

  holdingDetailCard.style.display = 'block';
  holdingDetailName.textContent = record.name || '-';
  const metaParts = [];
  if (record.code) metaParts.push(record.code);
  if (record.asset_class) metaParts.push(record.asset_class);
  holdingDetailMeta.textContent = metaParts.join(' / ');
  holdingDetailSummary.innerHTML = formatHoldingSummary(record);

  const ctx = holdingDetailCanvas.getContext('2d');
  const W = holdingDetailCanvas.parentElement.clientWidth - 40;
  holdingDetailCanvas.width = W;
  holdingDetailCanvas.height = 260;
  ctx.clearRect(0, 0, W, 260);

  const pad = {{ left: 80, right: 20, top: 20, bottom: 30 }};
  const cW = W - pad.left - pad.right;
  const cH = 260 - pad.top - pad.bottom;

  const allVals = [];
  filtered.forEach(d => {{
    allVals.push(d.total_value || 0);
    if (d.total_cost != null) allVals.push(d.total_cost);
  }});
  const minVal = Math.min(...allVals) * 0.95;
  const maxVal = Math.max(...allVals) * 1.05;
  const range = maxVal - minVal || 1;
  const toY = v => pad.top + cH * (1 - (v - minVal) / range);
  const toX = i => pad.left + (cW / (filtered.length - 1 || 1)) * i;

  ctx.strokeStyle = '#f1f2f6';
  ctx.fillStyle = '#b2bec3';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {{
    const y = pad.top + cH * (1 - i / 4);
    const val = minVal + range * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
    ctx.fillText((val / 10000).toFixed(0) + '\\u4e07', pad.left - 6, y + 4);
  }}

  const hasCost = filtered.some(d => d.total_cost != null);
  if (hasCost) {{
    ctx.strokeStyle = '#b2bec3';
    ctx.lineWidth = 2;
    ctx.beginPath();
    let started = false;
    filtered.forEach((d, i) => {{
      if (d.total_cost == null) return;
      const x = toX(i), y = toY(d.total_cost);
      if (!started) {{ ctx.moveTo(x, y); started = true; }} else ctx.lineTo(x, y);
    }});
    ctx.stroke();
  }}

  ctx.strokeStyle = '#2881D7';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  filtered.forEach((d, i) => {{
    const x = toX(i), y = toY(d.total_value);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();

  const step = Math.max(1, Math.floor(filtered.length / 8));
  ctx.fillStyle = '#636e72';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  filtered.forEach((d, i) => {{
    if (i % step === 0 || i === filtered.length - 1) {{
      ctx.fillText(d.date.substring(5), toX(i), pad.top + cH + 18);
    }}
  }});

  let lx = pad.left;
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'left';
  [['#2881D7', '\\u8a55\\u4fa1\\u984d'], ['#b2bec3', '\\u53d6\\u5f97\\u4fa1\\u984d']].forEach(([color, label]) => {{
    ctx.fillStyle = color;
    ctx.fillRect(lx, 6, 14, 10);
    ctx.fillStyle = '#2d3436';
    ctx.fillText(label, lx + 17, 14);
    lx += ctx.measureText(label).width + 32;
  }});

  holdingDetailCanvas.onmousemove = function(e) {{
    const rect = holdingDetailCanvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const idx = Math.round((mx - pad.left) / (cW / (filtered.length - 1 || 1)));
    const tip = document.getElementById('pie-tooltip');
    if (!tip || idx < 0 || idx >= filtered.length) {{
      if (tip) tip.classList.remove('show');
      return;
    }}
    const d = filtered[idx];
    let html = '<strong>' + esc(d.date) + '</strong>';
    html += '<div style="margin-top:4px;border-top:1px solid rgba(255,255,255,0.2);padding-top:4px">';
    html += '<div style="display:flex;justify-content:space-between;gap:12px"><span style="color:#2881D7">\\u25cf \\u8a55\\u4fa1\\u984d</span><span>'
      + (d.total_value / 10000).toLocaleString('ja-JP', {{maximumFractionDigits:0}}) + '\\u4e07</span></div>';
    if (d.total_cost != null && d.total_cost > 0) {{
      html += '<div style="display:flex;justify-content:space-between;gap:12px"><span style="color:#b2bec3">\\u25cf \\u53d6\\u5f97\\u4fa1\\u984d</span><span>'
        + (d.total_cost / 10000).toLocaleString('ja-JP', {{maximumFractionDigits:0}}) + '\\u4e07</span></div>';
    }}
    html += '</div>';
    tip.innerHTML = html;
    tip.style.left = e.clientX + 14 + 'px';
    tip.style.top = e.clientY + 14 + 'px';
    tip.classList.add('show');
  }};
  holdingDetailCanvas.onmouseleave = function() {{
    const tip = document.getElementById('pie-tooltip');
    if (tip) tip.classList.remove('show');
  }};
}}

holdingRangeBtns.forEach(btn => btn.addEventListener('click', function() {{
  holdingRangeBtns.forEach(b => b.classList.remove('active'));
  this.classList.add('active');
  holdingRange = parseInt(this.dataset.days);
  drawHoldingDetailChart();
}}));

holdingLinks.forEach(btn => btn.addEventListener('click', function() {{
  holdingLinks.forEach(link => link.classList.remove('active'));
  this.classList.add('active');
  selectedHoldingKey = this.dataset.holdingKey;
  drawHoldingDetailChart();
  if (holdingDetailCard) {{
    holdingDetailCard.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
  }}
}}));

if (selectedHoldingKey && holdingHistoryMap[selectedHoldingKey]) {{
  const firstLink = document.querySelector('.holding-link[data-holding-key="' + CSS.escape(selectedHoldingKey) + '"]');
  if (firstLink) firstLink.classList.add('active');
  drawHoldingDetailChart();
}}

// 日付ナビゲーション
const sel = document.getElementById('date-select');
const dates = Array.from(sel.options).map(o => o.value);
const idx = sel.selectedIndex;
const prevBtn = document.getElementById('prev-btn');
const nextBtn = document.getElementById('next-btn');

// dates[0]が最新、dates[n-1]が最古。prevは古い方向、nextは新しい方向
nextBtn.disabled = idx === 0;
prevBtn.disabled = idx === dates.length - 1;

prevBtn.onclick = () => {{ if (idx < dates.length - 1) location.href = '/?date=' + dates[idx + 1]; }};
nextBtn.onclick = () => {{ if (idx > 0) location.href = '/?date=' + dates[idx - 1]; }};

{
        "// reload polling"
        + f'''
const loadedVersion = {_update_state["version"]};
const pollId = setInterval(async () => {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    if (s.version > loadedVersion) {{
      document.getElementById('reload-banner').style.display = 'flex';
      clearInterval(pollId);
    }}
  }} catch(e) {{}}
}}, 5000);
'''
        if not skip_update
        else ""
    }

// --- 投資信託 評価額・取得価額推移グラフ ---
(function() {{
  const ftData = {json.dumps(fund_total_history, ensure_ascii=False)};
  const ftCanvas = document.getElementById('fund-total-chart');
  if (!ftData.length || !ftCanvas) return;

  let ftRange = 90;  // デフォルト3ヶ月
  const ftBtns = document.querySelectorAll('.ft-range-btn');
  ftBtns.forEach(btn => btn.addEventListener('click', function() {{
    ftBtns.forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    ftRange = parseInt(this.dataset.days);
    drawFundTotalChart();
  }}));

  function drawFundTotalChart() {{
    // 期間フィルタ
    const now = new Date(ftData[ftData.length - 1].date);
    const cutoff = new Date(now);
    cutoff.setDate(cutoff.getDate() - ftRange);
    const filtered = ftData.filter(d => new Date(d.date) >= cutoff);
    if (!filtered.length) return;

    const ctx = ftCanvas.getContext('2d');
    const W = ftCanvas.parentElement.clientWidth - 40;
    ftCanvas.width = W;
    ftCanvas.height = 260;
    ctx.clearRect(0, 0, W, 260);

    const pad = {{ left: 80, right: 20, top: 20, bottom: 30 }};
    const cW = W - pad.left - pad.right;
    const cH = 260 - pad.top - pad.bottom;

    // Y軸レンジ
    let allVals = [];
    filtered.forEach(d => {{
      allVals.push(d.total_value || 0);
      if (d.total_cost != null) allVals.push(d.total_cost);
    }});
    const minVal = Math.min(...allVals) * 0.95;
    const maxVal = Math.max(...allVals) * 1.05;
    const range = maxVal - minVal || 1;

    const toY = v => pad.top + cH * (1 - (v - minVal) / range);
    const toX = i => pad.left + (cW / (filtered.length - 1 || 1)) * i;

    // Y軸グリッド
    ctx.strokeStyle = '#f1f2f6';
    ctx.fillStyle = '#b2bec3';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'right';
    for (let i = 0; i <= 4; i++) {{
      const y = pad.top + cH * (1 - i / 4);
      const val = minVal + range * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(W - pad.right, y); ctx.stroke();
      ctx.fillText((val / 10000).toFixed(0) + '\u4e07', pad.left - 6, y + 4);
    }}

    // 取得価額ライン（グレー）
    const hasCost = filtered.some(d => d.total_cost != null);
    if (hasCost) {{
      ctx.strokeStyle = '#b2bec3';
      ctx.lineWidth = 2;
      ctx.beginPath();
      let started = false;
      filtered.forEach((d, i) => {{
        if (d.total_cost == null) return;
        const x = toX(i), y = toY(d.total_cost);
        if (!started) {{ ctx.moveTo(x, y); started = true; }} else ctx.lineTo(x, y);
      }});
      ctx.stroke();
    }}

    // 評価額ライン（オレンジ）
    ctx.strokeStyle = '#E67E22';
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    filtered.forEach((d, i) => {{
      const x = toX(i), y = toY(d.total_value);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }});
    ctx.stroke();

    // X軸ラベル
    const step = Math.max(1, Math.floor(filtered.length / 8));
    ctx.fillStyle = '#636e72';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    filtered.forEach((d, i) => {{
      if (i % step === 0 || i === filtered.length - 1) {{
        ctx.fillText(d.date.substring(5), toX(i), pad.top + cH + 18);
      }}
    }});

    // 凡例
    let lx = pad.left;
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'left';
    [['#E67E22', '\u8a55\u4fa1\u984d'], ['#b2bec3', '\u53d6\u5f97\u4fa1\u984d']].forEach(([color, label]) => {{
      ctx.fillStyle = color;
      ctx.fillRect(lx, 6, 14, 10);
      ctx.fillStyle = '#2d3436';
      ctx.fillText(label, lx + 17, 14);
      lx += ctx.measureText(label).width + 32;
    }});

    // ツールチップ
    ftCanvas.onmousemove = function(e) {{
      const rect = ftCanvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const idx = Math.round((mx - pad.left) / (cW / (filtered.length - 1 || 1)));
      if (idx < 0 || idx >= filtered.length) {{ const tip = document.getElementById('pie-tooltip'); if (tip) tip.classList.remove('show'); return; }}
      const d = filtered[idx];
      let html = '<strong>' + esc(d.date) + '</strong>';
      html += '<div style="margin-top:4px;border-top:1px solid rgba(255,255,255,0.2);padding-top:4px">';
      html += '<div style="display:flex;justify-content:space-between;gap:12px"><span style="color:#E67E22">\u25cf \u8a55\u4fa1\u984d</span><span>' + (d.total_value/10000).toLocaleString('ja-JP',{{maximumFractionDigits:0}}) + '\u4e07</span></div>';
      if (d.total_cost != null && d.total_cost > 0) {{
        html += '<div style="display:flex;justify-content:space-between;gap:12px"><span style="color:#b2bec3">\u25cf \u53d6\u5f97\u4fa1\u984d</span><span>' + (d.total_cost/10000).toLocaleString('ja-JP',{{maximumFractionDigits:0}}) + '\u4e07</span></div>';
        const gain = d.total_value - d.total_cost;
        const pct = (gain / d.total_cost * 100).toFixed(2);
        const clr = gain >= 0 ? '#e17055' : '#0984e3';
        html += '<div style="display:flex;justify-content:space-between;gap:12px"><span>\u640d\u76ca</span><span style="color:' + clr + '">' + (gain >= 0 ? '+' : '') + (gain/10000).toLocaleString('ja-JP',{{maximumFractionDigits:0}}) + '\u4e07 (' + pct + '%)</span></div>';
      }}
      html += '</div>';
      const tip = document.getElementById('pie-tooltip');
      if (tip) {{
        tip.innerHTML = html;
        tip.classList.add('show');
        tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 260) + 'px';
        tip.style.top = (e.clientY - 10) + 'px';
      }}
    }};
    ftCanvas.onmouseleave = function() {{
      const tip = document.getElementById('pie-tooltip');
      if (tip) tip.classList.remove('show');
    }};
  }}

  drawFundTotalChart();
  window.addEventListener('resize', drawFundTotalChart);
}})();

{_COLLAPSE_JS}
</script>
</body>
</html>"""


def _demo_data() -> dict:
    """SNS共有用のダミーデータを生成する。"""
    from datetime import date, timedelta

    today = date.today().isoformat()

    by_class = {
        "預金・現金": 4_820_000,
        "株式（現物）": 6_350_000,
        "投資信託": 5_180_000,
        "不動産": 1_200_000,
        "年金": 3_950_000,
    }

    accounts = [
        {"name": "普通預金", "asset_class": "預金・現金", "balance": 2_150_000, "institution": "みずほ銀行"},
        {
            "name": "普通預金",
            "asset_class": "預金・現金",
            "balance": 1_380_000,
            "institution": "三井住友銀行",
        },
        {
            "name": "定期預金",
            "asset_class": "預金・現金",
            "balance": 1_000_000,
            "institution": "住信SBIネット銀行",
        },
        {"name": "円預金", "asset_class": "預金・現金", "balance": 245_000, "institution": "楽天銀行"},
        {"name": "Suica", "asset_class": "預金・現金", "balance": 3_200, "institution": "モバイルSuica"},
        {"name": "預り金", "asset_class": "預金・現金", "balance": 41_800, "institution": "SBI証券"},
    ]

    holdings = [
        {
            "name": "トヨタ自動車",
            "code": "7203",
            "asset_class": "株式（現物）",
            "value": 1_260_000,
            "quantity": 300,
            "acquisition_price": 3_800,
            "current_price": 4_200,
            "unrealized_gain": 120_000,
            "unrealized_gain_pct": 10.5,
        },
        {
            "name": "ソニーグループ",
            "code": "6758",
            "asset_class": "株式（現物）",
            "value": 980_000,
            "quantity": 100,
            "acquisition_price": 8_500,
            "current_price": 9_800,
            "unrealized_gain": 130_000,
            "unrealized_gain_pct": 15.3,
        },
        {
            "name": "三菱商事",
            "code": "8058",
            "asset_class": "株式（現物）",
            "value": 875_000,
            "quantity": 100,
            "acquisition_price": 7_200,
            "current_price": 8_750,
            "unrealized_gain": 155_000,
            "unrealized_gain_pct": 21.5,
        },
        {
            "name": "信越化学工業",
            "code": "4063",
            "asset_class": "株式（現物）",
            "value": 720_000,
            "quantity": 100,
            "acquisition_price": 6_500,
            "current_price": 7_200,
            "unrealized_gain": 70_000,
            "unrealized_gain_pct": 10.8,
        },
        {
            "name": "日立製作所",
            "code": "6501",
            "asset_class": "株式（現物）",
            "value": 685_000,
            "quantity": 200,
            "acquisition_price": 2_800,
            "current_price": 3_425,
            "unrealized_gain": 125_000,
            "unrealized_gain_pct": 22.3,
        },
        {
            "name": "キーエンス",
            "code": "6861",
            "asset_class": "株式（現物）",
            "value": 650_000,
            "quantity": 10,
            "acquisition_price": 58_000,
            "current_price": 65_000,
            "unrealized_gain": 70_000,
            "unrealized_gain_pct": 12.1,
        },
        {
            "name": "任天堂",
            "code": "7974",
            "asset_class": "株式（現物）",
            "value": 580_000,
            "quantity": 100,
            "acquisition_price": 5_200,
            "current_price": 5_800,
            "unrealized_gain": 60_000,
            "unrealized_gain_pct": 11.5,
        },
        {
            "name": "ダイキン工業",
            "code": "6367",
            "asset_class": "株式（現物）",
            "value": 350_000,
            "quantity": 100,
            "acquisition_price": 3_000,
            "current_price": 3_500,
            "unrealized_gain": 50_000,
            "unrealized_gain_pct": 16.7,
        },
        {
            "name": "INPEX",
            "code": "1605",
            "asset_class": "株式（現物）",
            "value": 250_000,
            "quantity": 500,
            "acquisition_price": 420,
            "current_price": 500,
            "unrealized_gain": 40_000,
            "unrealized_gain_pct": 19.0,
        },
        {
            "name": "eMAXIS Slim 全世界株式(オルカン)",
            "code": "",
            "asset_class": "投資信託",
            "value": 2_480_000,
            "quantity": 680000,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": 480_000,
            "unrealized_gain_pct": 24.0,
        },
        {
            "name": "eMAXIS Slim 米国株式(S&P500)",
            "code": "",
            "asset_class": "投資信託",
            "value": 1_850_000,
            "quantity": 520000,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": 350_000,
            "unrealized_gain_pct": 23.3,
        },
        {
            "name": "ニッセイ外国株式インデックスファンド",
            "code": "",
            "asset_class": "投資信託",
            "value": 850_000,
            "quantity": 290000,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": 80_000,
            "unrealized_gain_pct": 10.4,
        },
        {
            "name": "不動産クラウドファンディング",
            "code": "",
            "asset_class": "不動産",
            "value": 1_200_000,
            "quantity": None,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": None,
            "unrealized_gain_pct": None,
        },
        {
            "name": "企業型確定拠出年金",
            "code": "",
            "asset_class": "年金",
            "value": 2_800_000,
            "quantity": None,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": None,
            "unrealized_gain_pct": None,
        },
        {
            "name": "iDeCo（先進国株式）",
            "code": "",
            "asset_class": "年金",
            "value": 850_000,
            "quantity": None,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": None,
            "unrealized_gain_pct": None,
        },
        {
            "name": "個人年金保険",
            "code": "",
            "asset_class": "年金",
            "value": 300_000,
            "quantity": None,
            "acquisition_price": None,
            "current_price": None,
            "unrealized_gain": None,
            "unrealized_gain_pct": None,
        },
    ]

    # 業種別
    demo_sectors = {
        "輸送用機器": 1_260_000,
        "電気機器": 2_315_000,
        "卸売業": 875_000,
        "化学": 720_000,
        "その他製品": 580_000,
        "機械": 350_000,
        "鉱業": 250_000,
    }
    demo_sectors = dict(sorted(demo_sectors.items(), key=lambda x: x[1], reverse=True))

    # 配当予測
    demo_dividends = [
        {
            "code": "7203",
            "name": "トヨタ自動車",
            "quantity": 300,
            "dps": 75,
            "annual": 22_500,
            "current_yield": 75 / 4200 * 100,
            "acq_yield": 75 / 3800 * 100,
        },
        {
            "code": "6758",
            "name": "ソニーグループ",
            "quantity": 100,
            "dps": 85,
            "annual": 8_500,
            "current_yield": 85 / 9800 * 100,
            "acq_yield": 85 / 8500 * 100,
        },
        {
            "code": "8058",
            "name": "三菱商事",
            "quantity": 100,
            "dps": 100,
            "annual": 10_000,
            "current_yield": 100 / 8750 * 100,
            "acq_yield": 100 / 7200 * 100,
        },
        {
            "code": "4063",
            "name": "信越化学工業",
            "quantity": 100,
            "dps": 120,
            "annual": 12_000,
            "current_yield": 120 / 7200 * 100,
            "acq_yield": 120 / 6500 * 100,
        },
        {
            "code": "6501",
            "name": "日立製作所",
            "quantity": 200,
            "dps": 52,
            "annual": 10_400,
            "current_yield": 52 / 3425 * 100,
            "acq_yield": 52 / 2800 * 100,
        },
        {
            "code": "6861",
            "name": "キーエンス",
            "quantity": 10,
            "dps": 300,
            "annual": 3_000,
            "current_yield": 300 / 65000 * 100,
            "acq_yield": 300 / 58000 * 100,
        },
        {
            "code": "7974",
            "name": "任天堂",
            "quantity": 100,
            "dps": 183,
            "annual": 18_300,
            "current_yield": 183 / 5800 * 100,
            "acq_yield": 183 / 5200 * 100,
        },
        {
            "code": "6367",
            "name": "ダイキン工業",
            "quantity": 100,
            "dps": 100,
            "annual": 10_000,
            "current_yield": 100 / 3500 * 100,
            "acq_yield": 100 / 3000 * 100,
        },
        {
            "code": "1605",
            "name": "INPEX",
            "quantity": 500,
            "dps": 60,
            "annual": 30_000,
            "current_yield": 60 / 500 * 100,
            "acq_yield": 60 / 420 * 100,
        },
    ]
    demo_dividends.sort(key=lambda x: x["annual"], reverse=True)

    total_asset = sum(by_class.values())

    # 比較デモデータ
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_month = (date.today() - timedelta(days=30)).isoformat()
    last_year = (date.today() - timedelta(days=365)).isoformat()

    # holding_diffs は (asset_class, name, current_value) でルックアップされる
    daily_hdiffs = [
        {"name": "トヨタ自動車", "asset_class": "株式（現物）", "current": 1_260_000, "diff": 18_000},
        {"name": "ソニーグループ", "asset_class": "株式（現物）", "current": 980_000, "diff": 12_500},
        {"name": "三菱商事", "asset_class": "株式（現物）", "current": 875_000, "diff": 8_200},
        {"name": "信越化学工業", "asset_class": "株式（現物）", "current": 720_000, "diff": -5_400},
        {"name": "日立製作所", "asset_class": "株式（現物）", "current": 685_000, "diff": 9_800},
        {"name": "キーエンス", "asset_class": "株式（現物）", "current": 650_000, "diff": -3_200},
        {"name": "任天堂", "asset_class": "株式（現物）", "current": 580_000, "diff": 4_100},
        {"name": "ダイキン工業", "asset_class": "株式（現物）", "current": 350_000, "diff": -2_500},
        {"name": "INPEX", "asset_class": "株式（現物）", "current": 250_000, "diff": -5_700},
        {"name": "eMAXIS Slim 全世界株式(オルカン)", "asset_class": "投資信託", "current": 2_480_000, "diff": 8_300},
        {"name": "eMAXIS Slim 米国株式(S&P500)", "asset_class": "投資信託", "current": 1_850_000, "diff": 5_200},
        {"name": "ニッセイ外国株式インデックスファンド", "asset_class": "投資信託", "current": 850_000, "diff": -1_000},
    ]
    monthly_hdiffs = [
        {"name": "トヨタ自動車", "asset_class": "株式（現物）", "current": 1_260_000, "diff": 72_000},
        {"name": "ソニーグループ", "asset_class": "株式（現物）", "current": 980_000, "diff": 45_000},
        {"name": "三菱商事", "asset_class": "株式（現物）", "current": 875_000, "diff": 32_000},
        {"name": "信越化学工業", "asset_class": "株式（現物）", "current": 720_000, "diff": -18_000},
        {"name": "日立製作所", "asset_class": "株式（現物）", "current": 685_000, "diff": 28_000},
        {"name": "キーエンス", "asset_class": "株式（現物）", "current": 650_000, "diff": 15_000},
        {"name": "任天堂", "asset_class": "株式（現物）", "current": 580_000, "diff": 22_000},
        {"name": "ダイキン工業", "asset_class": "株式（現物）", "current": 350_000, "diff": -8_000},
        {"name": "INPEX", "asset_class": "株式（現物）", "current": 250_000, "diff": -12_000},
        {"name": "eMAXIS Slim 全世界株式(オルカン)", "asset_class": "投資信託", "current": 2_480_000, "diff": 52_000},
        {"name": "eMAXIS Slim 米国株式(S&P500)", "asset_class": "投資信託", "current": 1_850_000, "diff": 38_000},
        {"name": "ニッセイ外国株式インデックスファンド", "asset_class": "投資信託", "current": 850_000, "diff": 5_000},
        {"name": "企業型確定拠出年金", "asset_class": "年金", "current": 2_800_000, "diff": 18_000},
        {"name": "iDeCo（先進国株式）", "asset_class": "年金", "current": 850_000, "diff": 7_000},
    ]
    yearly_hdiffs = [
        {"name": "トヨタ自動車", "asset_class": "株式（現物）", "current": 1_260_000, "diff": 320_000},
        {"name": "ソニーグループ", "asset_class": "株式（現物）", "current": 980_000, "diff": 215_000},
        {"name": "三菱商事", "asset_class": "株式（現物）", "current": 875_000, "diff": 195_000},
        {"name": "信越化学工業", "asset_class": "株式（現物）", "current": 720_000, "diff": 140_000},
        {"name": "日立製作所", "asset_class": "株式（現物）", "current": 685_000, "diff": 285_000},
        {"name": "キーエンス", "asset_class": "株式（現物）", "current": 650_000, "diff": 180_000},
        {"name": "任天堂", "asset_class": "株式（現物）", "current": 580_000, "diff": 125_000},
        {"name": "ダイキン工業", "asset_class": "株式（現物）", "current": 350_000, "diff": 60_000},
        {"name": "INPEX", "asset_class": "株式（現物）", "current": 250_000, "diff": 130_000},
        {"name": "eMAXIS Slim 全世界株式(オルカン)", "asset_class": "投資信託", "current": 2_480_000, "diff": 680_000},
        {"name": "eMAXIS Slim 米国株式(S&P500)", "asset_class": "投資信託", "current": 1_850_000, "diff": 520_000},
        {"name": "ニッセイ外国株式インデックスファンド", "asset_class": "投資信託", "current": 850_000, "diff": 80_000},
        {"name": "企業型確定拠出年金", "asset_class": "年金", "current": 2_800_000, "diff": 420_000},
        {"name": "iDeCo（先進国株式）", "asset_class": "年金", "current": 850_000, "diff": 160_000},
    ]

    demo_comparisons = [
        ComparisonResult(
            label="前日比",
            target_date=today,
            compare_date=yesterday,
            total_diff=42_300,
            total_ratio=0.20,
            by_class_diff={"株式（現物）": 35_800, "投資信託": 12_500, "預金・現金": -6_000},
            account_diffs=[],
            holding_diffs=daily_hdiffs,
        ),
        ComparisonResult(
            label="前月比",
            target_date=today,
            compare_date=last_month,
            total_diff=285_000,
            total_ratio=1.35,
            by_class_diff={
                "株式（現物）": 180_000,
                "投資信託": 95_000,
                "年金": 25_000,
                "預金・現金": -15_000,
            },
            account_diffs=[],
            holding_diffs=monthly_hdiffs,
        ),
        ComparisonResult(
            label="前年比",
            target_date=today,
            compare_date=last_year,
            total_diff=3_420_000,
            total_ratio=18.9,
            by_class_diff={
                "株式（現物）": 1_650_000,
                "投資信託": 1_280_000,
                "年金": 580_000,
                "預金・現金": -90_000,
            },
            account_diffs=[],
            holding_diffs=yearly_hdiffs,
        ),
    ]

    # 配当利回り別内訳
    demo_yield_breakdown = {"低配当 (0-2%)": 0.0, "中配当 (2-4%)": 0.0, "高配当 (4%超)": 0.0}
    for d in demo_dividends:
        cy = d.get("current_yield", 0)
        stock_val = d["annual"] / (cy / 100) if cy > 0 else 0
        if cy < 2:
            demo_yield_breakdown["低配当 (0-2%)"] += stock_val
        elif cy < 4:
            demo_yield_breakdown["中配当 (2-4%)"] += stock_val
        else:
            demo_yield_breakdown["高配当 (4%超)"] += stock_val

    # 業種別配当
    demo_sector_dividends = {
        "輸送用機器": {"value": 1_260_000, "dividend": 22_500, "yield": 22_500 / 1_260_000 * 100},
        "電気機器": {"value": 2_315_000, "dividend": 21_900, "yield": 21_900 / 2_315_000 * 100},
        "卸売業": {"value": 875_000, "dividend": 10_000, "yield": 10_000 / 875_000 * 100},
        "化学": {"value": 720_000, "dividend": 12_000, "yield": 12_000 / 720_000 * 100},
        "その他製品": {"value": 580_000, "dividend": 18_300, "yield": 18_300 / 580_000 * 100},
        "機械": {"value": 350_000, "dividend": 10_000, "yield": 10_000 / 350_000 * 100},
        "鉱業": {"value": 250_000, "dividend": 30_000, "yield": 30_000 / 250_000 * 100},
    }

    # 業種別→銘柄マッピング（デモ用）
    demo_sector_map = {
        "7203": "輸送用機器",
        "6758": "電気機器",
        "8058": "卸売業",
        "4063": "化学",
        "6501": "電気機器",
        "6861": "電気機器",
        "7974": "その他製品",
        "6367": "機械",
        "1605": "鉱業",
    }
    demo_sector_holdings: dict[str, list] = {}
    for h in holdings:
        if h["asset_class"] == "株式（現物）" and h["code"]:
            sec = demo_sector_map.get(h["code"], "その他")
            demo_sector_holdings.setdefault(sec, []).append({"name": h["name"], "value": h["value"]})
    for v in demo_sector_holdings.values():
        v.sort(key=lambda x: x["value"], reverse=True)

    # 投資信託 評価額・取得価額推移デモデータ（365日分）
    import random

    rng = random.Random(42)
    fund_base_cost = 3_600_000  # 取得価額の開始値
    fund_total_history = []
    cost = fund_base_cost
    for i in range(365):
        d = date.today() - timedelta(days=364 - i)
        # 取得価額: 毎月1日に積立で階段状に増加
        if d.day == 1:
            cost += 50_000
        # 評価額: 取得価額 + 含み益（市場変動あり）
        growth_rate = 1 + 0.0002 * i + rng.uniform(-0.008, 0.008)
        value = cost * growth_rate * (1 + 0.1)  # 約10%の含み益ベース
        fund_total_history.append(
            {
                "date": d.isoformat(),
                "total_value": round(value),
                "total_cost": round(cost),
            }
        )

    holding_histories: dict[str, dict] = {}
    for idx, h in enumerate(holdings):
        if not h.get("value"):
            continue
        latest_cost = h["value"] - h["unrealized_gain"] if h.get("unrealized_gain") is not None else None
        hist = []
        base_value = h["value"] * (0.82 + idx * 0.01)
        base_cost = latest_cost * 0.9 if latest_cost else None
        for i in range(365):
            d = date.today() - timedelta(days=364 - i)
            growth = 1 + 0.0006 * i + rng.uniform(-0.012, 0.012)
            total_value_hist = round(base_value * growth)
            if base_cost is not None:
                total_cost_hist = round(min(base_cost + i * max(0, h["value"] * 0.00008), latest_cost))
            else:
                total_cost_hist = None
            hist.append(
                {
                    "date": d.isoformat(),
                    "total_value": total_value_hist,
                    "total_cost": total_cost_hist,
                }
            )
        key = _holding_history_key(h["asset_class"], h["code"], h["name"])
        holding_histories[key] = {
            "key": key,
            "name": h["name"],
            "code": h["code"],
            "asset_class": h["asset_class"],
            "history": hist,
            "latest_value": h["value"],
            "latest_cost": latest_cost,
        }

    return {
        "date": today,
        "total_asset": total_asset,
        "by_class": by_class,
        "accounts": accounts,
        "holdings": holdings,
        "sector_totals": demo_sectors,
        "dividends": demo_dividends,
        "total_dividend": sum(d["annual"] for d in demo_dividends),
        "dividend_error_count": 0,
        "yield_breakdown": demo_yield_breakdown,
        "sector_dividends": demo_sector_dividends,
        "volatility": 0.142,
        "max_drawdown": 3.8,
        "concentration": {"top_n": [], "concentration_pct": 32.5},
        "comparisons": demo_comparisons,
        "_sector_holdings": demo_sector_holdings,
        "fund_total_history": fund_total_history,
        "holding_histories": holding_histories,
        "investable_cash": {
            "as_of": today,
            "snapshot_date": today,
            "cash_balance": 3_250_000,
            "monthly_living_expense": 300_000,
            "monthly_living_expense_source": "setting",
            "emergency_fund_months": 6,
            "emergency_fund": 1_800_000,
            "planned_expense_horizon_months": 12,
            "planned_expenses": 300_000,
            "planned_expenses_by_year": {int(today[:4]): 300_000},
            "scheduled_card_payments": [],
            "scheduled_card_payment_total": 0,
            "additional_reserve": 150_000,
            "required_cash": 2_250_000,
            "investable_cash": 1_000_000,
            "shortfall": 0,
            "formula": "cash - emergency_fund - planned_expenses - scheduled_card_payments - additional_reserve",
        },
    }


def _calc_monthly_totals(conn: sqlite3.Connection) -> list[dict]:
    """スナップショットから月末の総資産推移を返す。

    各月の最終スナップショットを採用する。
    Returns: [{"year_month": "2026-02", "total": ...}, ...] 古い順
    """
    rows = conn.execute("SELECT date, total_asset FROM snapshots ORDER BY date ASC").fetchall()
    if not rows:
        return []

    # 月ごとの最終値を集める
    monthly_end: dict[str, float] = {}
    for date_str, total in rows:
        ym = date_str[:7]  # "YYYY-MM"
        monthly_end[ym] = total  # 後勝ちで最終日の値が残る

    return [{"year_month": ym, "total": monthly_end[ym]} for ym in sorted(monthly_end.keys())]


def _get_plan_data(db_path: str, monthly_contribution: float | None = None) -> dict:
    """月次収支 + 成長予測データを取得する。"""
    conn = get_connection(db_path)
    try:
        # 積立額: 引数指定があればDBに保存、なければDBから読む
        if monthly_contribution is not None:
            save_setting(conn, "monthly_contribution", str(int(monthly_contribution)))
        else:
            monthly_contribution = float(get_setting(conn, "monthly_contribution", "50000"))

        # 最新スナップショット情報を取得
        row = conn.execute(
            "SELECT date, total_asset, by_class_json FROM snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return {}

        date = row[0]
        total_asset = row[1]
        by_class = json.loads(row[2])

        # 月次収支データ（テーブルが無い場合も init_db で作成済み）
        cashflows = get_cashflows(conn, limit=12)

        # 古い順に並び替え
        cashflows.reverse()

        # 月末スナップショットから月次資産推移を算出
        monthly_totals = _calc_monthly_totals(conn)

        # 年金の株式型を判定してリスク資産に移す
        holdings_for_pension = [
            {"name": r[0], "asset_class": r[1], "value": r[2]}
            for r in conn.execute(
                "SELECT name, asset_class, value FROM snapshot_holdings WHERE date = ? AND asset_class = '年金'",
                (date,),
            ).fetchall()
        ]

        # 家計簿の実績貯蓄データ
        closing_day = int(get_setting(conn, "closing_day", "1") or "1")
        holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"
        cf_savings = get_cf_actual_savings(conn, closing_day=closing_day, holiday_mode=holiday_mode)

        # 日次資産推移
        daily_assets = get_daily_assets(conn, months=6)

        # 配当実績
        dividend_history = get_cf_dividend_history(conn, closing_day=closing_day, holiday_mode=holiday_mode)
    finally:
        conn.close()

    eq_pension, ins_pension = classify_pension_holdings(holdings_for_pension)

    # リスク資産 / 安全資産の分離
    risk_value = sum(v for cls, v in by_class.items() if cls in RISK_CLASSES)
    risk_value += eq_pension
    safe_value = total_asset - risk_value

    # クラス別評価額（加重デフォルトパラメータ計算用）
    class_values = {}
    for cls, v in by_class.items():
        if cls in RISK_CLASSES:
            class_values[cls] = v
    if eq_pension > 0:
        class_values["年金（株式型）"] = eq_pension

    # 成長予測（追加投資なし）
    try:
        predictions, pred_params = predict_no_contribution(
            db_path, risk_value, safe_value, simulations=2000, class_values=class_values
        )
    except Exception:
        predictions, pred_params = [], {}

    # 成長予測（積立込み）
    try:
        predictions_c, pred_params_c = predict_with_contribution(
            db_path, risk_value, safe_value, monthly_contribution, simulations=2000, class_values=class_values
        )
    except Exception:
        predictions_c, pred_params_c = [], {}

    return {
        "date": date,
        "total_asset": total_asset,
        "cashflows": cashflows,
        "monthly_totals": monthly_totals,
        "predictions": predictions,
        "pred_params": pred_params,
        "predictions_contrib": predictions_c,
        "pred_params_contrib": pred_params_c,
        "monthly_contribution": monthly_contribution,
        "cf_savings": cf_savings,
        "daily_assets": daily_assets,
        "dividend_history": dividend_history,
    }


def _demo_plan_data() -> dict:
    """ライフプランページ用のデモデータを生成する。"""
    cashflows = [
        {"year_month": "2025-09", "income": 380000, "expense": 310000},
        {"year_month": "2025-10", "income": 385000, "expense": 345000},
        {"year_month": "2025-11", "income": 380000, "expense": 290000},
        {"year_month": "2025-12", "income": 520000, "expense": 420000},
        {"year_month": "2026-01", "income": 385000, "expense": 320000},
        {"year_month": "2026-02", "income": 380000, "expense": 305000},
    ]

    monthly_totals = [
        {"year_month": "2025-09", "total": 19_800_000},
        {"year_month": "2025-10", "total": 19_650_000},
        {"year_month": "2025-11", "total": 20_100_000},
        {"year_month": "2025-12", "total": 20_550_000},
        {"year_month": "2026-01", "total": 21_200_000},
        {"year_month": "2026-02", "total": 21_500_000},
    ]

    demo_predictions = [
        PredictionRange(years=1, p10=19_800_000, p50=22_100_000, p90=24_800_000),
        PredictionRange(years=3, p10=18_200_000, p50=24_500_000, p90=33_100_000),
        PredictionRange(years=5, p10=17_500_000, p50=27_800_000, p90=44_200_000),
        PredictionRange(years=10, p10=16_000_000, p50=36_500_000, p90=82_000_000),
        PredictionRange(years=20, p10=18_500_000, p50=63_000_000, p90=215_000_000),
        PredictionRange(years=30, p10=24_000_000, p50=110_000_000, p90=500_000_000),
    ]
    demo_pred_params = {
        "annual_return": 0.05,
        "annual_volatility": 0.15,
        "is_estimated": True,
        "data_points": 1,
        "risk_value": 11_530_000,
        "safe_value": 9_970_000,
    }
    demo_predictions_c = [
        PredictionRange(years=1, p10=20_400_000, p50=22_700_000, p90=25_400_000),
        PredictionRange(years=3, p10=20_000_000, p50=26_500_000, p90=35_200_000),
        PredictionRange(years=5, p10=20_800_000, p50=31_200_000, p90=47_500_000),
        PredictionRange(years=10, p10=22_000_000, p50=42_000_000, p90=90_000_000),
        PredictionRange(years=20, p10=30_000_000, p50=85_000_000, p90=250_000_000),
        PredictionRange(years=30, p10=45_000_000, p50=160_000_000, p90=600_000_000),
    ]

    # 日次資産推移デモデータ（30日分）
    from datetime import date as _date
    from datetime import timedelta

    demo_daily = []
    base_total = 19_500_000
    base_classes = {
        "株式（現物）": 8_000_000,
        "投資信託": 2_800_000,
        "年金": 2_600_000,
        "預金・現金": 4_900_000,
        "不動産": 1_200_000,
    }
    for i in range(180):
        d = _date(2025, 8, 19) + timedelta(days=i)
        # 緩やかな上昇 + 小さなランダム風の変動
        drift = int(base_total * 0.001 * i + (((i * 7 + 3) % 11) - 5) * 30_000)
        total = base_total + drift
        by_class = {}
        remaining = total
        for j, (cls, base_val) in enumerate(base_classes.items()):
            if j == len(base_classes) - 1:
                by_class[cls] = remaining
            else:
                val = int(base_val + base_val * 0.001 * i + (((i * 3 + j * 5) % 7) - 3) * 10_000)
                by_class[cls] = val
                remaining -= val
        demo_daily.append({"date": d.isoformat(), "total": total, "by_class": by_class})

    return {
        "date": "2026-02-14",
        "total_asset": 21_500_000,
        "cashflows": cashflows,
        "monthly_totals": monthly_totals,
        "predictions": demo_predictions,
        "pred_params": demo_pred_params,
        "predictions_contrib": demo_predictions_c,
        "pred_params_contrib": demo_pred_params,
        "monthly_contribution": 50000,
        "cf_savings": {
            "avg_income": 388_333,
            "avg_expense": 331_667,
            "avg_savings": 56_667,
            "savings_rate": 14.6,
            "months_used": 6,
        },
        "daily_assets": demo_daily,
        "dividend_history": {
            "monthly": [
                {"year_month": "2025-03", "amount": 12500},
                {"year_month": "2025-06", "amount": 18200},
                {"year_month": "2025-09", "amount": 15800},
                {"year_month": "2025-12", "amount": 22300},
                {"year_month": "2026-01", "amount": 5200},
            ],
            "annual": [
                {"year": "2025", "amount": 68800},
                {"year": "2026", "amount": 5200},
            ],
        },
    }


# --- シミュレーター ---

_SIMULATOR_DEFAULTS = {
    "current_age": 35,
    "retirement_age": 65,
    "end_age": 95,
    "initial_investment": 5_000_000,
    "safe_value": 5_000_000,
    "monthly_contribution": 50_000,
    "annual_return": 0.05,
    "annual_volatility": 0.15,
    "monthly_withdrawal": 200_000,
    "inflation_rate": 0.02,
    "expense_ratio": 0.003,
    "pension_start_age": 65,
    "monthly_pension": 150_000,
    "other_monthly_income": 0,
    # 再雇用・嘱託フェーズ（retirement_age と同値なら再雇用なし）
    "reemployment_end_age": 65,
    "reemployment_monthly_income": 0,
}


def _demo_simulator_data() -> dict:
    """シミュレーターページ用のデモデータを生成する。"""
    # ライフプランのデモデータと同じ値を使用
    params = dict(_SIMULATOR_DEFAULTS)
    params["initial_investment"] = 11_530_000  # _demo_plan_data() の risk_value
    params["safe_value"] = 9_970_000  # _demo_plan_data() の safe_value
    params["monthly_contribution"] = 50_000  # _demo_plan_data() の monthly_contribution
    result = run_lifecycle_simulation(**params, rng_seed=42)
    return {
        "params": params,
        "result": result,
        "result_no_events": result,
        "life_events": [],
        "children_profiles": [],
        "life_inflation_rate": 0.01,
        "annual_event_expenses_by_age": {},
        "total_event_expense": 0.0,
    }


def _sanitize_simulator_params(params: dict) -> dict:
    """DB から読み込んだシミュレーターパラメータを正規化し、範囲外ならデフォルトに戻す。"""
    defaults = _SIMULATOR_DEFAULTS
    clean: dict = {}
    # 各キーを型変換＋範囲チェック（失敗時はデフォルト値にフォールバック）
    int_keys = {"current_age", "retirement_age", "end_age", "pension_start_age", "reemployment_end_age"}
    for k, default_v in defaults.items():
        try:
            v = int(params[k]) if k in int_keys else float(params[k])
            if not math.isfinite(v) if isinstance(v, float) else False:
                v = default_v
        except (ValueError, TypeError, KeyError):
            v = default_v
        clean[k] = v
    # 年齢整合性: 崩れていたら全てデフォルトに戻す
    if not (clean["current_age"] <= clean["retirement_age"] <= clean["end_age"]):
        for k in ("current_age", "retirement_age", "end_age"):
            clean[k] = defaults[k]
    # pension_start_age 範囲
    if not (60 <= clean["pension_start_age"] <= 75):
        clean["pension_start_age"] = defaults["pension_start_age"]
    # 再雇用終了年齢: 退職年齢〜終了年齢の範囲外なら退職年齢（再雇用なし）に戻す
    if not (clean["retirement_age"] <= clean["reemployment_end_age"] <= clean["end_age"]):
        clean["reemployment_end_age"] = clean["retirement_age"]
    # 金額非負 + 上限
    _MAX_LUMP = 200_000_000
    _MAX_MONTHLY = 1_000_000
    for k, upper in [
        ("initial_investment", _MAX_LUMP),
        ("safe_value", _MAX_LUMP),
        ("monthly_contribution", _MAX_MONTHLY),
        ("monthly_withdrawal", _MAX_MONTHLY),
        ("monthly_pension", 500_000),
        ("other_monthly_income", 500_000),
        ("reemployment_monthly_income", _MAX_MONTHLY),
    ]:
        if clean[k] < 0 or clean[k] > upper:
            clean[k] = defaults[k]
    # レート範囲
    for k, lo, hi in [
        ("annual_return", 0.0, 0.15),
        ("annual_volatility", 0.01, 0.40),
        ("inflation_rate", 0.0, 0.10),
        ("expense_ratio", 0.0, 0.03),
    ]:
        if not (lo <= clean[k] <= hi):
            clean[k] = defaults[k]
    return clean


def _annual_event_expenses_by_age(
    conn: sqlite3.Connection,
    current_age: int,
    end_age: int,
) -> dict[int, float]:
    """DBイベントを「年齢 -> 年次支出」へ変換する。"""
    now_year = datetime.now().year
    end_year = now_year + max(0, end_age - current_age)
    by_year = get_annual_life_event_expenses(conn, start_year=now_year, end_year=end_year, include_children=True)
    by_age: dict[int, float] = {}
    for year, amount in by_year.items():
        age = current_age + (year - now_year) + 1  # 年末時点の年齢
        if current_age < age <= end_age:
            by_age[age] = by_age.get(age, 0.0) + float(amount)
    return by_age


def _annual_event_details_by_age(
    conn: sqlite3.Connection,
    current_age: int,
    end_age: int,
) -> dict[int, list[dict]]:
    """DBイベントを「年齢 -> 詳細イベント配列」へ変換する。"""
    now_year = datetime.now().year
    end_year = now_year + max(0, end_age - current_age)
    inflation_rate = get_life_plan_inflation_rate(conn)
    base_year = now_year

    events = list_life_events(conn, enabled_only=True)
    for child in list_children_profiles(conn, enabled_only=True):
        events.extend(
            build_education_events_for_child(
                child=child,
                start_year=now_year,
                end_year=end_year,
                inflation_rate=0.0,
                base_year=base_year,
            )
        )

    details_by_age: dict[int, list[dict]] = {}
    for ev in events:
        if not ev.get("enabled", True):
            continue
        first_year = int(ev.get("start_year", now_year))
        repeat = ev.get("repeat_every_years")
        repeat_years = int(repeat) if repeat not in (None, 0, "") else None
        until = ev.get("end_year")
        last_year = int(until) if until not in (None, "") else end_year
        last_year = min(last_year, end_year)
        amount_base = max(0.0, float(ev.get("amount", 0.0)))

        years = [first_year] if repeat_years is None else list(range(first_year, last_year + 1, repeat_years))
        for year in years:
            if year < now_year or year > end_year:
                continue
            age = current_age + (year - now_year) + 1
            if not (current_age < age <= end_age):
                continue
            amount = amount_base * ((1 + inflation_rate) ** max(0, year - base_year))
            details_by_age.setdefault(age, []).append({"title": str(ev.get("title", "イベント")), "amount": amount})
    return details_by_age


def _get_simulator_data(db_path: str) -> dict:
    """DB設定からシミュレーターパラメータを読み込み、シミュレーションを実行する。"""
    conn = get_connection(db_path)
    try:
        raw = get_setting(conn, "simulator_params", "")
        if raw:
            try:
                params = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                params = {}
            # デフォルト値で補完 → 正規化
            for k, v in _SIMULATOR_DEFAULTS.items():
                if k not in params:
                    params[k] = v
            params = _sanitize_simulator_params(params)
        else:
            # 初回: ライフプランの実データからデフォルト値を構築
            params = dict(_SIMULATOR_DEFAULTS)
            row = conn.execute(
                "SELECT date, total_asset, by_class_json FROM snapshots ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if row:
                date, total_asset, by_class_json = row
                by_class = json.loads(by_class_json)
                # リスク資産の算出（ライフプランと同じロジック）
                risk_value = sum(v for cls, v in by_class.items() if cls in RISK_CLASSES)
                # 年金の株式型を判定
                holdings = [
                    {"name": r[0], "asset_class": r[1], "value": r[2]}
                    for r in conn.execute(
                        "SELECT name, asset_class, value FROM snapshot_holdings "
                        "WHERE date = ? AND asset_class = '年金'",
                        (date,),
                    ).fetchall()
                ]
                eq_pension, _ = classify_pension_holdings(holdings)
                risk_value += eq_pension
                params["initial_investment"] = risk_value
                params["safe_value"] = total_asset - risk_value
            contrib = get_setting(conn, "monthly_contribution", "")
            if contrib:
                with contextlib.suppress(ValueError, TypeError):
                    params["monthly_contribution"] = float(contrib)
            params = _sanitize_simulator_params(params)
        annual_event_expenses = _annual_event_expenses_by_age(
            conn=conn,
            current_age=int(params["current_age"]),
            end_age=int(params["end_age"]),
        )
        annual_event_details_by_age = _annual_event_details_by_age(
            conn=conn,
            current_age=int(params["current_age"]),
            end_age=int(params["end_age"]),
        )
        life_events = list_life_events(conn, enabled_only=False)
        children_profiles = list_children_profiles(conn, enabled_only=False)
        life_inflation_rate = get_life_plan_inflation_rate(conn)
    finally:
        conn.close()

    result = run_lifecycle_simulation(
        current_age=int(params["current_age"]),
        retirement_age=int(params["retirement_age"]),
        end_age=int(params["end_age"]),
        initial_investment=float(params["initial_investment"]),
        safe_value=float(params["safe_value"]),
        monthly_contribution=float(params["monthly_contribution"]),
        annual_return=float(params["annual_return"]),
        annual_volatility=float(params["annual_volatility"]),
        monthly_withdrawal=float(params["monthly_withdrawal"]),
        inflation_rate=float(params["inflation_rate"]),
        expense_ratio=float(params["expense_ratio"]),
        pension_start_age=int(params["pension_start_age"]),
        monthly_pension=float(params["monthly_pension"]),
        other_monthly_income=float(params["other_monthly_income"]),
        reemployment_end_age=int(params["reemployment_end_age"]),
        reemployment_monthly_income=float(params["reemployment_monthly_income"]),
        annual_event_expenses=annual_event_expenses,
        rng_seed=42,
    )
    result_no_events = run_lifecycle_simulation(
        current_age=int(params["current_age"]),
        retirement_age=int(params["retirement_age"]),
        end_age=int(params["end_age"]),
        initial_investment=float(params["initial_investment"]),
        safe_value=float(params["safe_value"]),
        monthly_contribution=float(params["monthly_contribution"]),
        annual_return=float(params["annual_return"]),
        annual_volatility=float(params["annual_volatility"]),
        monthly_withdrawal=float(params["monthly_withdrawal"]),
        inflation_rate=float(params["inflation_rate"]),
        expense_ratio=float(params["expense_ratio"]),
        pension_start_age=int(params["pension_start_age"]),
        monthly_pension=float(params["monthly_pension"]),
        other_monthly_income=float(params["other_monthly_income"]),
        reemployment_end_age=int(params["reemployment_end_age"]),
        reemployment_monthly_income=float(params["reemployment_monthly_income"]),
        annual_event_expenses={},
        rng_seed=42,
    )
    return {
        "params": params,
        "result": result,
        "result_no_events": result_no_events,
        "life_events": life_events,
        "children_profiles": children_profiles,
        "life_inflation_rate": life_inflation_rate,
        "annual_event_expenses_by_age": annual_event_expenses,
        "annual_event_details_by_age": annual_event_details_by_age,
        "total_event_expense": sum(float(v) for v in annual_event_expenses.values()),
    }


def _build_ai_prompt_simulator(data: dict) -> str:
    """シミュレーター結果からAIチャット用Markdownプロンプトを生成する。"""
    params = data["params"]
    result: SimulatorResult = data["result"]

    retirement_age = int(params["retirement_age"])
    reemployment_end_age = int(params.get("reemployment_end_age", retirement_age))
    reemployment_income = float(params.get("reemployment_monthly_income", 0))
    has_reemployment = reemployment_end_age > retirement_age

    lines = [
        "# ライフサイクル・シミュレーション結果",
        "",
        "## 前提条件",
        "",
        "| 項目 | 値 |",
        "|---|---:|",
        f"| 現在の年齢 | {int(params['current_age'])}歳 |",
        f"| 退職年齢 | {int(params['retirement_age'])}歳 |",
        f"| 再雇用・嘱託の終了年齢 | {reemployment_end_age}歳 |",
        f"| 再雇用期間の月収 | {reemployment_income:,.0f}円 |",
        f"| シミュレーション終了年齢 | {int(params['end_age'])}歳 |",
        f"| リスク資産（運用元本） | {params['initial_investment']:,.0f}円 |",
        f"| 安全資産（預金等） | {params['safe_value']:,.0f}円 |",
        f"| 毎月の積立額（退職まで） | {params['monthly_contribution']:,.0f}円 |",
        f"| 期待リターン（年率） | {params['annual_return'] * 100:.1f}% |",
        f"| リスク（年率ボラティリティ） | {params['annual_volatility'] * 100:.1f}% |",
        f"| 退職後の毎月取り崩し額 | {params['monthly_withdrawal']:,.0f}円 |",
        f"| インフレ率 | {params['inflation_rate'] * 100:.1f}% |",
        f"| 信託報酬率 | {params['expense_ratio'] * 100:.2f}% |",
        f"| 年金受給開始年齢 | {int(params['pension_start_age'])}歳 |",
        f"| 年金月額 | {params['monthly_pension']:,.0f}円 |",
        f"| その他月収入 | {params['other_monthly_income']:,.0f}円 |",
        "",
    ]

    if has_reemployment:
        lines += [
            f"※ フェーズ: 現役（〜{retirement_age}歳）→ 再雇用・嘱託（{retirement_age}〜{reemployment_end_age}歳、"
            f"月収{reemployment_income:,.0f}円）→ 完全退職（{reemployment_end_age}歳〜、年金＋その他収入のみ）",
            "",
        ]
    else:
        lines += [
            "※ 再雇用フェーズなし（退職年齢以降は年金＋その他収入のみ）",
            "",
        ]

    lines += [
        "## シミュレーション結果（モンテカルロ法 2,000回）",
        "",
        f"- 資産枯渇確率: **{result.depletion_probability * 100:.1f}%**",
        f"- 元本割れ確率: **{result.principal_loss_probability * 100:.1f}%**",
        f"- 投入元本合計: {result.total_principal:,.0f}円",
        f"- 運用益（中央値）: {result.total_gains:,.0f}円",
        f"- 税金合計（中央値）: {result.total_tax:,.0f}円",
        f"- 最終残高（中央値）: {result.net_final:,.0f}円",
        "",
        "## 年齢別資産残高（パーセンタイル）",
        "",
        "| 年齢 | 悲観(P10) | P25 | 中央値(P50) | P75 | 楽観(P90) |",
        "|---:|---:|---:|---:|---:|---:|",
    ]

    # 主要な年齢ポイントのみ抽出（全年表示だと長すぎる）
    balances = result.yearly_balances
    key_ages = set()
    if balances:
        key_ages.add(balances[0]["age"])  # 開始年齢
        key_ages.add(balances[-1]["age"])  # 終了年齢
    key_ages.add(int(params["retirement_age"]))
    key_ages.add(reemployment_end_age)
    key_ages.add(int(params["pension_start_age"]))
    # 5歳刻みも追加
    for b in balances:
        if b["age"] % 5 == 0:
            key_ages.add(b["age"])

    for b in balances:
        if b["age"] in key_ages:
            lines.append(
                f"| {b['age']}歳 | {b['p10']:,.0f}円 | {b['p25']:,.0f}円 "
                f"| {b['p50']:,.0f}円 | {b['p75']:,.0f}円 | {b['p90']:,.0f}円 |"
            )

    lines += [
        "",
        "---",
        "",
        "上記はモンテカルロ・シミュレーションの結果です。以下の観点でアドバイスをお願いします：",
        "1. 資産枯渇リスクの評価と対策",
        "2. 前提条件（積立額・取り崩し額・リターン等）の妥当性",
        "3. 退職後の収支バランスの改善提案",
        "4. シミュレーション結果を踏まえた具体的なアクションプラン",
    ]
    return "\n".join(lines)


def _build_simulator_html(data: dict, skip_update: bool = False) -> str:
    """シミュレーターページの HTML を生成する。"""
    params = data["params"]
    result: SimulatorResult = data["result"]
    result_no_events: SimulatorResult = data.get("result_no_events", result)
    life_events = data.get("life_events", [])
    children_profiles = data.get("children_profiles", [])
    life_inflation_rate = float(data.get("life_inflation_rate", 0.01))
    annual_event_expenses_by_age = data.get("annual_event_expenses_by_age", {})
    annual_event_details_by_age = data.get("annual_event_details_by_age", {})
    total_event_expense = float(data.get("total_event_expense", 0.0))

    # パラメータ表示用
    def _fmt_money(v: float) -> str:
        return f"{v:,.0f}"

    def _fmt_pct(v: float) -> str:
        return f"{v * 100:.1f}"

    # --- パラメータ入力カード ---
    param_fields = [
        # (id, label, value, min, max, step, unit, input_type)
        # 基本パラメータ
        (
            "current_age",
            "現在の年齢",
            params["current_age"],
            20,
            80,
            1,
            "歳",
            "stepper",
            "シミュレーション開始時点の年齢",
        ),
        (
            "retirement_age",
            "退職年齢",
            params["retirement_age"],
            30,
            85,
            1,
            "歳",
            "stepper",
            "この年齢以降は積立を止め、取崩しフェーズに入ります",
        ),
        (
            "reemployment_end_age",
            "再雇用終了年齢",
            params["reemployment_end_age"],
            30,
            85,
            1,
            "歳",
            "stepper",
            "定年後に再雇用・嘱託で働き終える年齢。退職年齢と同じなら再雇用なし（例: 60歳定年・65歳まで嘱託）",
        ),
        (
            "reemployment_monthly_income",
            "再雇用の月額収入",
            params["reemployment_monthly_income"],
            0,
            1_000_000,
            10_000,
            "円",
            "number",
            "退職年齢から再雇用終了年齢まで毎月受け取る収入。退職前月収の6割程度が目安",
        ),
        (
            "end_age",
            "シミュレーション終了年齢",
            params["end_age"],
            70,
            110,
            1,
            "歳",
            "stepper",
            "資産推移を何歳まで試算するか",
        ),
        # 金額パラメータ
        (
            "initial_investment",
            "リスク資産額",
            params["initial_investment"],
            0,
            200_000_000,
            100_000,
            "円",
            "number",
            "株式・投信など価格変動する資産の現在残高",
        ),
        (
            "safe_value",
            "安全資産額",
            params["safe_value"],
            0,
            200_000_000,
            100_000,
            "円",
            "number",
            "預金・現金など価格変動を想定しない資産の現在残高",
        ),
        (
            "monthly_contribution",
            "月額積立",
            params["monthly_contribution"],
            0,
            1_000_000,
            10_000,
            "円",
            "number",
            "退職年齢まで毎月リスク資産へ積み立てる金額",
        ),
        (
            "monthly_withdrawal",
            "月額取崩し（生活費）",
            params["monthly_withdrawal"],
            0,
            1_000_000,
            10_000,
            "円",
            "number",
            "退職後に毎月取り崩す金額（生活費想定）",
        ),
        # リターンパラメータ
        (
            "annual_return",
            "期待リターン（年率）",
            params["annual_return"],
            0.0,
            0.15,
            0.005,
            "%",
            "range",
            "リスク資産の平均的な年間リターン想定。初心者の目安: 保守 2〜4% / 標準 4〜6% / 強気 6〜8%",
        ),
        (
            "annual_volatility",
            "ボラティリティ（年率）",
            params["annual_volatility"],
            0.01,
            0.40,
            0.005,
            "%",
            "range",
            "リターンのぶれ幅。高いほど結果の上下が大きくなります。目安: 低め 8〜12% / 標準 12〜20% / 高め 20%以上",
        ),
        (
            "inflation_rate",
            "インフレ率",
            params["inflation_rate"],
            0.0,
            0.10,
            0.005,
            "%",
            "range",
            "物価上昇率。将来価値を現在価値へ換算するために使用。目安: 低め 0〜1% / 標準 1〜3% / 高め 3%以上",
        ),
        (
            "expense_ratio",
            "信託報酬",
            params["expense_ratio"],
            0.0,
            0.03,
            0.001,
            "%",
            "range",
            "運用商品にかかる年間コスト（年率）。目安: 低コスト指数 0.05〜0.30% / アクティブ 0.5〜2.0%",
        ),
        # 年金・収入
        (
            "pension_start_age",
            "年金受給開始年齢",
            params["pension_start_age"],
            60,
            75,
            1,
            "歳",
            "stepper",
            "公的年金を受け取り始める年齢",
        ),
        (
            "monthly_pension",
            "月額年金",
            params["monthly_pension"],
            0,
            500_000,
            10_000,
            "円",
            "number",
            "公的年金の月額。独身目安:約14.6万円／夫婦目安:約29.2万円（65歳・額面）",
        ),
        (
            "other_monthly_income",
            "年金以外の月額収入",
            params["other_monthly_income"],
            0,
            500_000,
            10_000,
            "円",
            "number",
            "家賃収入・副業など、年金以外の定期収入",
        ),
    ]

    param_rows_html = ""
    for field in param_fields:
        fid, label, val, fmin, fmax, step, unit, itype = field[:8]
        tooltip = field[8] if len(field) > 8 else None
        label_html = label
        if tooltip:
            label_html += f' <span class="sim-info-btn" tabindex="0" data-tooltip="{tooltip}">i</span>'
        if itype == "stepper":
            param_rows_html += f"""
          <div class="sim-field">
            <label for="{fid}">{label_html}</label>
            <div class="sim-input-row">
              <button type="button" class="stepper-btn" onclick="stepVal('{fid}',-1,{fmin},{fmax})">-</button>
              <input type="number" id="{fid}" name="{fid}" min="{fmin}" max="{fmax}" step="{step}" value="{int(val)}" class="stepper-input">
              <button type="button" class="stepper-btn" onclick="stepVal('{fid}',1,{fmin},{fmax})">+</button>
              <span class="sim-unit">{unit}</span>
            </div>
          </div>"""
        elif itype == "range":
            if unit == "%":
                display_val = f"{float(val) * 100:.1f}"
                param_rows_html += f"""
          <div class="sim-field">
            <label for="{fid}">{label_html}</label>
            <div class="sim-input-row">
              <input type="range" id="{fid}" name="{fid}" min="{fmin}" max="{fmax}" step="{step}" value="{val}"
                     oninput="document.getElementById('{fid}-val').textContent=(this.value*100).toFixed(1)">
              <span id="{fid}-val">{display_val}</span><span class="sim-unit">{unit}</span>
            </div>
          </div>"""
            else:
                param_rows_html += f"""
          <div class="sim-field">
            <label for="{fid}">{label_html}</label>
            <div class="sim-input-row">
              <input type="range" id="{fid}" name="{fid}" min="{fmin}" max="{fmax}" step="{step}" value="{val}"
                     oninput="document.getElementById('{fid}-val').textContent=this.value">
              <span id="{fid}-val">{val}</span><span class="sim-unit">{unit}</span>
            </div>
          </div>"""
        else:
            formatted_val = f"{int(val):,}"
            param_rows_html += f"""
          <div class="sim-field">
            <label for="{fid}">{label_html}</label>
            <div class="sim-input-row">
              <input type="text" inputmode="numeric" id="{fid}" name="{fid}" data-min="{fmin}" data-max="{fmax}" data-step="{step}" value="{formatted_val}" class="money-input">
              <span class="sim-unit">{unit}</span>
            </div>
          </div>"""

    # --- 財務サマリー ---
    depl_pct = result.depletion_probability * 100
    loss_pct = result.principal_loss_probability * 100
    depl_color = "#e74c3c" if depl_pct > 10 else "#f39c12" if depl_pct > 0 else "#27ae60"
    loss_color = "#e74c3c" if loss_pct > 30 else "#f39c12" if loss_pct > 10 else "#27ae60"
    net_impact = result.net_final - result_no_events.net_final
    impact_color = "#e74c3c" if net_impact < 0 else "#0F7F30"

    summary_html = f"""
    <div class="card full sim-summary-card" data-card-id="sim-summary">
      <div class="card-header">
        <h2>財務サマリー</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="sim-summary-grid">
        <div class="sim-summary-item">
          <div class="sim-summary-label">投入元本</div>
          <div class="sim-summary-value">{_fmt_money(result.total_principal)}円</div>
        </div>
        <div class="sim-summary-item">
          <div class="sim-summary-label">運用益（P50） <span class="sim-info-btn" tabindex="0" data-tooltip="今の貨幣価値に換算した実質値。インフレ分は差し引き済み">i</span></div>
          <div class="sim-summary-value">{_fmt_money(result.total_gains)}円</div>
        </div>
        <div class="sim-summary-item">
          <div class="sim-summary-label">税金合計（P50） <span class="sim-info-btn" tabindex="0" data-tooltip="取崩し時にリスク資産を売却した際の実現益への課税。含み益への潜在税は含まない">i</span></div>
          <div class="sim-summary-value">{_fmt_money(result.total_tax)}円</div>
        </div>
        <div class="sim-summary-item">
          <div class="sim-summary-label">最終残高（P50） <span class="sim-info-btn" tabindex="0" data-tooltip="今の貨幣価値での残高。全売却時は含み益に別途課税あり">i</span></div>
          <div class="sim-summary-value" style="font-size:1.3rem;color:#2881D7">{_fmt_money(result.net_final)}円</div>
        </div>
        <div class="sim-summary-item">
          <div class="sim-summary-label">イベント影響（最終残高差）</div>
          <div class="sim-summary-value" id="event-impact-val" style="color:{impact_color}">{net_impact:+,.0f}円</div>
        </div>
        <div class="sim-summary-item">
          <div class="sim-summary-label">期間内イベント支出合計</div>
          <div class="sim-summary-value" id="event-total-val">{total_event_expense:,.0f}円</div>
        </div>
      </div>
      <div class="sim-prob-grid">
        <div class="sim-prob-item">
          <span class="sim-prob-label">枯渇確率</span>
          <span class="sim-prob-value" style="color:{depl_color}">{depl_pct:.1f}%</span>
        </div>
        <div class="sim-prob-item">
          <span class="sim-prob-label">元本割れ確率</span>
          <span class="sim-prob-value" style="color:{loss_color}">{loss_pct:.1f}%</span>
        </div>
      </div>
      <div class="sim-notes" id="sim-notes">
        <div class="sim-notes-header" onclick="this.parentElement.classList.toggle('open')">
          <span class="sim-notes-icon">&#x25B6;</span> 計算の前提
        </div>
        <div class="sim-notes-body">
          <ul>
            <li><strong>すべての金額は今の貨幣価値（実質値）</strong>で表示。インフレ率はリターンから差し引いて計算するため、「今の感覚でいくら」が直感的にわかります</li>
            <li><strong>リスク資産</strong>はモンテカルロ法（2,000回）で月次シミュレーション。<strong>安全資産</strong>は変動なし</li>
            <li><strong>積立</strong>は現在〜退職年齢まで毎月リスク資産に加算</li>
            <li><strong>取崩し</strong>は退職後に毎月実行。安全資産から先に消費し、不足分をリスク資産から売却</li>
            <li><strong>税金</strong>（{float(params.get("tax_rate", 0.20315)) * 100:.1f}%）はリスク資産の売却時、含み益部分にのみ課税。含み益への潜在税や NISA 非課税枠は未考慮</li>
            <li><strong>年金・その他収入</strong>は受給開始年齢以降、毎月安全資産に加算</li>
            <li><strong>P50</strong> = 中央値（半分がこれ以上、半分がこれ以下）。P10 は悲観、P90 は楽観シナリオ</li>
          </ul>
        </div>
      </div>
      </div>
    </div>"""

    event_rows = ""
    for ev in life_events:
        repeat = f"{ev['repeat_every_years']}年ごと" if ev.get("repeat_every_years") else "単発"
        end_year = ev.get("end_year") if ev.get("end_year") is not None else "-"
        event_rows += (
            "<tr>"
            f"<td>{_h(ev['title'])}</td>"
            f"<td class='num'>{ev['amount']:,.0f}円</td>"
            f"<td class='num'>{ev['start_year']}</td>"
            f"<td>{repeat}</td>"
            f"<td class='num'>{end_year}</td>"
            "<td>"
            f"<button class='btn btn-edit-sm' data-id='{int(ev['id'])}' data-title='{_h(ev['title'])}' "
            f"data-amount='{float(ev['amount'])}' data-start-year='{int(ev['start_year'])}' "
            f"data-repeat='{int(ev.get('repeat_every_years') or 0)}' "
            f"data-end-year='{'' if ev.get('end_year') is None else int(ev['end_year'])}' "
            "onclick='editLifeEvent(this)'>編集</button> "
            f"<button class='btn btn-danger-sm' onclick='deleteLifeEvent({int(ev['id'])})'>削除</button>"
            "</td>"
            "</tr>"
        )
    if not event_rows:
        event_rows = "<tr><td colspan='6' style='color:#999'>イベント未登録</td></tr>"

    stage_options = {
        "kindergarten": [("public", "幼:公"), ("private", "幼:私")],
        "elementary": [("public", "小:公"), ("private", "小:私")],
        "junior_high": [("public", "中:公"), ("private", "中:私")],
        "high_school": [("public", "高:公"), ("private", "高:私")],
        "university": [("public", "大:国公立"), ("private_humanities", "大:私文"), ("private_science", "大:私理")],
    }

    def _plan_select(child_id: int, stage: str, cur: str) -> str:
        options = stage_options[stage]
        html = [f"<select class='plan-select' onchange=\"saveChildPlan({child_id}, '{stage}', this.value)\">"]
        for val, label in options:
            selected = " selected" if cur == val else ""
            html.append(f"<option value='{val}'{selected}>{label}</option>")
        html.append("</select>")
        return "".join(html)

    child_rows = ""
    for ch in children_profiles:
        plan = ch.get("education_plan", {})
        plan_html = " ".join(
            [
                _plan_select(int(ch["id"]), "kindergarten", plan.get("kindergarten", "public")),
                _plan_select(int(ch["id"]), "elementary", plan.get("elementary", "public")),
                _plan_select(int(ch["id"]), "junior_high", plan.get("junior_high", "public")),
                _plan_select(int(ch["id"]), "high_school", plan.get("high_school", "public")),
                _plan_select(int(ch["id"]), "university", plan.get("university", "public")),
            ]
        )
        child_rows += (
            "<tr>"
            f"<td>{_h(ch['name'])}</td>"
            f"<td class='num'>{int(ch['birth_year'])}/{int(ch['birth_month']):02d}</td>"
            f"<td>{plan_html}</td>"
            "<td>"
            f"<button class='btn btn-edit-sm' data-id='{int(ch['id'])}' data-name='{_h(ch['name'])}' "
            f"data-birth-year='{int(ch['birth_year'])}' data-birth-month='{int(ch['birth_month'])}' "
            "onclick='editChildProfile(this)'>編集</button> "
            f"<button class='btn btn-danger-sm' onclick='deleteChildProfile({int(ch['id'])})'>削除</button>"
            "</td>"
            "</tr>"
        )
    if not child_rows:
        child_rows = "<tr><td colspan='4' style='color:#999'>子ども未登録</td></tr>"

    expense_rows = ""
    expense_count = 0
    expense_total = 0.0
    expense_max_age = None
    expense_max_amount = 0.0
    for age, amount in sorted((int(k), float(v)) for k, v in annual_event_expenses_by_age.items()):
        details = annual_event_details_by_age.get(age, [])
        detail_txt = " / ".join(f"{d.get('title', 'イベント')}: {float(d.get('amount', 0.0)):,.0f}円" for d in details)
        title_attr = _h(detail_txt) if detail_txt else ""
        expense_rows += (
            f"<tr><td class='num'>{age}歳</td><td class='num' title=\"{title_attr}\">{amount:,.0f}円</td></tr>"
        )
        expense_count += 1
        expense_total += amount
        if amount > expense_max_amount:
            expense_max_amount = amount
            expense_max_age = age
    if not expense_rows:
        expense_rows = "<tr><td colspan='2' style='color:#999'>該当期間のイベント支出なし</td></tr>"

    expense_summary = f"年{expense_count}件 / 合計 {expense_total:,.0f}円" + (
        f" / 最大 {expense_max_age}歳: {expense_max_amount:,.0f}円" if expense_max_age is not None else ""
    )

    life_events_html = f"""
    <div class="card full" data-card-id="life-events">
      <div class="card-header">
        <h2>ライフイベント管理</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div class="sim-param-grid">
          <div class="sim-param-section">
            <h3>イベント追加</h3>
            <div class="sim-field"><label>タイトル</label><input id="le-title" type="text" class="text-input" placeholder="例: 車買い替え"></div>
            <div class="sim-field"><label>金額（円）</label><input id="le-amount" type="text" class="money-input" value="2,000,000"></div>
            <div class="sim-input-row">
              <div class="sim-field" style="flex:1"><label>開始年</label><input id="le-start-year" type="number" class="stepper-input" min="2000" max="2200" value="{datetime.now().year + 2}"></div>
              <div class="sim-field" style="flex:1"><label>繰返し年数（0=単発）</label><input id="le-repeat" type="number" class="stepper-input" min="0" max="30" value="0"></div>
            </div>
            <div class="sim-field"><label>終了年（任意）</label><input id="le-end-year" type="number" class="stepper-input" min="2000" max="2200" value=""></div>
            <button class="btn" onclick="createLifeEvent()">イベント追加</button>
            <hr style="border:none;border-top:1px solid #eef2f7;margin:14px 0">
            <h3>住宅テンプレート</h3>
            <div class="sim-field"><label>購入年</label><input id="house-year" type="number" class="stepper-input" min="2000" max="2200" value="{datetime.now().year + 1}"></div>
            <div class="sim-field"><label>住宅価格（円）</label><input id="house-price" type="text" class="money-input" value="45,000,000"></div>
            <div class="sim-field"><label>頭金（円）</label><input id="house-down" type="text" class="money-input" value="5,000,000"></div>
            <div class="sim-input-row">
              <div class="sim-field" style="flex:1"><label>ローン年数</label><input id="house-loan-years" type="number" class="stepper-input" min="1" max="50" value="35"></div>
              <div class="sim-field" style="flex:1"><label>金利（年率 %）</label><input id="house-rate" type="number" class="stepper-input" min="0" max="10" step="0.01" value="1.2"></div>
            </div>
            <div class="sim-field"><label>維持費（年額円, 任意）</label><input id="house-maint" type="text" class="money-input" value="300,000"></div>
            <button class="btn" onclick="createHousingTemplate()">住宅イベントを自動作成</button>
          </div>
          <div class="sim-param-section">
            <h3>子ども登録（教育費自動反映）</h3>
            <div class="sim-field"><label>名前</label><input id="ch-name" type="text" class="text-input" placeholder="例: 長女"></div>
            <div class="sim-input-row">
              <div class="sim-field" style="flex:1"><label>生年</label><input id="ch-birth-year" type="number" class="stepper-input" min="1980" max="2100" value="{datetime.now().year - 5}"></div>
              <div class="sim-field" style="flex:1"><label>生月</label><input id="ch-birth-month" type="number" class="stepper-input" min="1" max="12" value="4"></div>
            </div>
            <button class="btn" onclick="createChildProfile()">子ども追加</button>
            <div class="sim-field" style="margin-top:16px">
              <label>物価上昇率（グローバル）</label>
              <div class="sim-input-row">
                <input id="life-inflation-rate" type="range" min="0" max="0.1" step="0.001" value="{life_inflation_rate}" oninput="document.getElementById('life-inflation-rate-val').textContent=(this.value*100).toFixed(1)">
                <span id="life-inflation-rate-val">{life_inflation_rate * 100:.1f}</span><span class="sim-unit">%</span>
                <button class="btn" onclick="saveLifeInflationRate()">保存</button>
              </div>
            </div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px">
          <div>
            <h3 style="font-size:0.9rem;margin-bottom:8px">イベント一覧</h3>
            <table class="pred-table"><tr><th>名称</th><th class="num">金額</th><th class="num">開始年</th><th>頻度</th><th class="num">終了年</th><th>操作</th></tr>{event_rows}</table>
          </div>
          <div>
            <h3 style="font-size:0.9rem;margin-bottom:8px">子ども一覧</h3>
            <table class="pred-table"><tr><th>名前</th><th class="num">生年月</th><th>教育費プラン</th><th>操作</th></tr>{child_rows}</table>
            <h3 style="font-size:0.9rem;margin:12px 0 8px">期間内イベント支出（年次）</h3>
            <div style="font-size:0.82rem;color:#636e72;margin-bottom:6px">{expense_summary}</div>
            <details>
              <summary style="cursor:pointer;color:#2881D7;font-size:0.82rem">詳細を表示</summary>
              <div style="margin-top:8px">
                <table class="pred-table"><tr><th class="num">年齢</th><th class="num">支出</th></tr>{expense_rows}</table>
              </div>
            </details>
          </div>
        </div>
      </div>
    </div>"""

    # --- 年次パーセンタイル表 ---
    projection_rows = ""
    _ra = int(params["retirement_age"])
    _rea = int(params["reemployment_end_age"])
    for yb in result.yearly_balances:
        age = yb["age"]
        # 退職年齢をハイライト、再雇用期間は淡い橙でハイライト
        if age == _ra:
            row_style = ' style="background:#eff8ff"'
        elif _ra < age <= _rea:
            row_style = ' style="background:#fff7ea"'
        else:
            row_style = ""
        projection_rows += f"""<tr{row_style}>
          <td class="num">{age}歳</td>
          <td class="num">{yb["p10"]:,.0f}</td>
          <td class="num">{yb["p25"]:,.0f}</td>
          <td class="num" style="font-weight:700">{yb["p50"]:,.0f}</td>
          <td class="num">{yb["p75"]:,.0f}</td>
          <td class="num">{yb["p90"]:,.0f}</td>
        </tr>"""

    balances_json = json.dumps(result.yearly_balances, ensure_ascii=False)
    balances_no_events_json = json.dumps(result_no_events.yearly_balances, ensure_ascii=False)
    chart_html = """
    <div class="card full sim-chart-card" data-card-id="sim-chart">
      <div class="card-header">
        <h2>資産推移グラフ</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="sim-chart-frame">
        <canvas id="sim-fan-chart" style="position:absolute;top:0;left:0;width:100%;height:100%"></canvas>
      </div>
      <div class="pred-note">※ 実質値（インフレ調整済み）。濃い帯=P25〜P75、薄い帯=P10〜P90、線=P50（中央値）</div>
      <div class="pred-note">※ 灰色破線はイベントなしのP50（基準線）</div>
      </div>
    </div>"""

    projection_table_html = f"""
    <div class="card full" data-card-id="sim-projection">
      <div class="card-header">
        <h2>年次データ</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div style="overflow-x:auto">
      <table class="pred-table">
        <tr><th>年齢</th><th class="num">悲観(P10)</th><th class="num">P25</th><th class="num" style="color:#2881D7">中央(P50)</th><th class="num">P75</th><th class="num">楽観(P90)</th></tr>
        {projection_rows}
      </table>
      </div>
      </div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%232881D7'/><path d='M50 5A45 45 0 0 1 95 50L50 50Z' fill='%23FCAD4C'/><path d='M50 5A45 45 0 0 0 10.2 72.5L50 50Z' fill='%230F7F30'/></svg>">
<title>ライフサイクル・シミュレーター</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f6fa; color: #2d3436; line-height: 1.6; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
  {_NAV_CSS}
  h1 {{ font-size: 1.5rem; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start; }}
  .sim-overview-stack {{
    position: sticky;
    top: 12px;
    z-index: 4;
    display: grid;
    gap: 12px;
    width: 100%;
    flex: 0 0 100%;
  }}
  .card {{
    background: #fff; border-radius: 12px; padding: 20px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
    width: calc(50% - 10px);
  }}
  .card.full {{ width: 100%; }}
  .card-header {{
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
  }}
  .card-header h2 {{
    font-size: 1rem; font-weight: 700; flex: 1; margin: 0;
  }}
  .card-body {{ }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; }}
  th, td {{ padding: 6px 8px; text-align: left; border-bottom: 1px solid #f1f2f6; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .pred-table th {{ background: #f8f9fa; font-weight: 600; position: sticky; top: 0; }}
  .pred-note {{ font-size: 0.75rem; color: #b2bec3; margin-top: 8px; }}
  {_COLLAPSE_CSS}

  /* シミュレーター固有 */
  .sim-field {{ margin-bottom: 12px; }}
  .sim-field label {{ display: block; font-size: 0.8rem; font-weight: 600; color: #636e72; margin-bottom: 4px; }}
  .sim-input-row {{ display: flex; align-items: center; gap: 8px; }}
  .sim-input-row input[type="range"] {{ flex: 1; }}
  .sim-input-row input[type="number"] {{ width: 140px; padding: 4px 8px; border: 1px solid #dfe6e9; border-radius: 4px; font-size: 0.9rem; }}
  .money-input {{ width: 140px; padding: 4px 8px; border: 1px solid #dfe6e9; border-radius: 4px; font-size: 0.9rem; text-align: right; }}
  .text-input {{ width: 100%; padding: 6px 8px; border: 1px solid #dfe6e9; border-radius: 4px; font-size: 0.9rem; }}
  .btn {{
    background: #2881D7; color: #fff; border: none; border-radius: 6px;
    padding: 8px 12px; font-size: 0.85rem; font-weight: 600; cursor: pointer;
  }}
  .btn:hover {{ background: #1a6bb5; }}
  .btn-danger-sm {{
    background: #fff; color: #c0392b; border: 1px solid #f3b4ae; border-radius: 6px;
    padding: 4px 8px; font-size: 0.75rem; cursor: pointer;
  }}
  .btn-danger-sm:hover {{ background: #fff5f4; }}
  .btn-edit-sm {{
    background: #fff; color: #1a6bb5; border: 1px solid #b9d7f6; border-radius: 6px;
    padding: 4px 8px; font-size: 0.75rem; cursor: pointer;
  }}
  .btn-edit-sm:hover {{ background: #f4f9ff; }}
  .plan-select {{
    padding: 2px 4px; border: 1px solid #dfe6e9; border-radius: 4px; font-size: 0.75rem;
    margin: 0 4px 4px 0; background: #fff;
  }}
  .sim-unit {{ font-size: 0.8rem; color: #636e72; min-width: 20px; }}
  .stepper-btn {{
    width: 32px; height: 32px; border: 1px solid #dfe6e9; border-radius: 4px;
    background: #f8f9fa; font-size: 1.1rem; font-weight: 700; cursor: pointer;
    display: flex; align-items: center; justify-content: center; color: #2d3436;
    flex-shrink: 0;
  }}
  .stepper-btn:hover {{ background: #e9ecef; }}
  .stepper-btn:active {{ background: #dfe6e9; }}
  .stepper-input {{
    width: 64px; text-align: center; padding: 4px 4px; border: 1px solid #dfe6e9;
    border-radius: 4px; font-size: 0.95rem; font-weight: 600;
    -moz-appearance: textfield;
  }}
  .stepper-input::-webkit-outer-spin-button,
  .stepper-input::-webkit-inner-spin-button {{ -webkit-appearance: none; margin: 0; }}
  .sim-info-btn {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 18px; height: 18px; border-radius: 50%; background: #2881D7; color: #fff;
    font-size: 0.7rem; font-weight: 700; font-style: italic; cursor: pointer;
    vertical-align: middle; margin-left: 4px; position: relative;
    border: 1.5px solid #2881D7;
  }}
  .sim-info-btn:hover, .sim-info-btn:focus {{ background: #1a6bb5; }}
  .sim-tooltip {{
    position: absolute; bottom: calc(100% + 6px); left: 50%; transform: translateX(-50%);
    background: #2d3436; color: #fff; font-size: 0.75rem; font-style: normal; font-weight: 400;
    padding: 6px 10px; border-radius: 6px; white-space: nowrap; z-index: 100;
    pointer-events: none;
  }}
  .sim-tooltip::after {{
    content: ''; position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
    border: 5px solid transparent; border-top-color: #2d3436;
  }}
  .sim-param-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }}
  .sim-param-section h3 {{ font-size: 0.85rem; font-weight: 700; color: #2881D7; margin-bottom: 8px; border-bottom: 2px solid #2881D7; padding-bottom: 4px; }}
  .sim-recalc {{ margin-top: 16px; text-align: center; }}
  .sim-recalc button {{
    background: #2881D7; color: #fff; border: none; border-radius: 6px;
    padding: 10px 32px; font-size: 0.95rem; font-weight: 600; cursor: pointer;
  }}
  .sim-recalc button:hover {{ background: #1a6bb5; }}
  .sim-recalc button:disabled {{ background: #b2bec3; cursor: not-allowed; }}
  .sim-reset-btn {{
    background: #fff !important; color: #636e72 !important; border: 1px solid #dfe6e9 !important;
    font-size: 0.8rem !important; padding: 8px 16px !important;
  }}
  .sim-reset-btn:hover {{ background: #f8f9fa !important; color: #2d3436 !important; }}
  .sim-summary-card {{
    padding: 14px 16px;
    background: linear-gradient(180deg, #ffffff 0%, #f8fbff 100%);
    border: 1px solid #e8f1fb;
  }}
  .sim-summary-card .card-header,
  .sim-chart-card .card-header {{
    margin-bottom: 10px;
  }}
  .sim-summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px; margin-bottom: 12px; }}
  .sim-summary-item {{
    text-align: left; padding: 10px 12px; background: rgba(248,249,250,0.92); border-radius: 10px;
    border: 1px solid #eef2f7;
  }}
  .sim-summary-label {{ font-size: 0.74rem; color: #636e72; margin-bottom: 4px; line-height: 1.4; }}
  .sim-summary-value {{ font-size: 1rem; font-weight: 700; line-height: 1.25; }}
  .sim-prob-grid {{
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px;
    padding-top: 12px; border-top: 1px solid #f1f2f6;
  }}
  .sim-prob-item {{ text-align: center; }}
  .sim-prob-label {{ display: block; font-size: 0.74rem; color: #636e72; margin-bottom: 2px; }}
  .sim-prob-value {{ font-size: 1.05rem; font-weight: 700; }}
  .sim-loading {{ display: none; margin-left: 8px; }}
  .sim-notes {{
    margin-top: 16px; padding: 0; background: #f8f9fa; border-radius: 8px;
    border-left: 3px solid #b2bec3;
  }}
  .sim-notes-header {{
    padding: 10px 16px; cursor: pointer; font-size: 0.8rem; font-weight: 700;
    color: #636e72; user-select: none;
  }}
  .sim-notes-header:hover {{ color: #2d3436; }}
  .sim-notes-icon {{
    display: inline-block; font-size: 0.65rem; transition: transform 0.2s;
    margin-right: 4px;
  }}
  .sim-notes.open .sim-notes-icon {{ transform: rotate(90deg); }}
  .sim-notes-body {{
    max-height: 0; overflow: hidden; transition: max-height 0.3s ease;
    padding: 0 16px;
  }}
  .sim-notes.open .sim-notes-body {{ max-height: 500px; padding: 0 16px 12px; }}
  .sim-notes ul {{ font-size: 0.75rem; color: #636e72; padding-left: 18px; margin: 0; }}
  .sim-notes li {{ margin-bottom: 3px; line-height: 1.5; }}
  .sim-notes strong {{ color: #2d3436; }}
  .sim-chart-card {{
    padding: 16px;
  }}
  .sim-chart-frame {{
    position: relative; width: 100%; min-height: 220px; max-height: 320px; height: clamp(220px, 30vw, 300px);
  }}

  @media (max-width: 700px) {{
    .sim-overview-stack {{ position: static; top: auto; }}
    .card {{ width: 100%; }}
    .sim-param-grid {{ grid-template-columns: 1fr; }}
    .sim-summary-grid {{ grid-template-columns: 1fr 1fr; }}
    .sim-chart-frame {{ min-height: 200px; height: 220px; }}
    .page-header {{ flex-direction: column; gap: 8px; align-items: flex-start; }}
    .nav-toolbar a {{ padding: 6px 10px; font-size: 0.78rem; }}
    h1 {{ font-size: 1.2rem; }}
    table {{ font-size: 0.8rem; }}
  }}
  @media (max-width: 520px) {{
    .sim-summary-grid {{ grid-template-columns: 1fr; }}
    .sim-prob-grid {{ grid-template-columns: 1fr; }}
  }}
</style>
</head>
<body>
<div class="container">
  <div class="page-header">
    <h1>ライフサイクル・シミュレーター</h1>
    {_nav_html("/simulator")}
  </div>
  <div class="grid">
    <div class="sim-overview-stack full">
      {chart_html}
      {summary_html}
    </div>
    <div class="card full" id="sim-params" data-card-id="sim-params">
      <div class="card-header">
        <h2>パラメータ設定</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="sim-param-grid">
{param_rows_html}
      </div>
      <div class="sim-recalc">
        <button id="sim-reset-btn" onclick="resetFromData()" class="sim-reset-btn">実データから再取得</button>
        <span class="sim-loading" id="sim-loading">計算中...</span>
      </div>
      </div>
    </div>
    {life_events_html}
    {projection_table_html}
  </div>
</div>
<script>
{_COLLAPSE_JS}

function fmtMoney(v) {{ return Math.round(v).toLocaleString('ja-JP'); }}
function parseMoney(s) {{ return parseFloat(String(s).replace(/,/g, '')) || 0; }}
function fmt(v) {{ return Math.round(v).toLocaleString('ja-JP'); }}
function fmtAxis(v) {{
  if (v >= 100_000_000) return (v / 100_000_000).toFixed(1) + '億';
  if (v >= 10_000) return Math.round(v / 10_000) + '万';
  return String(v);
}}

function stepVal(id, dir, min, max) {{
  const el = document.getElementById(id);
  let v = parseInt(el.value, 10) + dir;
  if (v < min) v = min;
  if (v > max) v = max;
  el.value = v;
  scheduleRecalc();
}}

// --- ツールチップ ---
document.querySelectorAll('.sim-info-btn').forEach(btn => {{
  function show() {{
    if (btn.querySelector('.sim-tooltip')) return;
    const tip = document.createElement('span');
    tip.className = 'sim-tooltip';
    tip.textContent = btn.dataset.tooltip;
    btn.appendChild(tip);
  }}
  function hide() {{ const t = btn.querySelector('.sim-tooltip'); if (t) t.remove(); }}
  btn.addEventListener('mouseenter', show);
  btn.addEventListener('mouseleave', hide);
  btn.addEventListener('focus', show);
  btn.addEventListener('blur', hide);
}});

// --- 金額入力フィールド ---
document.querySelectorAll('.money-input').forEach(el => {{
  el.addEventListener('blur', () => {{
    el.value = fmtMoney(parseMoney(el.value));
    scheduleRecalc();
  }});
  el.addEventListener('focus', () => {{
    el.value = String(parseMoney(el.value));
  }});
}});

// --- デバウンス付きリアルタイム再計算 ---
let _recalcTimer = null;
let _recalcInFlight = false;
function scheduleRecalc() {{
  if (_recalcTimer) clearTimeout(_recalcTimer);
  _recalcTimer = setTimeout(() => recalcSimulator(), 600);
}}

// スライダー・ステッパーの変更で自動再計算
document.querySelectorAll('#sim-params input[type="range"]').forEach(el => {{
  el.addEventListener('change', scheduleRecalc);
}});
document.querySelectorAll('#sim-params .stepper-input').forEach(el => {{
  el.addEventListener('change', scheduleRecalc);
}});

async function recalcSimulator() {{
  if (_recalcInFlight) return;
  _recalcInFlight = true;
  const loading = document.getElementById('sim-loading');
  loading.style.display = 'inline';

  const params = {{}};
  const fields = ['current_age','retirement_age','end_age','initial_investment','safe_value','monthly_contribution',
    'annual_return','annual_volatility','monthly_withdrawal','inflation_rate','expense_ratio',
    'pension_start_age','monthly_pension','other_monthly_income',
    'reemployment_end_age','reemployment_monthly_income'];
  fields.forEach(f => {{
    const el = document.getElementById(f);
    params[f] = el.classList.contains('money-input') ? parseMoney(el.value) : parseFloat(el.value);
  }});

  try {{
    const resp = await fetch('/api/simulator', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(params)
    }});
    const data = await resp.json();
    if (!data.ok) {{
      if (data.error) alert(data.error);
    }} else {{
      updateSummary(data);
      updateProjection(data.yearly_balances, params.retirement_age, params.reemployment_end_age);
      _initBalances = data.yearly_balances;
      _initBalancesNoEvents = data.yearly_balances_no_events || [];
      drawFanChart(data.yearly_balances, params.retirement_age, _initBalancesNoEvents, params.reemployment_end_age);
    }}
  }} catch(e) {{
    console.error('Simulator error:', e);
  }} finally {{
    _recalcInFlight = false;
    loading.style.display = 'none';
  }}
}}

function updateSummary(data) {{
  const summaryCard = document.querySelector('[data-card-id="sim-summary"] .card-body');
  if (!summaryCard) return;
  const grid = summaryCard.querySelector('.sim-summary-grid');
  if (grid) {{
    const vals = grid.querySelectorAll('.sim-summary-value');
    if (vals[0]) vals[0].textContent = fmt(data.total_principal) + '円';
    if (vals[1]) vals[1].textContent = fmt(data.total_gains) + '円';
    if (vals[2]) vals[2].textContent = fmt(data.total_tax) + '円';
    if (vals[3]) vals[3].textContent = fmt(data.net_final) + '円';
  }}
  const impact = document.getElementById('event-impact-val');
  if (impact && data.net_final_no_events != null) {{
    const diff = data.net_final - data.net_final_no_events;
    impact.textContent = (diff >= 0 ? '+' : '') + fmt(diff) + '円';
    impact.style.color = diff < 0 ? '#e74c3c' : '#0F7F30';
  }}
  const totalEvent = document.getElementById('event-total-val');
  if (totalEvent && data.total_event_expense != null) {{
    totalEvent.textContent = fmt(data.total_event_expense) + '円';
  }}
  const probs = summaryCard.querySelectorAll('.sim-prob-value');
  if (probs[0]) {{
    const dp = (data.depletion_probability * 100).toFixed(1);
    probs[0].textContent = dp + '%';
    probs[0].style.color = dp > 10 ? '#e74c3c' : dp > 0 ? '#f39c12' : '#27ae60';
  }}
  if (probs[1]) {{
    const lp = (data.principal_loss_probability * 100).toFixed(1);
    probs[1].textContent = lp + '%';
    probs[1].style.color = lp > 30 ? '#e74c3c' : lp > 10 ? '#f39c12' : '#27ae60';
  }}
}}

function updateProjection(balances, retirementAge, reemploymentEndAge) {{
  const table = document.querySelector('[data-card-id="sim-projection"] .pred-table');
  if (!table) return;
  const header = table.querySelector('tr');
  table.innerHTML = '';
  table.appendChild(header);
  const ra = Math.round(retirementAge);
  const rea = Math.round(reemploymentEndAge || retirementAge);
  balances.forEach(yb => {{
    const tr = document.createElement('tr');
    if (yb.age === ra) tr.style.background = '#eff8ff';
    else if (yb.age > ra && yb.age <= rea) tr.style.background = '#fff7ea';
    tr.innerHTML = '<td class="num">' + yb.age + '歳</td>'
      + '<td class="num">' + fmt(yb.p10) + '</td>'
      + '<td class="num">' + fmt(yb.p25) + '</td>'
      + '<td class="num" style="font-weight:700">' + fmt(yb.p50) + '</td>'
      + '<td class="num">' + fmt(yb.p75) + '</td>'
      + '<td class="num">' + fmt(yb.p90) + '</td>';
    table.appendChild(tr);
  }});
}}

// --- ファンチャート描画 ---
function drawFanChart(balances, retirementAge, baselineBalances = null, reemploymentEndAge = null) {{
  const canvas = document.getElementById('sim-fan-chart');
  if (!canvas || !balances || balances.length === 0) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const padL = 70, padR = 20, padT = 20, padB = 40;
  const cW = W - padL - padR, cH = H - padT - padB;
  const n = balances.length;

  // Y軸の最大値
  let yMax = 0;
  balances.forEach(b => {{ if (b.p90 > yMax) yMax = b.p90; }});
  yMax = yMax > 0 ? yMax * 1.1 : 10_000_000;

  function xPos(i) {{ return padL + (i / Math.max(n - 1, 1)) * cW; }}
  function yPos(v) {{ return padT + cH - (Math.max(v, 0) / yMax) * cH; }}

  // グリッド線と Y 軸ラベル
  ctx.strokeStyle = '#eee';
  ctx.lineWidth = 1;
  ctx.fillStyle = '#999';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  const gridSteps = 5;
  for (let i = 0; i <= gridSteps; i++) {{
    const val = (yMax / gridSteps) * i;
    const y = yPos(val);
    ctx.beginPath(); ctx.moveTo(padL, y); ctx.lineTo(padL + cW, y); ctx.stroke();
    ctx.fillText(fmtAxis(val), padL - 8, y + 4);
  }}

  // P10-P90 帯（薄い青）
  ctx.fillStyle = 'rgba(40,129,215,0.10)';
  ctx.beginPath();
  for (let i = 0; i < n; i++) {{ const x = xPos(i); ctx.lineTo(x, yPos(balances[i].p90)); }}
  for (let i = n - 1; i >= 0; i--) {{ const x = xPos(i); ctx.lineTo(x, yPos(balances[i].p10)); }}
  ctx.closePath(); ctx.fill();

  // P25-P75 帯（濃い青）
  ctx.fillStyle = 'rgba(40,129,215,0.22)';
  ctx.beginPath();
  for (let i = 0; i < n; i++) {{ const x = xPos(i); ctx.lineTo(x, yPos(balances[i].p75)); }}
  for (let i = n - 1; i >= 0; i--) {{ const x = xPos(i); ctx.lineTo(x, yPos(balances[i].p25)); }}
  ctx.closePath(); ctx.fill();

  // P50 線
  ctx.strokeStyle = '#2881D7';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  for (let i = 0; i < n; i++) {{ const x = xPos(i); i === 0 ? ctx.moveTo(x, yPos(balances[i].p50)) : ctx.lineTo(x, yPos(balances[i].p50)); }}
  ctx.stroke();

  // baseline（イベントなし）P50線
  if (baselineBalances && baselineBalances.length > 0) {{
    const n2 = Math.min(n, baselineBalances.length);
    ctx.strokeStyle = 'rgba(99,110,114,0.85)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    for (let i = 0; i < n2; i++) {{
      const x = xPos(i);
      i === 0 ? ctx.moveTo(x, yPos(baselineBalances[i].p50)) : ctx.lineTo(x, yPos(baselineBalances[i].p50));
    }}
    ctx.stroke();
    ctx.setLineDash([]);
  }}

  // 退職年齢の縦線
  let retLabelX = null, retLabelHalfW = 0;
  const retIdx = balances.findIndex(b => b.age === Math.round(retirementAge));
  if (retIdx >= 0) {{
    const rx = xPos(retIdx);
    ctx.strokeStyle = 'rgba(231,76,60,0.4)';
    ctx.lineWidth = 1.5;
    ctx.setLineDash([6, 4]);
    ctx.beginPath(); ctx.moveTo(rx, padT); ctx.lineTo(rx, padT + cH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#e74c3c';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('退職', rx, padT - 4);
    retLabelX = rx;
    retLabelHalfW = ctx.measureText('退職').width / 2;
  }}

  // 再雇用終了年齢の縦線（退職年齢より後に設定されている場合のみ）
  if (reemploymentEndAge && Math.round(reemploymentEndAge) > Math.round(retirementAge)) {{
    const reIdx = balances.findIndex(b => b.age === Math.round(reemploymentEndAge));
    if (reIdx >= 0) {{
      const rex = xPos(reIdx);
      ctx.strokeStyle = 'rgba(243,156,18,0.5)';
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(rex, padT); ctx.lineTo(rex, padT + cH); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = '#f39c12';
      ctx.font = '11px sans-serif';
      ctx.textAlign = 'center';
      const reLabelHalfW = ctx.measureText('再雇用終了').width / 2;
      // 退職ラベルと近接・重複する場合は1行下にずらして衝突を避ける
      const overlapsRetLabel = retLabelX !== null && Math.abs(rex - retLabelX) < (retLabelHalfW + reLabelHalfW + 4);
      ctx.fillText('再雇用終了', rex, overlapsRetLabel ? padT + 10 : padT - 4);
    }}
  }}

  // 0 線（枯渇ライン）
  const zeroY = yPos(0);
  ctx.strokeStyle = 'rgba(0,0,0,0.15)';
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, zeroY); ctx.lineTo(padL + cW, zeroY); ctx.stroke();

  // X 軸ラベル（年齢）
  ctx.fillStyle = '#999';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  const labelEvery = n > 30 ? 5 : n > 15 ? 3 : 2;
  balances.forEach((b, i) => {{
    if (i === 0 || i === n - 1 || b.age % labelEvery === 0) {{
      ctx.fillText(b.age + '歳', xPos(i), padT + cH + 20);
    }}
  }});

  // ホバーツールチップ用データを canvas に保持
  canvas._chartData = {{ balances, xPos, yPos, padL, padR, padT, padB, cW, cH, n }};
}}

// --- チャートホバーツールチップ ---
(function() {{
  const canvas = document.getElementById('sim-fan-chart');
  if (!canvas) return;
  const tip = document.createElement('div');
  tip.style.cssText = 'position:absolute;background:rgba(45,52,54,0.92);color:#fff;font-size:12px;'
    + 'padding:8px 12px;border-radius:6px;pointer-events:none;display:none;z-index:50;white-space:nowrap;line-height:1.6';
  canvas.parentElement.appendChild(tip);

  canvas.addEventListener('mousemove', e => {{
    const d = canvas._chartData;
    if (!d) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    // 最寄りのデータポイント
    let closest = 0, minDist = Infinity;
    for (let i = 0; i < d.n; i++) {{
      const dist = Math.abs(mx - d.xPos(i));
      if (dist < minDist) {{ minDist = dist; closest = i; }}
    }}
    if (minDist > 30) {{ tip.style.display = 'none'; return; }}
    const b = d.balances[closest];
    tip.innerHTML = '<strong>' + b.age + '歳</strong><br>'
      + 'P90: ' + fmt(b.p90) + '円<br>'
      + 'P75: ' + fmt(b.p75) + '円<br>'
      + '<span style="color:#5dade2">P50: ' + fmt(b.p50) + '円</span><br>'
      + 'P25: ' + fmt(b.p25) + '円<br>'
      + 'P10: ' + fmt(b.p10) + '円';
    tip.style.display = 'block';
    const tx = d.xPos(closest);
    tip.style.left = (tx + 15) + 'px';
    tip.style.top = '20px';
    if (tx + 15 + tip.offsetWidth > d.cW + d.padL) {{
      tip.style.left = (tx - tip.offsetWidth - 15) + 'px';
    }}
  }});
  canvas.addEventListener('mouseleave', () => {{ tip.style.display = 'none'; }});
}})();

// --- 初期チャート描画 ---
let _initBalances = {balances_json};
let _initBalancesNoEvents = {balances_no_events_json};
drawFanChart(_initBalances, {int(params["retirement_age"])}, _initBalancesNoEvents, {int(params["reemployment_end_age"])});
window.addEventListener('resize', () => {{
  if (_initBalances) drawFanChart(_initBalances, parseInt(document.getElementById('retirement_age').value) || 65, _initBalancesNoEvents,
    parseInt(document.getElementById('reemployment_end_age').value) || null);
}});

// --- 実データから再取得 ---
async function resetFromData() {{
  if (!confirm('リスク資産額・安全資産額・月額積立を実データから再取得します。\\n他のパラメータはそのまま維持されます。')) return;
  const btn = document.getElementById('sim-reset-btn');
  const loading = document.getElementById('sim-loading');
  btn.disabled = true;
  loading.style.display = 'inline';
  try {{
    const resp = await fetch('/api/simulator/reset', {{ method: 'POST' }});
    const data = await resp.json();
    if (data.ok) {{
      const ii = document.getElementById('initial_investment');
      const sv = document.getElementById('safe_value');
      const mc = document.getElementById('monthly_contribution');
      if (ii) ii.value = fmtMoney(data.initial_investment);
      if (sv) sv.value = fmtMoney(data.safe_value);
      if (mc) mc.value = fmtMoney(data.monthly_contribution);
      await recalcSimulator();
    }}
  }} catch(e) {{
    console.error('Reset error:', e);
  }} finally {{
    btn.disabled = false;
    loading.style.display = 'none';
  }}
}}

async function createLifeEvent() {{
  const payload = {{
    title: (document.getElementById('le-title')?.value || '').trim(),
    amount: parseMoney(document.getElementById('le-amount')?.value || '0'),
    start_year: parseInt(document.getElementById('le-start-year')?.value || '0', 10),
    repeat_every_years: parseInt(document.getElementById('le-repeat')?.value || '0', 10),
    end_year: parseInt(document.getElementById('le-end-year')?.value || '0', 10) || null
  }};
  if (!payload.title || payload.amount <= 0 || !payload.start_year) {{
    alert('タイトル・金額・開始年を入力してください');
    return;
  }}
  try {{
    const resp = await fetch('/api/life-events', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '保存に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function createHousingTemplate() {{
  const payload = {{
    purchase_year: parseInt(document.getElementById('house-year')?.value || '0', 10),
    price: parseMoney(document.getElementById('house-price')?.value || '0'),
    down_payment: parseMoney(document.getElementById('house-down')?.value || '0'),
    loan_years: parseInt(document.getElementById('house-loan-years')?.value || '0', 10),
    annual_interest_rate: parseFloat(document.getElementById('house-rate')?.value || '0'),
    annual_maintenance: parseMoney(document.getElementById('house-maint')?.value || '0'),
  }};
  if (!payload.purchase_year || payload.price <= 0 || payload.loan_years <= 0) {{
    alert('購入年・住宅価格・ローン年数を入力してください');
    return;
  }}
  try {{
    const resp = await fetch('/api/life-events/housing-template', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '住宅テンプレート作成に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function editLifeEvent(btn) {{
  const id = parseInt(btn.dataset.id || '0', 10);
  const title = prompt('イベント名', btn.dataset.title || '');
  if (title === null) return;
  const amountRaw = prompt('金額（円）', fmt(parseFloat(btn.dataset.amount || '0')));
  if (amountRaw === null) return;
  const startYearRaw = prompt('開始年', btn.dataset.startYear || '');
  if (startYearRaw === null) return;
  const repeatRaw = prompt('繰返し年数（0=単発）', btn.dataset.repeat || '0');
  if (repeatRaw === null) return;
  const endYearRaw = prompt('終了年（空欄でなし）', btn.dataset.endYear || '');
  if (endYearRaw === null) return;
  const payload = {{
    id,
    title: title.trim(),
    amount: parseMoney(amountRaw),
    start_year: parseInt(startYearRaw, 10),
    repeat_every_years: parseInt(repeatRaw, 10) || 0,
    end_year: endYearRaw.trim() ? parseInt(endYearRaw, 10) : null,
  }};
  if (!payload.title || payload.amount <= 0 || !payload.start_year) {{
    alert('入力値が不正です');
    return;
  }}
  try {{
    const resp = await fetch('/api/life-events/update', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '更新に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function deleteLifeEvent(id) {{
  if (!confirm('このイベントを削除しますか？')) return;
  try {{
    const resp = await fetch('/api/life-events/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ id }})
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '削除に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function createChildProfile() {{
  const payload = {{
    name: (document.getElementById('ch-name')?.value || '').trim(),
    birth_year: parseInt(document.getElementById('ch-birth-year')?.value || '0', 10),
    birth_month: parseInt(document.getElementById('ch-birth-month')?.value || '0', 10)
  }};
  if (!payload.name || !payload.birth_year || payload.birth_month < 1 || payload.birth_month > 12) {{
    alert('名前・生年・生月を正しく入力してください');
    return;
  }}
  try {{
    const resp = await fetch('/api/children', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '保存に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function editChildProfile(btn) {{
  const id = parseInt(btn.dataset.id || '0', 10);
  const name = prompt('子どもの名前', btn.dataset.name || '');
  if (name === null) return;
  const birthYearRaw = prompt('生年', btn.dataset.birthYear || '');
  if (birthYearRaw === null) return;
  const birthMonthRaw = prompt('生月', btn.dataset.birthMonth || '');
  if (birthMonthRaw === null) return;

  const payload = {{
    id,
    name: name.trim(),
    birth_year: parseInt(birthYearRaw, 10),
    birth_month: parseInt(birthMonthRaw, 10),
  }};
  if (!payload.name || !payload.birth_year || payload.birth_month < 1 || payload.birth_month > 12) {{
    alert('入力値が不正です');
    return;
  }}
  try {{
    const resp = await fetch('/api/children/update', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(payload)
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '更新に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function deleteChildProfile(id) {{
  if (!confirm('この子どもプロフィールを削除しますか？')) return;
  try {{
    const resp = await fetch('/api/children/delete', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ id }})
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '削除に失敗しました');
      return;
    }}
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function saveChildPlan(childId, stage, value) {{
  try {{
    const resp = await fetch('/api/children/update-plan', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ id: childId, stage, value }})
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '教育費プランの保存に失敗しました');
      return;
    }}
    await recalcSimulator();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}

async function saveLifeInflationRate() {{
  const rate = parseFloat(document.getElementById('life-inflation-rate')?.value || '0');
  try {{
    const resp = await fetch('/api/life-settings', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ inflation_rate: rate }})
    }});
    const data = await resp.json();
    if (!data.ok) {{
      alert(data.error || '保存に失敗しました');
      return;
    }}
    await recalcSimulator();
    location.reload();
  }} catch (e) {{
    alert('通信エラーが発生しました');
    console.error(e);
  }}
}}
</script>
</body>
</html>"""


def _build_plan_html(data: dict, skip_update: bool = False, ai_comment: str | None = None) -> str:
    """ライフプランニングページの HTML を生成する。"""
    if not data:
        return "<html><body><h1>データがありません</h1><p><a href='/'>ダッシュボードに戻る</a></p></body></html>"

    date = data["date"]
    total_asset = data["total_asset"]
    cashflows = data.get("cashflows", [])
    predictions = data.get("predictions", [])
    pred_params = data.get("pred_params", {})
    predictions_c = data.get("predictions_contrib", [])
    data.get("pred_params_contrib", {})  # reserved for future use
    monthly_contribution = data.get("monthly_contribution", 50000)
    daily_assets = data.get("daily_assets", [])
    dividend_history = data.get("dividend_history", {})

    # --- 家計簿実績バナー ---
    cf_savings = data.get("cf_savings")
    cf_savings_html = ""
    if cf_savings:
        s = cf_savings
        savings_sign = "+" if s["avg_savings"] >= 0 else ""
        savings_color = "color:#e74c3c" if s["avg_savings"] >= 0 else "color:#2881D7"
        cf_savings_html = f"""
    <div class="card full" style="background:linear-gradient(135deg,#f0faf4 0%,#f5f6fa 100%);border:1px solid #b8e6c8">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span style="background:#0F7F30;color:#fff;font-size:0.7rem;font-weight:700;padding:2px 8px;border-radius:4px">家計簿実績</span>
        <span style="font-size:0.8rem;color:#636e72">直近{s["months_used"]}ヶ月平均</span>
      </div>
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <div><span style="font-size:0.8rem;color:#636e72">収入</span><div style="font-size:1.1rem;font-weight:700">{s["avg_income"] / 10000:.1f}万円</div></div>
        <div><span style="font-size:0.8rem;color:#636e72">支出</span><div style="font-size:1.1rem;font-weight:700">{s["avg_expense"] / 10000:.1f}万円</div></div>
        <div><span style="font-size:0.8rem;color:#636e72">貯蓄</span><div style="font-size:1.1rem;font-weight:700;{savings_color}">{savings_sign}{s["avg_savings"] / 10000:.1f}万円</div></div>
        <div><span style="font-size:0.8rem;color:#636e72">貯蓄率</span><div style="font-size:1.1rem;font-weight:700">{s["savings_rate"]}%</div></div>
      </div>
      <div style="font-size:0.75rem;color:#b2bec3;margin-top:8px">※ 積立額設定の参考値としてご活用ください（自動変更はしません）</div>
    </div>"""

    # --- セクション0: 日次資産推移（実績） ---
    daily_chart_data = json.dumps(daily_assets, ensure_ascii=False)
    daily_card_html = ""
    if daily_assets:
        daily_card_html = """
    <div class="card full" data-card-id="plan-daily-assets">
      <div class="card-header">
        <h2>資産推移（実績）</h2>
        <div class="period-buttons">
          <button class="period-btn active" data-period="1">1M</button>
          <button class="period-btn" data-period="3">3M</button>
          <button class="period-btn" data-period="6">6M</button>
          <button class="period-btn" data-period="12">1Y</button>
          <button class="period-btn" data-period="0">ALL</button>
        </div>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <canvas id="daily-chart" height="280"></canvas>
      </div>
    </div>"""

    # --- セクション2: 月次収支（MF集計、参考） ---
    cf_chart_data = json.dumps(cashflows, ensure_ascii=False)
    mc_int = int(monthly_contribution)

    cf_rows = ""
    for cf in cashflows:
        living = cf["expense"] - mc_int
        net = cf["income"] - cf["expense"]
        sign = "+" if net >= 0 else ""
        css = "plus" if net >= 0 else "minus"
        cf_rows += f"""<tr>
          <td>{cf["year_month"]}</td>
          <td class="num">{cf["income"]:,.0f}円</td>
          <td class="num">{cf["expense"]:,.0f}円</td>
          <td class="num">{living:,.0f}円</td>
          <td class="num {css}">{sign}{net:,.0f}円</td>
        </tr>"""

    # --- セクション2.5: 配当実績 ---
    div_monthly = dividend_history.get("monthly", [])
    div_annual = dividend_history.get("annual", [])
    div_html = ""
    if div_monthly:
        div_chart_data = json.dumps(div_monthly, ensure_ascii=False)
        div_annual_rows = ""
        for a in div_annual:
            div_annual_rows += f'<tr><td>{a["year"]}年</td><td class="num">{a["amount"]:,.0f}円</td></tr>'
        div_total = sum(a["amount"] for a in div_annual)
        div_html = f"""
    <div class="card" data-card-id="plan-dividends">
      <div class="card-header">
        <h2>配当・分配金実績</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <canvas id="div-chart" height="200"></canvas>
      <table style="margin-top:12px">
        <tr><th>年</th><th class="num">受取額</th></tr>
        {div_annual_rows}
        <tr style="border-top:2px solid #dfe6e9;font-weight:700"><td>合計</td><td class="num">{div_total:,.0f}円</td></tr>
      </table>
      <div class="pred-note" style="margin-top:8px">※ 家計簿の「配当」「分配金」「利息」カテゴリから集計</div>
      </div>
    </div>"""
    else:
        div_chart_data = "[]"

    # --- セクション3: 成長予測（追加投資なし） ---
    pred_html = ""
    if predictions:
        pred_rows = ""
        for p in predictions:
            pred_rows += f'<tr><td class="num">{p.years}年後</td>'
            pred_rows += f'<td class="num">{p.p10:,.0f}円</td>'
            pred_rows += f'<td class="num" style="font-weight:700">{p.p50:,.0f}円</td>'
            pred_rows += f'<td class="num">{p.p90:,.0f}円</td></tr>'
        is_est = pred_params.get("is_estimated", True)
        note = (
            "※ デフォルトパラメータ使用（データ蓄積中）"
            if is_est
            else f"※ {pred_params.get('data_points', 0)}日分のデータから推定"
        )
        annual_ret = pred_params.get("annual_return", 0) * 100
        annual_vol = pred_params.get("annual_volatility", 0) * 100
        p_risk = pred_params.get("risk_value", 0)
        p_safe = pred_params.get("safe_value", 0)
        pred_html = f"""
    <div class="card" data-card-id="plan-pred">
      <div class="card-header">
        <h2>成長予測（追加投資なし）</h2>
        <button class="info-btn" onclick="document.getElementById('pred-info').classList.toggle('show')" title="予測手法について">?</button>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="info-panel" id="pred-info">
        <strong>モンテカルロ・シミュレーションとは</strong>
        <p>現在の資産を出発点に、将来の資産額を確率的にシミュレーションする手法です。</p>
        <ul>
          <li><strong>対象資産の分離:</strong> リスク資産（株式・投信: <strong>{p_risk:,.0f}円</strong>）のみ市場変動の対象とし、安全資産（預金・不動産・年金: <strong>{p_safe:,.0f}円</strong>）は変動なしで固定加算</li>
          <li><strong>手法:</strong> 幾何ブラウン運動（対数正規モデル）でリスク資産の月次リターンを生成し、2,000回のシミュレーションを実行</li>
          <li><strong>パラメータ:</strong> 過去の日次リターンから年率の期待リターンとボラティリティ（価格変動の大きさ）を推定。データが60日未満の場合は資産クラス別デフォルト値の加重平均を使用</li>
          <li><strong>P10（悲観）:</strong> シミュレーション結果の下位10% — 10回中9回はこれ以上になる水準</li>
          <li><strong>P50（中央）:</strong> シミュレーション結果の中央値 — 最も起こりやすい水準</li>
          <li><strong>P90（楽観）:</strong> シミュレーション結果の上位10% — 好調時に期待できる水準</li>
        </ul>
        <p>この予測は過去のデータに基づく参考値であり、将来のリターンを保証するものではありません。</p>
      </div>
      <table class="pred-table">
        <tr><th></th><th class="num">悲観(P10)</th><th class="num">中央(P50)</th><th class="num">楽観(P90)</th></tr>
        {pred_rows}
      </table>
      <div class="pred-note">{note}<br>リスク資産 {p_risk:,.0f}円 + 安全資産 {p_safe:,.0f}円（固定）<br>期待リターン {annual_ret:.1f}%/年　ボラティリティ {annual_vol:.1f}%/年</div>
      </div>
    </div>"""

    # --- セクション4: 成長予測（積立込み） ---
    pred_contrib_html = ""
    if predictions_c:
        pred_c_rows = ""
        for p in predictions_c:
            pred_c_rows += f'<tr><td class="num">{p.years}年後</td>'
            pred_c_rows += f'<td class="num">{p.p10:,.0f}円</td>'
            pred_c_rows += f'<td class="num" style="font-weight:700">{p.p50:,.0f}円</td>'
            pred_c_rows += f'<td class="num">{p.p90:,.0f}円</td></tr>'
        mc_int = int(monthly_contribution)
        pred_contrib_html = f'''
    <div class="card" data-card-id="plan-pred-c">
      <div class="card-header">
        <h2>成長予測（積立込み）</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      <div class="contrib-form">
        <label>月額積立:</label>
        <input type="number" id="contrib-input" value="{mc_int}" step="10000" min="0">
        <span>円/月</span>
        <button onclick="updateContrib()">再計算</button>
      </div>
      <table class="pred-table">
        <tr><th></th><th class="num">悲観(P10)</th><th class="num">中央(P50)</th><th class="num">楽観(P90)</th></tr>
        {pred_c_rows}
      </table>
      <div class="pred-note">※ 上記の予測パラメータ + 毎月 {mc_int:,}円 の積立を加算</div>
      </div>
    </div>'''

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%232881D7'/><path d='M50 5A45 45 0 0 1 95 50L50 50Z' fill='%23FCAD4C'/><path d='M50 5A45 45 0 0 0 10.2 72.5L50 50Z' fill='%230F7F30'/></svg>">
<title>ライフプランニング - {date}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f6fa; color: #2d3436; line-height: 1.6; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
  {_NAV_CSS}
  h1 {{ font-size: 1.5rem; }}
  .total {{ font-size: 1.4rem; font-weight: 700; color: #636e72; margin-bottom: 24px; }}
  .total strong {{ color: #2d3436; font-size: 1.8rem; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 20px; align-items: flex-start; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); width: calc(50% - 10px); }}
  .card h2 {{ font-size: 1.1rem; margin-bottom: 12px; color: #2d3436; }}
  .full {{ width: 100%; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid #dfe6e9; color: #636e72; font-weight: 600; }}
  td {{ padding: 6px; border-bottom: 1px solid #f1f2f6; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .plus {{ color: #e74c3c; }}
  .minus {{ color: #2881D7; }}
  .no-data {{ color: #b2bec3; font-size: 0.9rem; padding: 20px 0; }}
  .no-data code {{ background: #f1f2f6; padding: 2px 6px; border-radius: 4px; font-size: 0.85rem; }}
  .avg-net {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }}
  .avg-label {{ font-size: 0.85rem; color: #636e72; margin-bottom: 12px; }}
  canvas {{ width: 100%; max-height: 250px; }}
  .pred-table {{ margin-top: 12px; }}
  .pred-table th {{ font-size: 0.8rem; }}
  .pred-note {{ font-size: 0.75rem; color: #b2bec3; margin-top: 8px; }}
  .card-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }}
  .card-header h2 {{ margin-bottom: 0; }}
  .info-btn {{
    width: 22px; height: 22px; border-radius: 50%; border: 1.5px solid #b2bec3;
    background: transparent; color: #636e72; font-size: 0.75rem; font-weight: 700;
    cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }}
  .info-btn:hover {{ background: #f1f2f6; border-color: #636e72; }}
  .info-panel {{
    display: none; background: #f8f9fa; border-radius: 8px; padding: 14px 16px;
    font-size: 0.8rem; color: #636e72; line-height: 1.7; margin-bottom: 12px;
    border: 1px solid #dfe6e9;
  }}
  .info-panel.show {{ display: block; }}
  .info-panel strong {{ color: #2d3436; }}
  .info-panel ul {{ margin: 6px 0 6px 18px; }}
  .info-panel li {{ margin-bottom: 2px; }}
  .contrib-form {{
    display: flex; align-items: center; gap: 8px; margin-bottom: 12px;
    font-size: 0.85rem; color: #636e72;
  }}
  .contrib-form input {{
    width: 120px; padding: 4px 8px; border: 1px solid #dfe6e9;
    border-radius: 6px; font-size: 0.9rem; text-align: right;
  }}
  .contrib-form button {{
    padding: 4px 12px; border: 1px solid #dfe6e9; border-radius: 6px;
    background: #fff; cursor: pointer; font-size: 0.85rem; color: #2d3436;
  }}
  .contrib-form button:hover {{ background: #f1f2f6; }}
  .period-buttons {{ display: flex; gap: 4px; margin-left: auto; }}
  .period-btn {{
    padding: 2px 10px; border: 1px solid #dfe6e9; border-radius: 4px;
    background: #fff; cursor: pointer; font-size: 0.75rem; color: #636e72; font-weight: 600;
  }}
  .period-btn:hover {{ background: #f1f2f6; }}
  .period-btn.active {{ background: #2881D7; color: #fff; border-color: #2881D7; }}
  .daily-tooltip {{
    position: fixed; pointer-events: none; background: rgba(45,52,54,0.92);
    color: #fff; padding: 10px 14px; border-radius: 8px; font-size: 0.8rem;
    line-height: 1.6; opacity: 0; transition: opacity 0.15s; z-index: 999;
    max-width: 280px;
  }}
  .daily-tooltip.show {{ opacity: 1; }}
  .ai-comment-card {{
    background: linear-gradient(135deg, #f8f9ff 0%, #f0f4ff 100%);
    border: 1px solid #d4ddee; border-radius: 12px;
    padding: 16px 20px; margin-bottom: 20px;
    display: flex; align-items: flex-start; gap: 12px;
    font-size: 0.9rem; line-height: 1.7;
  }}
  .ai-icon {{
    background: #2881D7; color: #fff; font-size: 0.7rem; font-weight: 700;
    padding: 3px 6px; border-radius: 4px; flex-shrink: 0; margin-top: 2px;
  }}
  #reload-banner {{
    display: none; position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
    background: #0F7F30; color: #fff; padding: 10px 20px;
    align-items: center; justify-content: center; gap: 12px;
    font-size: 0.9rem; font-weight: 600;
  }}
  #reload-banner button {{
    background: #fff; color: #0F7F30; border: none; border-radius: 6px;
    padding: 4px 14px; font-size: 0.85rem; font-weight: 600; cursor: pointer;
  }}
  #reload-banner button:hover {{ background: #f1f2f6; }}
  {_COLLAPSE_CSS}
  {_RESPONSIVE_CSS}
</style>
</head>
<body>
<div id="reload-banner">
  データが更新されました
  <button onclick="location.reload()">再読み込み</button>
</div>
<div class="container">
  <div class="page-header">
    <h1>ライフプランニング</h1>
    {_nav_html("/plan")}
  </div>
  <div class="total">現在の総資産: <strong>{total_asset:,.0f}</strong> 円 <span style="font-size:0.85rem;color:#b2bec3">({
        date
    }時点)</span></div>
  {
        f'<div class="ai-comment-card"><div class="ai-icon">AI</div><div class="ai-text">{ai_comment}</div></div>'
        if ai_comment
        else ""
    }

  <div class="grid">
    {daily_card_html}

    {cf_savings_html}

    <div class="card full" data-card-id="plan-cashflow">
      <div class="card-header">
        <h2>月次収支</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
      {'<canvas id="cf-chart" height="200"></canvas>' if cashflows else ""}
      {
        f'''<table style="margin-top:16px">
        <tr><th>月</th><th class="num">収入</th><th class="num">支出</th><th class="num">生活費</th><th class="num">収支</th></tr>
        {cf_rows}
      </table>
      <div class="pred-note" style="margin-top:8px">※ 支出には積立投資・貯蓄性の振替を含みます。生活費 = 支出 - 月額積立({mc_int:,}円)</div>'''
        if cashflows
        else '<div class="no-data">月次収支データがありません。<code>python -m src.daily</code> を実行すると取得されます。</div>'
    }
      </div>
    </div>

    {div_html}

    {pred_html}

    {pred_contrib_html}
  </div>
</div>
<div class="daily-tooltip" id="daily-tooltip"></div>

<script>
{_ESC_JS}
// --- 日次資産推移（実績）チャート ---
const dailyAllData = {daily_chart_data};
const dailyCanvas = document.getElementById('daily-chart');
const AREA_COLORS = ['#2881D7','#FCAD4C','#0F7F30','#008986','#9C39B6','#DF3727','#80BD45','#E67E22'];

function drawDailyChart(data) {{
  if (!data.length || !dailyCanvas) return;
  const ctx = dailyCanvas.getContext('2d');
  const W = dailyCanvas.parentElement.clientWidth - 40;
  dailyCanvas.width = W;
  dailyCanvas.height = 280;
  ctx.clearRect(0, 0, W, 280);

  const padding = {{ left: 80, right: 20, top: 20, bottom: 30 }};
  const chartW = W - padding.left - padding.right;
  const chartH = 280 - padding.top - padding.bottom;

  // 資産クラスキー収集
  const classKeys = [];
  const classSet = new Set();
  data.forEach(d => {{ Object.keys(d.by_class || {{}}).forEach(k => {{ if (!classSet.has(k)) {{ classSet.add(k); classKeys.push(k); }} }}); }});

  const totals = data.map(d => d.total);
  const hasClasses = classKeys.length > 0;
  const minVal = hasClasses ? 0 : Math.min(...totals) * 0.95;
  const maxVal = Math.max(...totals) * 1.05;
  const range = maxVal - minVal || 1;

  // Y軸グリッド
  ctx.strokeStyle = '#f1f2f6';
  ctx.fillStyle = '#b2bec3';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {{
    const y = padding.top + chartH * (1 - i/4);
    const val = minVal + range * i / 4;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(W - padding.right, y);
    ctx.stroke();
    ctx.fillText((val/10000).toFixed(0) + '万', padding.left - 6, y + 4);
  }}

  // 積み上げ面グラフ（資産クラス別）
  if (classKeys.length > 0) {{
    // 各クラスの累積値を計算
    const stacked = data.map(d => {{
      let cum = 0;
      const vals = {{}};
      classKeys.forEach(k => {{
        cum += (d.by_class || {{}})[k] || 0;
        vals[k] = cum;
      }});
      return vals;
    }});

    for (let ci = classKeys.length - 1; ci >= 0; ci--) {{
      const color = AREA_COLORS[ci % AREA_COLORS.length];
      ctx.fillStyle = color + '99';
      ctx.beginPath();
      data.forEach((d, i) => {{
        const x = padding.left + (chartW / (data.length - 1 || 1)) * i;
        const val = stacked[i][classKeys[ci]];
        const y = padding.top + chartH * (1 - (val - minVal) / range);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      }});
      // 下辺: 一つ下のクラスの上辺、または 0
      for (let i = data.length - 1; i >= 0; i--) {{
        const x = padding.left + (chartW / (data.length - 1 || 1)) * i;
        const val = ci > 0 ? stacked[i][classKeys[ci - 1]] : 0;
        const y = padding.top + chartH * (1 - (val - minVal) / range);
        ctx.lineTo(x, y);
      }}
      ctx.closePath();
      ctx.fill();
    }}
  }}

  // 総資産ライン
  ctx.strokeStyle = '#2881D7';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  data.forEach((d, i) => {{
    const x = padding.left + (chartW / (data.length - 1 || 1)) * i;
    const y = padding.top + chartH * (1 - (d.total - minVal) / range);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();

  // X軸ラベル（7日おきに間引き）
  const step = Math.max(1, Math.floor(data.length / 8));
  ctx.fillStyle = '#636e72';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'center';
  data.forEach((d, i) => {{
    if (i % step === 0 || i === data.length - 1) {{
      const x = padding.left + (chartW / (data.length - 1 || 1)) * i;
      ctx.fillText(d.date.substring(5), x, padding.top + chartH + 18);
    }}
  }});

  // 凡例
  if (classKeys.length > 0) {{
    let lx = padding.left;
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'left';
    classKeys.forEach((k, ci) => {{
      const color = AREA_COLORS[ci % AREA_COLORS.length];
      ctx.fillStyle = color + '80';
      ctx.fillRect(lx, 4, 10, 10);
      ctx.fillStyle = '#2d3436';
      ctx.fillText(k, lx + 13, 13);
      lx += ctx.measureText(k).width + 22;
    }});
  }}

  // ツールチップ
  dailyCanvas.onmousemove = function(e) {{
    const rect = dailyCanvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const idx = Math.round((mx - padding.left) / (chartW / (data.length - 1 || 1)));
    if (idx < 0 || idx >= data.length) {{ dailyTooltip.classList.remove('show'); return; }}
    const d = data[idx];
    let html = '<strong>' + esc(d.date) + '</strong><br>総資産: ' + (d.total / 10000).toLocaleString('ja-JP', {{maximumFractionDigits:0}}) + '万円';
    if (d.by_class) {{
      html += '<div style="margin-top:4px;border-top:1px solid rgba(255,255,255,0.2);padding-top:4px">';
      Object.entries(d.by_class).forEach(([k, v]) => {{
        html += '<div style="display:flex;justify-content:space-between;gap:12px"><span>' + esc(k) + '</span><span>' + (v/10000).toLocaleString('ja-JP', {{maximumFractionDigits:0}}) + '万</span></div>';
      }});
      html += '</div>';
    }}
    const tip = document.getElementById('daily-tooltip');
    if (tip) {{
      tip.innerHTML = html;
      tip.classList.add('show');
      tip.style.left = Math.min(e.clientX + 14, window.innerWidth - 260) + 'px';
      tip.style.top = (e.clientY - 10) + 'px';
    }}
  }};
  dailyCanvas.onmouseleave = function() {{
    const tip = document.getElementById('daily-tooltip');
    if (tip) tip.classList.remove('show');
  }};
}}

// 期間切替
const periodBtns = document.querySelectorAll('.period-btn');
function filterByPeriod(months) {{
  if (months === 0 || !months) {{ drawDailyChart(dailyAllData); return; }}
  const cutoff = new Date();
  cutoff.setMonth(cutoff.getMonth() - months);
  const cutoffStr = cutoff.toISOString().substring(0, 10);
  drawDailyChart(dailyAllData.filter(d => d.date >= cutoffStr));
}}
periodBtns.forEach(btn => {{
  btn.addEventListener('click', function() {{
    periodBtns.forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    filterByPeriod(parseInt(this.dataset.period));
  }});
}});
// 初期描画（デフォルト1M）
filterByPeriod(1);

// 月次収支棒グラフ描画
const cfData = {cf_chart_data};
const cfCanvas = document.getElementById('cf-chart');
if (cfData.length > 0 && cfCanvas) {{
  const ctx = cfCanvas.getContext('2d');
  const W = cfCanvas.parentElement.clientWidth - 40;
  cfCanvas.width = W;
  cfCanvas.height = 220;

  const labels = cfData.map(d => d.year_month.substring(5));
  const incomes = cfData.map(d => d.income);
  const expenses = cfData.map(d => d.expense);
  const maxVal = Math.max(...incomes, ...expenses) * 1.15;

  const padding = {{ left: 70, right: 20, top: 20, bottom: 30 }};
  const chartW = W - padding.left - padding.right;
  const chartH = 220 - padding.top - padding.bottom;
  const barGroupW = chartW / cfData.length;
  const barW = barGroupW * 0.3;

  // Y軸グリッド
  ctx.strokeStyle = '#f1f2f6';
  ctx.fillStyle = '#b2bec3';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {{
    const y = padding.top + chartH * (1 - i/4);
    const val = maxVal * i / 4;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(W - padding.right, y);
    ctx.stroke();
    ctx.fillText((val/10000).toFixed(0) + '万', padding.left - 6, y + 4);
  }}

  // 棒グラフ
  cfData.forEach((d, i) => {{
    const x = padding.left + i * barGroupW + barGroupW * 0.1;
    const iH = (d.income / maxVal) * chartH;
    const eH = (d.expense / maxVal) * chartH;

    // 収入（赤系）
    ctx.fillStyle = '#e74c3c';
    ctx.fillRect(x, padding.top + chartH - iH, barW, iH);

    // 支出（青系）
    ctx.fillStyle = '#2881D7';
    ctx.fillRect(x + barW + 2, padding.top + chartH - eH, barW, eH);

    // ラベル
    ctx.fillStyle = '#636e72';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(labels[i], x + barW + 1, padding.top + chartH + 18);
  }});

  // 凡例
  ctx.fillStyle = '#e74c3c';
  ctx.fillRect(padding.left, 4, 12, 12);
  ctx.fillStyle = '#2d3436';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText('収入', padding.left + 16, 14);
  ctx.fillStyle = '#2881D7';
  ctx.fillRect(padding.left + 55, 4, 12, 12);
  ctx.fillStyle = '#2d3436';
  ctx.fillText('支出', padding.left + 71, 14);
}}

// 積立額変更
function updateContrib() {{
  const v = document.getElementById('contrib-input').value;
  const url = new URL(window.location);
  url.searchParams.set('contrib', v);
  location.href = url.toString();
}}

// --- 配当実績チャート ---
(function() {{
  const divData = {div_chart_data};
  const divCanvas = document.getElementById('div-chart');
  if (!divData.length || !divCanvas) return;
  const ctx = divCanvas.getContext('2d');
  const W = divCanvas.parentElement.clientWidth - 40;
  divCanvas.width = W;
  divCanvas.height = 200;
  ctx.clearRect(0, 0, W, 200);

  const padding = {{ left: 70, right: 20, top: 20, bottom: 40 }};
  const chartW = W - padding.left - padding.right;
  const chartH = 200 - padding.top - padding.bottom;

  const maxAmt = Math.max(...divData.map(d => d.amount));
  const barW = Math.min(30, chartW / divData.length * 0.7);
  const gap = (chartW - barW * divData.length) / (divData.length + 1);

  // Y軸グリッド
  ctx.strokeStyle = '#f1f2f6';
  ctx.fillStyle = '#b2bec3';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {{
    const y = padding.top + chartH * (1 - i/4);
    const val = maxAmt * i / 4;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(W - padding.right, y);
    ctx.stroke();
    ctx.fillText(val >= 10000 ? (val/10000).toFixed(1) + '万' : val.toLocaleString(), padding.left - 6, y + 4);
  }}

  // バー描画
  ctx.fillStyle = '#0F7F30';
  ctx.textAlign = 'center';
  divData.forEach((d, i) => {{
    const x = padding.left + gap + i * (barW + gap);
    const h = (d.amount / maxAmt) * chartH;
    const y = padding.top + chartH - h;
    ctx.fillStyle = '#0F7F30';
    ctx.fillRect(x, y, barW, h);
    // ラベル
    ctx.fillStyle = '#636e72';
    ctx.font = '10px sans-serif';
    const label = d.year_month.slice(2).replace('-', '/');
    ctx.save();
    ctx.translate(x + barW/2, padding.top + chartH + 12);
    ctx.rotate(-0.5);
    ctx.fillText(label, 0, 0);
    ctx.restore();
    // 金額
    ctx.fillStyle = '#2d3436';
    ctx.font = '10px sans-serif';
    if (h > 20) {{
      ctx.fillText(d.amount >= 10000 ? (d.amount/10000).toFixed(1) + '万' : d.amount.toLocaleString(), x + barW/2, y - 4);
    }}
  }});
}})()

{
        "// reload polling"
        + f'''
const loadedVersion = {_update_state["version"]};
const pollId = setInterval(async () => {{
  try {{
    const r = await fetch('/api/status');
    const s = await r.json();
    if (s.version > loadedVersion) {{
      document.getElementById('reload-banner').style.display = 'flex';
      clearInterval(pollId);
    }}
  }} catch(e) {{}}
}}, 5000);
'''
        if not skip_update
        else ""
    }
{_COLLAPSE_JS}
</script>
</body>
</html>"""


def _build_settings_html(db_path: str, saved: str | None = None, skip_update: bool = False) -> str:
    """設定ページのHTMLを生成する。skip_update はスケジューラが起動していないモード。"""
    import os

    conn = get_connection(db_path)
    try:
        db_key = get_setting(conn, "gemini_api_key", "")
        closing_day = int(get_setting(conn, "closing_day", "1") or "1")
        holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"
        scheduler_enabled = get_setting(conn, "scheduler_enabled", "1") != "0"
        scheduler_time = get_setting(conn, "scheduler_time", _SCHEDULER_DEFAULT_TIME) or _SCHEDULER_DEFAULT_TIME
        scheduler_last_run = get_setting(conn, "scheduler_last_run_at")
        scheduler_last_result = get_setting(conn, "scheduler_last_result")
        moneyforward_card_schedule_last_fetch = get_setting(conn, "moneyforward_card_schedule_last_fetch_at")
        regional_holdings = get_regional_exposure_holdings(conn)
        regional_config = get_regional_exposure_config(conn)
        investable = calculate_investable_cash(conn)
        scheduled_card_payments = list_scheduled_card_payments(conn)
        monthly_living_expense = int(float(get_setting(conn, "monthly_living_expense", "0") or "0"))
        emergency_fund_months = float(get_setting(conn, "emergency_fund_months", "6") or "6")
        planned_expense_horizon_months = int(float(get_setting(conn, "planned_expense_horizon_months", "12") or "12"))
        additional_cash_reserve = int(float(get_setting(conn, "additional_cash_reserve", "0") or "0"))
    finally:
        conn.close()

    # スケジューラのステータス表示
    result_label = {"success": "成功", "failure": "失敗", "skipped": "スキップ"}.get(scheduler_last_result or "", "")
    scheduler_status = "まだ実行されていません"
    if scheduler_last_run:
        with contextlib.suppress(ValueError):
            last_run_disp = datetime.fromisoformat(scheduler_last_run).strftime("%Y-%m-%d %H:%M")
            scheduler_status = f"最終実行: {last_run_disp}" + (f" — {result_label}" if result_label else "")
    if skip_update:
        scheduler_status += " ／ 自動取得: 停止中（--demo / --skip-update で起動中）"
    elif scheduler_enabled:
        next_run = _next_scheduled_run(datetime.now(), scheduler_time)
        scheduler_status += f" ／ 次回予定: {next_run.strftime('%Y-%m-%d %H:%M')}"
    else:
        scheduler_status += " ／ 自動取得: オフ"
    env_key = os.environ.get("GEMINI_API_KEY", "")
    # 表示用マスク
    if env_key:
        source = "環境変数"
        display_key = env_key[:8] + "..." if len(env_key) > 8 else env_key
    elif db_key:
        source = "DB設定"
        display_key = db_key[:8] + "..." if len(db_key) > 8 else db_key
    else:
        source = ""
        display_key = ""

    # 保存メッセージは setting_type ごとに分岐（"1" は旧URL互換で gemini 扱い）
    if saved == "regional_error":
        saved_msg = (
            '<div class="saved-msg" style="background:#DF3727">地域配分の合計を商品ごとに100%にしてください。</div>'
        )
    elif saved == "scheduled_card_payment_error":
        saved_msg = (
            '<div class="saved-msg" style="background:#DF3727">カード引き落とし予定の入力を確認してください。'
            "日付・カード名・正の金額が必要です。</div>"
        )
    elif saved in ("gemini", "1"):
        saved_msg = (
            '<div class="saved-msg">設定を保存しました。AIコメントをバックグラウンドで生成中です'
            " — 数十秒後にダッシュボードを開くと表示されます。</div>"
        )
    elif saved:
        saved_msg = '<div class="saved-msg">設定を保存しました。</div>'
    else:
        saved_msg = ""

    region_fields = [
        ("日本", "japan"),
        ("米国", "us"),
        ("先進国（日本・米国除く）", "developed"),
        ("新興国", "emerging"),
        ("その他", "other"),
    ]
    regional_rows = ""
    for index, holding in enumerate(regional_holdings):
        allocation = regional_config.get(holding["key"], {})
        inputs = "".join(
            f'<label style="font-size:0.75rem;display:block">{_h(label)}<input type="number" min="0" max="100" step="0.1" '
            f'style="display:block;width:100%;padding:6px 8px;border:1px solid #dfe6e9;border-radius:6px" '
            f'name="region_{index}_{slug}" value="{float(allocation.get(label, 0)):g}" required></label>'
            for label, slug in region_fields
        )
        regional_rows += f"""
        <div style="border-top:1px solid #eee;padding:12px 0">
          <input type="hidden" name="exposure_key_{index}" value="{_h(holding["key"])}">
          <div style="font-weight:600">{_h(holding["name"])} <span style="color:#636e72;font-size:0.8rem">({_h(holding["asset_class"])})</span></div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:8px;margin-top:8px">{inputs}</div>
        </div>"""
    if not regional_rows:
        regional_rows = '<p style="font-size:0.85rem;color:#636e72">最新データに投資信託・年金の商品がありません。</p>'

    scheduled_investable_ids = {item["id"] for item in investable.get("scheduled_card_payments", [])}
    today_iso = date.today().isoformat()
    scheduled_card_rows = ""
    for payment in scheduled_card_payments:
        if payment["due_date"] < today_iso:
            status = '<span style="color:#b2bec3">予定日経過・計算対象外</span>'
        elif payment["id"] in scheduled_investable_ids:
            status = '<span style="color:#0F7F30">投資可能額に反映</span>'
        else:
            status = '<span style="color:#636e72">計画期間外</span>'
        is_moneyforward = payment.get("source") == "moneyforward"
        source_label = "MoneyForward自動" if is_moneyforward else "手入力"
        action_html = (
            '<span style="color:#636e72;font-size:0.75rem">自動更新</span>'
            if is_moneyforward
            else f"""
            <form method="POST" action="/settings" style="margin:0">
              <input type="hidden" name="setting_type" value="scheduled_card_payment">
              <input type="hidden" name="scheduled_action" value="disable">
              <input type="hidden" name="scheduled_payment_id" value="{payment["id"]}">
              <button type="submit" class="btn btn-muted" style="padding:4px 8px;font-size:0.75rem">無効化</button>
            </form>"""
        )
        scheduled_card_rows += f"""
        <tr>
          <td>{_h(payment["due_date"])}</td>
          <td>{_h(payment["card_name"])}</td>
          <td>{_h(payment["withdrawal_account"]) or "-"}</td>
          <td style="text-align:right">{payment["amount"]:,.0f}円</td>
          <td>{_h(payment["memo"]) or "-"}</td>
          <td>{status}</td>
          <td>{source_label}</td>
          <td>{action_html}</td>
        </tr>"""
    if not scheduled_card_rows:
        scheduled_card_rows = '<tr><td colspan="8" style="color:#636e72">登録済みの予定はありません。</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%232881D7'/><path d='M50 5A45 45 0 0 1 95 50L50 50Z' fill='%23FCAD4C'/><path d='M50 5A45 45 0 0 0 10.2 72.5L50 50Z' fill='%230F7F30'/></svg>">
<title>設定</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f6fa; color: #2d3436; line-height: 1.6; }}
  .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
  {_NAV_CSS}
  h1 {{ font-size: 1.5rem; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 20px; }}
  .card h2 {{ font-size: 1.1rem; margin-bottom: 12px; }}
  .field {{ margin-bottom: 16px; }}
  .field label {{ display: block; font-size: 0.85rem; color: #636e72; margin-bottom: 4px; font-weight: 600; }}
  .field input {{ width: 100%; padding: 8px 12px; border: 1px solid #dfe6e9; border-radius: 6px; font-size: 0.9rem; }}
  .field .hint {{ font-size: 0.78rem; color: #b2bec3; margin-top: 4px; }}
  .status {{ font-size: 0.85rem; color: #636e72; margin-bottom: 12px; padding: 8px 12px; background: #f8f9fa; border-radius: 6px; }}
  .status .active {{ color: #0F7F30; font-weight: 600; }}
  .status .inactive {{ color: #b2bec3; }}
  .btn {{ padding: 8px 20px; background: #2881D7; color: #fff; border: none; border-radius: 6px;
          font-size: 0.9rem; font-weight: 600; cursor: pointer; }}
  .btn:hover {{ background: #1a6cb8; }}
  .btn-muted {{ background: #636e72; }}
  .btn-muted:hover {{ background: #4b5559; }}
  .scheduled-table {{ width:100%; border-collapse:collapse; font-size:0.8rem; margin:12px 0 18px; }}
  .scheduled-table th, .scheduled-table td {{ border-bottom:1px solid #eee; padding:8px 6px; text-align:left; vertical-align:middle; }}
  .scheduled-table th {{ color:#636e72; font-weight:600; white-space:nowrap; }}
  .scheduled-table td:nth-child(4) {{ white-space:nowrap; }}
  .scheduled-scroll {{ overflow-x:auto; }}
  .saved-msg {{ background: #0F7F30; color: #fff; padding: 10px 16px; border-radius: 8px;
               margin-bottom: 16px; font-size: 0.9rem; font-weight: 600; }}
</style>
</head>
<body>
<div class="container">
  <div class="page-header">
    <h1>設定</h1>
    {_nav_html("/settings")}
  </div>
  {saved_msg}
  <div class="card">
    <h2>AI分析（Gemini API）</h2>
    <div class="status">
      現在のステータス:
      {f'<span class="active">有効</span>（{source}: {display_key}）' if display_key else '<span class="inactive">未設定 — APIキーを入力するとAI分析コメントが有効になります</span>'}
    </div>
    <form method="POST" action="/settings">
      <div class="field">
        <label>Gemini APIキー</label>
        <input type="password" name="gemini_api_key" value="{db_key}" placeholder="AIza...">
        <div class="hint">
          Google AI Studio（<a href="https://aistudio.google.com/apikey" target="_blank">aistudio.google.com/apikey</a>）で無料で取得できます。
          環境変数 GEMINI_API_KEY でも設定可能です。空欄で保存するとDBのキーを削除します。
        </div>
      </div>
      <button type="submit" class="btn">保存</button>
    </form>
  </div>
  <div class="card">
    <h2>家計簿の締め日</h2>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">
      毎月の集計開始日を設定します。例: 25日に設定すると「2月分 = 1/25〜2/24」で集計されます。
    </p>
    <form method="POST" action="/settings">
      <input type="hidden" name="setting_type" value="closing_day">
      <div class="field">
        <label>集計開始日</label>
        <select name="closing_day" style="width:auto;padding:8px 12px;border:1px solid #dfe6e9;border-radius:6px;font-size:0.9rem">
          <option value="1"{"selected" if closing_day <= 1 else ""}>1日（暦月）</option>
          {"".join(f'<option value="{d}"{" selected" if closing_day == d else ""}>{d}日</option>' for d in range(2, 32))}
        </select>
        <div class="hint">給与日に合わせると実際の家計サイクルに合った分析ができます。</div>
      </div>
      <div class="field" style="margin-top:12px;text-align:left">
        <label>土日祝日の扱い</label>
        <div style="display:flex;flex-direction:column;align-items:flex-start;gap:6px;margin-top:4px">
          <label style="font-weight:normal;font-size:0.9rem;display:flex;align-items:center;gap:6px">
            <input type="radio" name="holiday_mode" value="none" style="width:auto"{"checked" if holiday_mode == "none" else ""}> 変更しない
          </label>
          <label style="font-weight:normal;font-size:0.9rem;display:flex;align-items:center;gap:6px">
            <input type="radio" name="holiday_mode" value="before" style="width:auto"{"checked" if holiday_mode == "before" else ""}> 設定日前の平日
          </label>
          <label style="font-weight:normal;font-size:0.9rem;display:flex;align-items:center;gap:6px">
            <input type="radio" name="holiday_mode" value="after" style="width:auto"{"checked" if holiday_mode == "after" else ""}> 設定日後の平日
          </label>
        </div>
        <div class="hint">締め日が土日祝日に当たる場合の調整方法を選択します。</div>
      </div>
      <button type="submit" class="btn">保存</button>
    </form>
  </div>
  <div class="card">
    <h2>自動データ取得</h2>
    <div class="status">{scheduler_status}</div>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">
      サーバー起動中、毎日指定した時刻にデータを自動取得します。停止していた場合は起動後に追いつき実行されます。
    </p>
    <form method="POST" action="/settings">
      <input type="hidden" name="setting_type" value="scheduler">
      <div class="field">
        <label style="font-weight:normal;font-size:0.9rem;display:flex;align-items:center;gap:6px">
          <input type="checkbox" name="scheduler_enabled" value="1" style="width:auto"{" checked" if scheduler_enabled else ""}> 自動取得を有効にする
        </label>
      </div>
      <div class="field">
        <label>実行時刻</label>
        <input type="time" name="scheduler_time" value="{scheduler_time}" style="width:auto">
        <div class="hint">デフォルトは毎日 7:00 です。設定はサーバー再起動なしで反映されます。</div>
      </div>
      <button type="submit" class="btn">保存</button>
    </form>
  </div>
  <div class="card">
    <h2>投信・年金の地域配分</h2>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">
      各商品の投資地域を設定します。推測は行わず、商品ごとの合計を100%にしてください。
    </p>
    <form method="POST" action="/settings">
      <input type="hidden" name="setting_type" value="regional_exposure">
      <input type="hidden" name="exposure_count" value="{len(regional_holdings)}">
      {regional_rows}
      <button type="submit" class="btn" style="margin-top:12px">保存</button>
    </form>
  </div>
  <div class="card">
    <h2 id="investable-cash">投資可能額の計算</h2>
    <div class="status">
      現在の投資可能額: <strong style="color:#0F7F30">{investable["investable_cash"]:,.0f}円</strong>
      ／ 預金・現金 {investable["cash_balance"]:,.0f}円
    </div>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">
      預金・現金から生活防衛資金、計画期間内のライフイベント、追加確保額を差し引きます。
      年単位のイベントは対象年の全額を保守的に確保します。
    </p>
    <form method="POST" action="/settings">
      <input type="hidden" name="setting_type" value="investable_cash">
      <div class="field">
        <label>月間生活費（円）</label>
        <input type="number" name="monthly_living_expense" min="0" step="1000" value="{monthly_living_expense}">
        <div class="hint">0の場合は直近6か月の支出実績平均を使います。</div>
      </div>
      <div class="field">
        <label>生活防衛資金（月数）</label>
        <input type="number" name="emergency_fund_months" min="0" max="60" step="0.5" value="{emergency_fund_months:g}">
      </div>
      <div class="field">
        <label>予定支出を確保する期間（月）</label>
        <input type="number" name="planned_expense_horizon_months" min="0" max="120" step="1" value="{planned_expense_horizon_months}">
      </div>
      <div class="field">
        <label>追加で現金として確保する額（円）</label>
        <input type="number" name="additional_cash_reserve" min="0" step="1000" value="{additional_cash_reserve}">
       </div>
       <button type="submit" class="btn">保存</button>
    </form>
  </div>
  <div class="card" id="scheduled-card-payments" data-testid="scheduled-card-payments">
    <h2>カード引き落とし予定</h2>
    <div class="status">
      {f"MoneyForwardから最終取得: {moneyforward_card_schedule_last_fetch}" if moneyforward_card_schedule_last_fetch else "MoneyForwardからの自動取得はまだ実行されていません。"}
    </div>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">
      MoneyForwardの登録口座詳細から、引落日と引落額を日次で自動取得します。
      引落予定日が「予定支出を確保する期間」内にあるものだけ、投資可能額から差し引きます。
      自動取得にない予定だけ、補助的に手入力できます。過去日・期間外の予定は計算対象外です。
    </p>
    <div class="scheduled-scroll">
      <table class="scheduled-table">
        <thead><tr><th>引落日</th><th>カード</th><th>引落口座</th><th>金額</th><th>メモ</th><th>状態</th><th>取得元</th><th></th></tr></thead>
        <tbody>{scheduled_card_rows}</tbody>
      </table>
    </div>
    <details style="margin-top:12px">
      <summary style="cursor:pointer;color:#2881D7;font-size:0.9rem">自動取得できない予定を手入力</summary>
    <form method="POST" action="/settings" style="margin-top:14px">
      <input type="hidden" name="setting_type" value="scheduled_card_payment">
      <input type="hidden" name="scheduled_action" value="add">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px">
        <div class="field"><label>引落予定日</label><input type="date" name="scheduled_due_date" required></div>
        <div class="field"><label>カード名</label><input type="text" name="scheduled_card_name" maxlength="80" placeholder="例: 楽天カード" required></div>
        <div class="field"><label>金額（円）</label><input type="number" name="scheduled_amount" min="1" step="1" placeholder="100000" required></div>
        <div class="field"><label>引落口座（任意）</label><input type="text" name="scheduled_withdrawal_account" maxlength="80" placeholder="例: きらぼし銀行"></div>
        <div class="field"><label>メモ（任意）</label><input type="text" name="scheduled_memo" maxlength="160" placeholder="例: 8月利用分"></div>
      </div>
      <button type="submit" class="btn">予定を追加</button>
    </form>
    </details>
  </div>
  <div class="card">
    <h2>データエクスポート</h2>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">CSV / JSON 形式でダウンロードできます。</p>
    <div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">資産スナップショット</span>
        <a href="/api/export/snapshots?format=csv" class="btn" style="font-size:0.8rem;padding:5px 12px;text-decoration:none">CSV</a>
        <a href="/api/export/snapshots?format=json" class="btn" style="font-size:0.8rem;padding:5px 12px;text-decoration:none;background:#636e72">JSON</a>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">月次収支</span>
        <a href="/api/export/cashflows?format=csv" class="btn" style="font-size:0.8rem;padding:5px 12px;text-decoration:none">CSV</a>
        <a href="/api/export/cashflows?format=json" class="btn" style="font-size:0.8rem;padding:5px 12px;text-decoration:none;background:#636e72">JSON</a>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">家計簿取引明細</span>
        <a href="/api/export/cf?format=csv" class="btn" style="font-size:0.8rem;padding:5px 12px;text-decoration:none">CSV</a>
        <a href="/api/export/cf?format=json" class="btn" style="font-size:0.8rem;padding:5px 12px;text-decoration:none;background:#636e72">JSON</a>
      </div>
    </div>
  </div>
  <div class="card">
    <h2>AIチャット用データ</h2>
    <p style="font-size:0.85rem;color:#636e72;margin-bottom:12px">
      ChatGPT / Claude 等にコピペで渡せるMarkdown形式のデータ+プロンプトを生成します。
    </p>
    <div style="display:flex;flex-direction:column;gap:8px">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">一括コピー（総合分析）</span>
        <button class="btn" style="font-size:0.8rem;padding:5px 12px" onclick="copyAiPrompt('all',this)">コピー</button>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">資産分析</span>
        <button class="btn" style="font-size:0.8rem;padding:5px 12px" onclick="copyAiPrompt('asset',this)">コピー</button>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">家計簿分析</span>
        <button class="btn" style="font-size:0.8rem;padding:5px 12px" onclick="copyAiPrompt('cf',this)">コピー</button>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">ライフプラン</span>
        <button class="btn" style="font-size:0.8rem;padding:5px 12px" onclick="copyAiPrompt('plan',this)">コピー</button>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span style="font-size:0.85rem;width:140px">シミュレーター</span>
        <button class="btn" style="font-size:0.8rem;padding:5px 12px" onclick="copyAiPrompt('simulator',this)">コピー</button>
      </div>
    </div>
    <div id="ai-preview" style="display:none;margin-top:12px;background:#f8f9fa;border-radius:8px;padding:12px;font-size:0.8rem;white-space:pre-wrap;max-height:300px;overflow-y:auto;border:1px solid #dfe6e9"></div>
  </div>
</div>
<script>
async function copyAiPrompt(type, btn) {{
  btn.textContent = '取得中...';
  btn.disabled = true;
  try {{
    const res = await fetch('/api/ai-prompt?type=' + type);
    const text = await res.text();
    await navigator.clipboard.writeText(text);
    btn.textContent = 'コピー済み!';
    btn.style.background = '#0F7F30';
    const preview = document.getElementById('ai-preview');
    preview.textContent = text;
    preview.style.display = 'block';
    setTimeout(() => {{ btn.textContent = 'コピー'; btn.style.background = ''; btn.disabled = false; }}, 2000);
  }} catch(e) {{
    btn.textContent = 'エラー';
    btn.style.background = '#e74c3c';
    setTimeout(() => {{ btn.textContent = 'コピー'; btn.style.background = ''; btn.disabled = false; }}, 2000);
  }}
}}
</script>
</body>
</html>"""


def _get_cf_data(db_path: str, year_month: str | None = None) -> dict:
    """家計簿分析データを取得する。"""
    conn = get_connection(db_path)
    try:
        # 締め日設定
        closing_day = int(get_setting(conn, "closing_day", "1") or "1")
        holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"

        # 利用可能月
        available = get_cf_available_months(conn, closing_day=closing_day, holiday_mode=holiday_mode)
        if not available:
            return {}

        # デフォルト月 = 最新月
        if year_month is None:
            year_month = available[0]["year_month"]

        # カテゴリ集計
        summary = get_cf_category_summary(conn, year_month, closing_day=closing_day, holiday_mode=holiday_mode)

        # 月別推移
        trend = get_cf_monthly_trend(conn, months=12, closing_day=closing_day, holiday_mode=holiday_mode)

        # カテゴリ別月次推移
        category_trend = get_cf_category_trend(conn, months=6, closing_day=closing_day, holiday_mode=holiday_mode)
        category_details = get_cf_category_details_history(
            conn, months=6, closing_day=closing_day, holiday_mode=holiday_mode
        )

        # 固定費 vs 変動費
        fixed_expenses = get_cf_fixed_expenses(conn, months=3, closing_day=closing_day, holiday_mode=holiday_mode)

        # 収入内訳
        income_breakdown = get_cf_income_breakdown(conn, year_month, closing_day=closing_day, holiday_mode=holiday_mode)

        # 収入推移
        income_trend = get_cf_income_trend(conn, months=6, closing_day=closing_day, holiday_mode=holiday_mode)

        # 予算
        budgets = get_budgets(conn)
    finally:
        conn.close()

    return {
        "year_month": year_month,
        "summary": summary,
        "trend": trend,
        "available_months": available,
        "category_trend": category_trend,
        "category_details": category_details,
        "fixed_expenses": fixed_expenses,
        "income_breakdown": income_breakdown,
        "income_trend": income_trend,
        "budgets": budgets,
        "closing_day": closing_day,
        "holiday_mode": holiday_mode,
    }


def _demo_cf_data() -> dict:
    """家計簿分析ページ用のデモデータを生成する。"""
    from datetime import date

    today = date.today()
    ym = f"{today.year}-{today.month:02d}"

    major_categories = [
        {"name": "食費", "total": 68_500},
        {"name": "住宅", "total": 85_000},
        {"name": "光熱・水道", "total": 18_200},
        {"name": "通信費", "total": 12_800},
        {"name": "交通費", "total": 15_600},
        {"name": "日用品", "total": 8_900},
        {"name": "趣味・娯楽", "total": 22_300},
        {"name": "衣服・美容", "total": 14_500},
        {"name": "健康・医療", "total": 5_800},
        {"name": "教養・教育", "total": 4_200},
        {"name": "保険", "total": 12_000},
        {"name": "その他", "total": 6_700},
    ]

    minor_by_major = {
        "食費": [
            {"name": "食料品", "total": 42_000},
            {"name": "外食", "total": 18_500},
            {"name": "カフェ", "total": 8_000},
        ],
        "住宅": [
            {"name": "家賃・地代", "total": 85_000},
        ],
        "光熱・水道": [
            {"name": "電気代", "total": 8_500},
            {"name": "ガス・灯油代", "total": 5_200},
            {"name": "水道代", "total": 4_500},
        ],
        "趣味・娯楽": [
            {"name": "書籍", "total": 5_800},
            {"name": "サブスクリプション", "total": 4_500},
            {"name": "ゲーム", "total": 6_000},
            {"name": "旅行", "total": 6_000},
        ],
        "通信費": [
            {"name": "携帯電話", "total": 8_800},
            {"name": "インターネット", "total": 4_000},
        ],
        "衣服・美容": [
            {"name": "衣服", "total": 9_500},
            {"name": "美容院・理髪", "total": 5_000},
        ],
    }

    top_expenses = [
        {
            "date": f"{ym}-01",
            "description": "家賃",
            "amount": 85_000,
            "major_category": "住宅",
            "minor_category": "家賃・地代",
            "institution": "三井住友銀行",
        },
        {
            "date": f"{ym}-05",
            "description": "イオン",
            "amount": 12_500,
            "major_category": "食費",
            "minor_category": "食料品",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-07",
            "description": "保険料",
            "amount": 12_000,
            "major_category": "保険",
            "minor_category": "生命保険",
            "institution": "みずほ銀行",
        },
        {
            "date": f"{ym}-03",
            "description": "ユニクロ",
            "amount": 9_500,
            "major_category": "衣服・美容",
            "minor_category": "衣服",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-10",
            "description": "携帯電話料金",
            "amount": 8_800,
            "major_category": "通信費",
            "minor_category": "携帯電話",
            "institution": "みずほ銀行",
        },
        {
            "date": f"{ym}-02",
            "description": "電気代",
            "amount": 8_500,
            "major_category": "光熱・水道",
            "minor_category": "電気代",
            "institution": "三井住友銀行",
        },
        {
            "date": f"{ym}-12",
            "description": "定期券",
            "amount": 8_200,
            "major_category": "交通費",
            "minor_category": "電車",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-15",
            "description": "Amazon",
            "amount": 7_800,
            "major_category": "日用品",
            "minor_category": "日用品",
            "institution": "Amazonカード",
        },
        {
            "date": f"{ym}-08",
            "description": "レストラン",
            "amount": 6_800,
            "major_category": "食費",
            "minor_category": "外食",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-20",
            "description": "ゲームソフト",
            "amount": 6_000,
            "major_category": "趣味・娯楽",
            "minor_category": "ゲーム",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-14",
            "description": "書籍",
            "amount": 5_800,
            "major_category": "趣味・娯楽",
            "minor_category": "書籍",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-06",
            "description": "美容院",
            "amount": 5_000,
            "major_category": "衣服・美容",
            "minor_category": "美容院・理髪",
            "institution": "現金",
        },
        {
            "date": f"{ym}-04",
            "description": "ガス代",
            "amount": 5_200,
            "major_category": "光熱・水道",
            "minor_category": "ガス・灯油代",
            "institution": "三井住友銀行",
        },
        {
            "date": f"{ym}-09",
            "description": "サブスクリプション",
            "amount": 4_500,
            "major_category": "趣味・娯楽",
            "minor_category": "サブスクリプション",
            "institution": "楽天カード",
        },
        {
            "date": f"{ym}-11",
            "description": "水道代",
            "amount": 4_500,
            "major_category": "光熱・水道",
            "minor_category": "水道代",
            "institution": "三井住友銀行",
        },
    ]

    total_expense = sum(c["total"] for c in major_categories)
    total_income = 385_000

    summary = {
        "year_month": ym,
        "total_expense": total_expense,
        "total_income": total_income,
        "balance": total_income - total_expense,
        "major_categories": major_categories,
        "minor_by_major": minor_by_major,
        "top_expenses": top_expenses,
    }

    # 6ヶ月分の月別推移
    trend = []
    base_expense = 270_000
    base_income = 380_000
    import random

    random.seed(42)
    for i in range(6):
        m = today.month - 5 + i
        y = today.year
        while m <= 0:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        exp_var = random.randint(-30_000, 50_000)
        inc_var = random.randint(-5_000, 15_000)
        trend.append(
            {
                "year_month": f"{y}-{m:02d}",
                "expense": base_expense + exp_var,
                "income": base_income + inc_var,
            }
        )

    available = [
        {
            "year_month": t["year_month"],
            "has_data": True,
            "fetched": today.isoformat(),
            "row_count": random.randint(30, 80),
        }
        for t in reversed(trend)
    ]

    # カテゴリ別月次推移デモデータ
    trend_months = [t["year_month"] for t in trend]
    demo_cats = [
        "住宅",
        "食費",
        "趣味・娯楽",
        "光熱・水道",
        "交通費",
        "通信費",
        "衣服・美容",
        "保険",
        "日用品",
        "健康・医療",
        "教養・教育",
        "その他",
    ]
    demo_cat_bases = {
        "住宅": 85_000,
        "食費": 65_000,
        "趣味・娯楽": 20_000,
        "光熱・水道": 17_000,
        "交通費": 14_000,
        "通信費": 12_500,
        "衣服・美容": 12_000,
        "保険": 12_000,
        "日用品": 8_000,
        "健康・医療": 5_000,
        "教養・教育": 4_000,
        "その他": 6_000,
    }
    cat_by_month = {}
    for tm in trend_months:
        cat_by_month[tm] = {}
        for cat, base in demo_cat_bases.items():
            cat_by_month[tm][cat] = base + random.randint(-int(base * 0.15), int(base * 0.2))
        cat_by_month[tm]["住宅"] = 85_000  # 家賃は固定

    category_trend = {
        "year_months": trend_months,
        "categories": demo_cats,
        "by_month": cat_by_month,
        "avg_by_category": {
            cat: sum(cat_by_month[m].get(cat, 0) for m in trend_months) / len(trend_months) for cat in demo_cats
        },
        "avg_months": len(trend_months),
    }
    category_details = {
        "year_months": trend_months,
        "categories": demo_cats,
        "by_category": {
            "趣味・娯楽": [
                {
                    "year_month": trend_months[-1],
                    "date": f"{trend_months[-1]}-03",
                    "description": "Steam",
                    "amount": 6200,
                    "minor_category": "ゲーム",
                    "institution": "楽天カード",
                },
                {
                    "year_month": trend_months[-1],
                    "date": f"{trend_months[-1]}-14",
                    "description": "書籍",
                    "amount": 5800,
                    "minor_category": "書籍",
                    "institution": "Amazon",
                },
                {
                    "year_month": trend_months[-2],
                    "date": f"{trend_months[-2]}-09",
                    "description": "映画",
                    "amount": 1900,
                    "minor_category": "映画",
                    "institution": "楽天カード",
                },
            ],
            "食費": [
                {
                    "year_month": trend_months[-1],
                    "date": f"{trend_months[-1]}-05",
                    "description": "イオン",
                    "amount": 12400,
                    "minor_category": "食料品",
                    "institution": "楽天カード",
                },
                {
                    "year_month": trend_months[-2],
                    "date": f"{trend_months[-2]}-19",
                    "description": "外食",
                    "amount": 4800,
                    "minor_category": "外食",
                    "institution": "三井住友カード",
                },
            ],
        },
    }

    # 固定費 vs 変動費デモデータ
    fixed_expenses = {
        "fixed": [
            {"major": "住宅", "minor": "家賃・地代", "avg_amount": 85_000, "latest": 85_000},
            {"major": "保険", "minor": "生命保険", "avg_amount": 12_000, "latest": 12_000},
            {"major": "通信費", "minor": "携帯電話", "avg_amount": 8_800, "latest": 8_800},
            {"major": "通信費", "minor": "インターネット", "avg_amount": 4_000, "latest": 4_000},
            {"major": "趣味・娯楽", "minor": "サブスクリプション", "avg_amount": 4_500, "latest": 4_500},
        ],
        "fixed_total": 114_300,
        "variable_total": 160_200,
        "fixed_ratio": 42,
        "months_used": 3,
    }

    # 収入内訳デモデータ
    income_breakdown = {
        "items": [
            {"name": "給与", "total": 350_000},
            {"name": "副業", "total": 25_000},
            {"name": "配当・利息", "total": 8_500},
            {"name": "その他", "total": 1_500},
        ],
        "total": 385_000,
    }

    # 収入推移デモデータ
    income_trend = [{"year_month": tm, "income": 380_000 + random.randint(-5_000, 15_000)} for tm in trend_months]

    # 予算デモデータ
    budgets = {
        "食費": 70_000,
        "住宅": 90_000,
        "光熱・水道": 20_000,
        "通信費": 15_000,
        "趣味・娯楽": 30_000,
        "日用品": 10_000,
    }

    return {
        "year_month": ym,
        "summary": summary,
        "trend": trend,
        "available_months": available,
        "category_trend": category_trend,
        "category_details": category_details,
        "fixed_expenses": fixed_expenses,
        "income_breakdown": income_breakdown,
        "income_trend": income_trend,
        "budgets": budgets,
    }


def _build_cf_html(data: dict, skip_update: bool = False, ai_comment: str | None = None) -> str:
    """家計簿分析ページの HTML を生成する。"""
    if not data:
        return "<html><body><h1>データがありません</h1><p><a href='/'>ダッシュボードに戻る</a></p></body></html>"

    year_month = data["year_month"]
    summary = data["summary"]
    trend = data.get("trend", [])
    available = data.get("available_months", [])
    closing_day = data.get("closing_day", 1)
    holiday_mode = data.get("holiday_mode", "none")

    # 当月（途中データ）判定 — fiscal month ベース
    from src.db.repository import _current_fiscal_month, _fiscal_month_range

    is_partial_month = year_month == _current_fiscal_month(closing_day, holiday_mode)
    progress_ratio: float | None = None
    progress_days_elapsed = 0
    progress_days_total = 0
    if is_partial_month:
        start_s, end_s = _fiscal_month_range(year_month, closing_day, holiday_mode)
        try:
            period_start = datetime.strptime(start_s, "%Y-%m-%d").date()
            period_end = datetime.strptime(end_s, "%Y-%m-%d").date()
            if period_end >= period_start:
                progress_days_total = (period_end - period_start).days + 1
                today = date.today()
                if today < period_start:
                    progress_days_elapsed = 0
                elif today >= period_end:
                    progress_days_elapsed = progress_days_total
                else:
                    progress_days_elapsed = (today - period_start).days + 1
                if progress_days_total > 0:
                    progress_ratio = progress_days_elapsed / progress_days_total
        except ValueError:
            progress_ratio = None

    total_expense = summary["total_expense"]
    total_income = summary["total_income"]
    balance = summary["balance"]
    major_categories = summary["major_categories"]
    minor_by_major = summary.get("minor_by_major", {})
    top_expenses = summary.get("top_expenses", [])
    budgets = data.get("budgets", {})

    # 月セレクタ
    month_options = ""
    for m in available:
        sel = " selected" if m["year_month"] == year_month else ""
        month_options += f'<option value="{_h(m["year_month"])}"{sel}>{_h(m["year_month"])}</option>'

    # カテゴリ別円グラフデータ
    colors = [
        "#2881D7",
        "#DF3727",
        "#FCAD4C",
        "#0F7F30",
        "#008986",
        "#9C39B6",
        "#FF5266",
        "#80BD45",
        "#FF689A",
        "#1FBBDB",
        "#FD9441",
        "#6C5CE7",
        "#00B894",
    ]
    pie_data = json.dumps(
        [
            {
                "label": c["name"],
                "value": c["total"],
                "color": colors[i % len(colors)],
                "details": [{"name": m["name"], "value": m["total"]} for m in minor_by_major.get(c["name"], [])],
            }
            for i, c in enumerate(major_categories)
        ],
        ensure_ascii=False,
    )

    # カテゴリテーブル（予算列付き）
    cat_rows = ""
    for i, c in enumerate(major_categories):
        color = colors[i % len(colors)]
        details = minor_by_major.get(c["name"], [])
        details_attr = _h(json.dumps(details, ensure_ascii=False))
        budget = budgets.get(c["name"])
        safe_name = _h(c["name"])
        if budget and budget > 0:
            budget_display = f"{budget:,.0f}円"
            usage_pct = c["total"] / budget * 100
            if usage_pct < 80:
                bar_color = "#2881D7"
            elif usage_pct <= 100:
                bar_color = "#FFD54F"
            else:
                bar_color = "#DF3727"
            bar_width = min(usage_pct, 100)
            progress_html = f'<div class="budget-bar-bg"><div class="budget-bar" style="width:{bar_width}%;background:{bar_color}"></div></div><span class="budget-pct" style="color:{bar_color}">{usage_pct:.0f}%</span>'
        else:
            budget_display = "—"
            progress_html = ""
        cat_rows += f"""
        <tr class="has-tip" data-details="{details_attr}" data-label="{safe_name}">
          <td><span class="dot" style="background:{color}"></span>{safe_name}</td>
          <td class="num">{c["total"]:,.0f}円</td>
          <td class="num budget-cell" data-category="{safe_name}" data-amount="{budget or 0}">{budget_display}</td>
          <td class="progress-cell">{progress_html}</td>
        </tr>"""

    # 予算残りサマリー（常にレンダリング、予算未設定時は非表示）
    budget_total = 0
    actual_total = 0
    cat_totals = {c["name"]: c["total"] for c in major_categories}
    for cat, amt in budgets.items():
        if amt > 0:
            budget_total += amt
            actual_total += cat_totals.get(cat, 0)
    remaining = budget_total - actual_total
    remaining_color = "#0F7F30" if remaining >= 0 else "#DF3727"
    remaining_sign = "+" if remaining >= 0 else ""
    budget_display_style = "" if budget_total > 0 else "display:none"

    # 予算合計カード（収入比つき）
    income_ratio_html = ""
    if budget_total > 0 and total_income > 0:
        income_ratio = budget_total / total_income * 100
        income_ratio_html = f'<div class="sub-info">収入の {income_ratio:.0f}%</div>'
    budget_total_html = f"""
    <div class="summary-card" data-testid="budget-total" style="{budget_display_style}">
      <h3>予算合計</h3>
      <div class="amount">{budget_total:,.0f}円</div>
      {income_ratio_html}
    </div>"""

    # 前月実績（予算設定済みカテゴリの前月支出合計）
    prev_month_html = ""
    cat_trend = data.get("category_trend", {})
    cat_trend_months = cat_trend.get("year_months", [])
    cat_trend_by_month = cat_trend.get("by_month", {})
    if budget_total > 0 and len(cat_trend_months) >= 2:
        prev_m = cat_trend_months[-2]
        prev_data = cat_trend_by_month.get(prev_m, {})
        prev_budget_actual = sum(prev_data.get(cat, 0) for cat in budgets if budgets[cat] > 0)
        if prev_budget_actual > 0:
            prev_month_html = f'<div class="sub-info">先月実績: {prev_budget_actual:,.0f}円</div>'

    budget_remaining_html = f"""
    <div class="summary-card" data-testid="budget-remaining" style="{budget_display_style}">
      <h3>予算残り</h3>
      <div class="amount" style="color:{remaining_color}">{remaining_sign}{remaining:,.0f}円</div>
      {prev_month_html}
    </div>"""

    # 高額支出テーブル（生活支出に寄せるため、積立・管理費等は除外）
    filtered_top_expenses = [t for t in top_expenses if not _is_top_expense_excluded(t)]
    using_filtered_top = len(filtered_top_expenses) > 0
    table_top_expenses = (filtered_top_expenses if using_filtered_top else top_expenses)[:15]
    top_title = "高額支出 TOP15（生活支出）" if using_filtered_top else "高額支出 TOP15"
    top_note = (
        '<div style="font-size:0.8rem;color:#636e72;margin:0 0 8px">積立・管理費等を除外して表示しています</div>'
        if using_filtered_top
        else '<div style="font-size:0.8rem;color:#636e72;margin:0 0 8px">除外条件に一致したため元データを表示しています</div>'
    )

    top_rows = ""
    for t in table_top_expenses:
        top_rows += f"""<tr>
          <td>{_h(t["date"][5:])}</td>
          <td>{_h(t["description"])}</td>
          <td class="num">{t["amount"]:,.0f}円</td>
          <td>{_h(t["major_category"])}</td>
          <td style="color:#636e72;font-size:0.82rem">{_h(t.get("institution", ""))}</td>
        </tr>"""

    # 月別推移データ
    trend_data = json.dumps(trend, ensure_ascii=False)

    # ダウンロード管理テーブル
    dl_rows = ""
    for m in available:
        fetched_date = m.get("fetched") or ""
        row_count = m.get("row_count") or 0
        safe_ym = _h(m["year_month"])
        if m["has_data"] and fetched_date:
            status = f'<span style="color:#0F7F30">取得済</span> ({_h(fetched_date)}、{row_count}件)'
        elif m["has_data"]:
            status = f'<span style="color:#0F7F30">取得済</span> ({row_count}件)'
        else:
            status = '<span style="color:#b2bec3">未取得</span>'
        dl_btn = (
            ""
            if m["has_data"]
            else f'<button class="dl-btn" onclick="downloadMonth(this.dataset.ym, this)" data-ym="{safe_ym}">ダウンロード</button>'
        )
        dl_rows += f"<tr><td>{safe_ym}</td><td>{status}</td><td>{dl_btn}</td></tr>"

    # 年・月セレクトボックスの選択肢を生成
    now = datetime.now()
    year_options = "".join(
        f'<option value="{y}"{" selected" if y == now.year else ""}>{y}</option>' for y in range(now.year, 2019, -1)
    )
    month_options = "".join(
        f'<option value="{m:02d}"{" selected" if m == now.month else ""}>{m}月</option>' for m in range(1, 13)
    )

    balance_sign = "+" if balance >= 0 else ""
    balance_css = "plus" if balance >= 0 else "minus"

    # --- カテゴリ別月次推移データ ---
    cat_trend = data.get("category_trend", {})
    cat_trend_months = cat_trend.get("year_months", [])
    cat_trend_categories = cat_trend.get("categories", [])
    cat_trend_by_month = cat_trend.get("by_month", {})
    cat_trend_avg_by_category = cat_trend.get("avg_by_category", {})
    cat_trend_avg_months = int(cat_trend.get("avg_months", len(cat_trend_months) or 0))
    cat_trend_json = json.dumps(
        {
            "months": cat_trend_months,
            "categories": cat_trend_categories,
            "by_month": cat_trend_by_month,
            "avg_by_category": cat_trend_avg_by_category,
            "avg_months": cat_trend_avg_months,
        },
        ensure_ascii=False,
    )

    # --- カテゴリ別支出の明細（直近Nヶ月） ---
    cat_details = data.get("category_details", {})
    cat_detail_months = cat_details.get("year_months", [])
    cat_detail_categories = cat_details.get("categories", [])
    cat_detail_by_category = cat_details.get("by_category", {})
    cat_detail_json = json.dumps(
        {"months": cat_detail_months, "categories": cat_detail_categories, "by_category": cat_detail_by_category},
        ensure_ascii=False,
    )
    detail_options = "".join(f'<option value="{_h(c)}">{_h(c)}</option>' for c in cat_detail_categories)
    cat_details_html = ""
    if cat_detail_categories:
        cat_details_html = f"""
    <div class="card full" data-card-id="cf-cat-details" data-default-collapsed="true">
      <div class="card-header">
        <h2>カテゴリ別支出の詳細（直近{len(cat_detail_months)}ヶ月）</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
          <label for="cat-detail-select" style="color:#636e72;font-size:0.9rem">カテゴリ</label>
          <select id="cat-detail-select" style="padding:6px 10px;border:1px solid #dfe6e9;border-radius:6px">{detail_options}</select>
          <label for="cat-detail-sort" style="color:#636e72;font-size:0.9rem">並び順</label>
          <select id="cat-detail-sort" style="padding:6px 10px;border:1px solid #dfe6e9;border-radius:6px">
            <option value="month_amount">月→金額（既定）</option>
            <option value="date_desc">日付（新しい順）</option>
            <option value="date_asc">日付（古い順）</option>
          </select>
        </div>
        <div class="cat-detail-table-wrap">
          <table class="cat-detail-table">
            <tr><th class="col-month">月</th><th class="col-date">日付</th><th class="col-desc">内容</th><th class="col-minor">中項目</th><th class="col-inst">金融機関</th><th class="num col-amount">金額</th></tr>
            <tbody id="cat-detail-rows"></tbody>
          </table>
        </div>
      </div>
    </div>"""

    # 差分テーブル
    diff_rows = ""
    progress_info_html = ""
    if len(cat_trend_months) >= 2:
        last_m = cat_trend_months[-1]
        prev_m = cat_trend_months[-2]
        last_data = cat_trend_by_month.get(last_m, {})
        prev_data = cat_trend_by_month.get(prev_m, {})
        if is_partial_month and progress_ratio and progress_days_total > 0:
            progress_info_html = (
                f'<div style="font-size:0.82rem;color:#636e72;margin:10px 0 8px">'
                f"期間進捗: {progress_ratio * 100:.0f}%（{progress_days_elapsed}/{progress_days_total}日）"
                f" / 着地予測 = 現時点実績 ÷ 期間進捗</div>"
            )
        for cat in cat_trend_categories:
            cur = last_data.get(cat, 0)
            prev = prev_data.get(cat, 0)
            avg = cat_trend_avg_by_category.get(cat, 0)
            diff = cur - prev
            if diff == 0 and cur == 0:
                continue
            vs_prev_now = (cur / prev * 100) if prev > 0 else None
            vs_prev_proj = None
            if prev > 0 and is_partial_month and progress_ratio and progress_ratio > 0:
                projected = cur / progress_ratio
                vs_prev_proj = projected / prev * 100

            diff_sign = "+" if diff > 0 else ""
            diff_color = "color:#e74c3c" if diff > 0 else ("color:#2881D7" if diff < 0 else "")
            now_color = (
                "color:#e74c3c" if vs_prev_now and vs_prev_now > 100 else ("color:#2881D7" if vs_prev_now else "")
            )
            proj_color = (
                "color:#e74c3c"
                if vs_prev_proj and vs_prev_proj > 100
                else ("color:#2881D7" if vs_prev_proj and vs_prev_proj < 100 else "")
            )
            now_text = f"{vs_prev_now:.0f}%" if vs_prev_now is not None else "—"
            proj_text = f"{vs_prev_proj:.0f}%" if vs_prev_proj is not None else "—"
            diff_rows += (
                f'<tr><td>{_h(cat)}</td><td class="num">{cur:,.0f}円</td>'
                f'<td class="num">{prev:,.0f}円</td><td class="num">{avg:,.0f}円</td>'
                f'<td class="num" style="{now_color}">{now_text}</td>'
                f'<td class="num" style="{proj_color}">{proj_text}</td>'
                f'<td class="num" style="{diff_color}">{diff_sign}{diff:,.0f}円</td></tr>'
            )

    cat_trend_html = ""
    if cat_trend_months:
        cat_trend_html = f"""
    <div class="card full" data-card-id="cf-cat-trend">
      <div class="card-header">
        <h2>カテゴリ別月次推移</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <canvas id="cat-trend-chart" height="280"></canvas>
        {
            f'''<h3 style="font-size:0.95rem;margin:16px 0 8px;color:#636e72">{cat_trend_months[-1]} vs {cat_trend_months[-2]} 差分</h3>
        {progress_info_html}
        <table>
          <tr><th>カテゴリ</th><th class="num">当月</th><th class="num">前月</th><th class="num">直近{cat_trend_avg_months}ヶ月平均</th><th class="num">先月比(現時点)</th><th class="num">先月比(着地予測)</th><th class="num">差分</th></tr>
          {diff_rows}
        </table>'''
            if len(cat_trend_months) >= 2
            else ""
        }
      </div>
    </div>"""

    # --- 固定費 vs 変動費データ ---
    fe = data.get("fixed_expenses", {})
    fe_fixed = fe.get("fixed", [])
    fe_fixed_total = fe.get("fixed_total", 0)
    fe_variable_total = fe.get("variable_total", 0)
    fe_ratio = fe.get("fixed_ratio", 0)
    fe_months = fe.get("months_used", 0)

    fe_rows = ""
    for f in fe_fixed:
        fe_rows += (
            f'<tr><td>{_h(f["major"])}</td><td>{_h(f["minor"])}</td><td class="num">{f["avg_amount"]:,.0f}円</td></tr>'
        )

    fe_bar_w = fe_ratio
    fe_html = f"""
    <div class="card" data-card-id="cf-fixed">
      <div class="card-header">
        <h2>固定費 vs 変動費</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div style="display:flex;gap:16px;margin-bottom:16px">
          <div style="flex:1;text-align:center">
            <div style="font-size:0.8rem;color:#636e72">固定費</div>
            <div style="font-size:1.2rem;font-weight:700">{fe_fixed_total:,.0f}円</div>
          </div>
          <div style="flex:1;text-align:center">
            <div style="font-size:0.8rem;color:#636e72">変動費</div>
            <div style="font-size:1.2rem;font-weight:700">{fe_variable_total:,.0f}円</div>
          </div>
          <div style="flex:1;text-align:center">
            <div style="font-size:0.8rem;color:#636e72">固定費率</div>
            <div style="font-size:1.2rem;font-weight:700">{fe_ratio}%</div>
          </div>
        </div>
        <div style="background:#f1f2f6;border-radius:8px;height:28px;overflow:hidden;margin-bottom:16px;display:flex">
          <div style="background:#636e72;width:{fe_bar_w}%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:0.75rem;font-weight:600">{f"固定 {fe_ratio}%" if fe_ratio > 15 else ""}</div>
          <div style="background:#2881D7;flex:1;display:flex;align-items:center;justify-content:center;color:#fff;font-size:0.75rem;font-weight:600">{f"変動 {100 - fe_ratio}%" if (100 - fe_ratio) > 15 else ""}</div>
        </div>
        {'<table><tr><th>大項目</th><th>中項目</th><th class="num">月平均額</th></tr>' + fe_rows + "</table>" if fe_fixed else '<div style="color:#b2bec3;padding:10px 0">固定費データなし（2ヶ月以上のデータが必要です）</div>'}
        <div style="font-size:0.75rem;color:#b2bec3;margin-top:8px">※ 直近{fe_months}ヶ月で毎月出現＆金額ブレ30%以内を固定費と判定</div>
      </div>
    </div>"""

    # --- 収入内訳データ ---
    ib = data.get("income_breakdown", {})
    ib_items = ib.get("items", [])
    ib_total = ib.get("total", 0)

    income_pie_data = json.dumps(
        [
            {"label": item["name"], "value": item["total"], "color": colors[i % len(colors)], "details": []}
            for i, item in enumerate(ib_items)
        ],
        ensure_ascii=False,
    )

    # 収入安定度（CV）
    it = data.get("income_trend", [])
    if len(it) >= 2:
        incomes_list = [d["income"] for d in it]
        avg_inc = sum(incomes_list) / len(incomes_list)
        if avg_inc > 0:
            variance = sum((x - avg_inc) ** 2 for x in incomes_list) / len(incomes_list)
            cv = (variance**0.5) / avg_inc * 100
        else:
            cv = 0
        if cv < 10:
            stability_label = "安定"
            stability_color = "#0F7F30"
        elif cv < 20:
            stability_label = "やや変動"
            stability_color = "#FCAD4C"
        else:
            stability_label = "変動大"
            stability_color = "#DF3727"
        stability_html = f'<div style="margin-top:12px;padding:8px 12px;background:#f8f9fa;border-radius:8px;font-size:0.85rem"><span style="color:#636e72">収入安定度:</span> <strong style="color:{stability_color}">{stability_label}</strong> <span style="color:#b2bec3;font-size:0.75rem">(CV={cv:.1f}%、直近{len(it)}ヶ月)</span></div>'
    else:
        stability_html = ""

    ib_html = f"""
    <div class="card" data-card-id="cf-income">
      <div class="card-header">
        <h2>収入の内訳</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        {'<div class="pie-wrap" style="margin:12px 0"><canvas id="income-pie" width="200" height="200"></canvas><ul class="pie-legend" id="income-legend"></ul></div>' if ib_items else '<div style="color:#b2bec3;padding:10px 0">収入データなし</div>'}
        {f'<div style="text-align:center;font-size:0.9rem;color:#636e72;margin-bottom:8px">収入合計: <strong>{ib_total:,.0f}円</strong></div>' if ib_total else ""}
        {stability_html}
      </div>
    </div>"""

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><circle cx='50' cy='50' r='45' fill='%232881D7'/><path d='M50 5A45 45 0 0 1 95 50L50 50Z' fill='%23FCAD4C'/><path d='M50 5A45 45 0 0 0 10.2 72.5L50 50Z' fill='%230F7F30'/></svg>">
<title>家計簿分析 - {year_month}</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f6fa; color: #2d3436; line-height: 1.6; }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 20px; }}
  {_NAV_CSS}
  h1 {{ font-size: 1.5rem; }}
  .month-picker {{ display: flex; align-items: center; gap: 8px; margin-bottom: 20px; }}
  .month-picker select {{
    font-size: 0.9rem; padding: 4px 8px; border: 1px solid #dfe6e9;
    border-radius: 6px; background: #fff; cursor: pointer;
  }}
  .month-picker .nav-btn {{
    background: #fff; border: 1px solid #dfe6e9; border-radius: 6px;
    padding: 4px 10px; cursor: pointer; font-size: 0.9rem; color: #2d3436;
  }}
  .month-picker .nav-btn:hover {{ background: #f1f2f6; }}
  .month-picker .nav-btn:disabled {{ color: #b2bec3; cursor: default; background: #fff; }}
  .summary-cards {{ display: flex; gap: 12px; margin-bottom: 20px; }}
  .summary-card {{ flex: 1; background: #fff; border-radius: 12px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); text-align: center; }}
  .summary-card h3 {{ font-size: 0.85rem; color: #636e72; margin-bottom: 6px; font-weight: 600; }}
  .summary-card .amount {{ font-size: 1.3rem; font-weight: 700; }}
  .summary-card .sub-info {{ font-size: 0.75rem; color: #636e72; margin-top: 4px; }}
  .grid {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 20px; align-items: flex-start; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); width: calc(50% - 10px); }}
  .card-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 0; }}
  .card-header h2 {{ font-size: 1.1rem; color: #2d3436; margin: 0; }}
  .full {{ width: 100%; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
  th {{ text-align: left; padding: 8px 6px; border-bottom: 2px solid #dfe6e9; color: #636e72; font-weight: 600; }}
  td {{ padding: 6px; border-bottom: 1px solid #f1f2f6; }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; }}
  .bar {{ height: 16px; border-radius: 3px; min-width: 2px; }}
  .plus {{ color: #e74c3c; }}
  .minus {{ color: #2881D7; }}
  .pie-wrap canvas {{ max-width: 280px; }}
  canvas {{ margin: 0 auto; display: block; }}
  .pie-wrap {{ display: flex; align-items: center; gap: 20px; position: relative; }}
  .pie-tooltip {{
    position: fixed; pointer-events: none; z-index: 9999;
    background: rgba(45,52,54,0.92); color: #fff; border-radius: 8px;
    padding: 8px 14px; font-size: 0.82rem; line-height: 1.5;
    white-space: nowrap; opacity: 0; transition: opacity 0.15s ease;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }}
  .pie-tooltip.show {{ opacity: 1; }}
  .has-tip {{ cursor: pointer; }}
  .has-tip:hover {{ background: #f0f4ff; }}
  .pie-legend {{ font-size: 0.85rem; }}
  .pie-legend li {{ list-style: none; margin-bottom: 4px; }}
  .cat-detail-table-wrap {{ max-height: 280px; overflow: auto; border: 1px solid #f1f2f6; border-radius: 8px; }}
  .cat-detail-table {{ table-layout: fixed; width: 100%; min-width: 760px; }}
  .cat-detail-table th, .cat-detail-table td {{ vertical-align: middle; }}
  .cat-detail-table .col-month {{ width: 78px; white-space: nowrap; }}
  .cat-detail-table .col-date {{ width: 68px; white-space: nowrap; }}
  .cat-detail-table .col-minor {{ width: 84px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .cat-detail-table .col-inst {{ width: 140px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .cat-detail-table .col-amount {{ width: 98px; white-space: nowrap; }}
  .cat-detail-table .col-desc {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .dl-btn {{
    padding: 3px 10px; border: 1px solid #2881D7; border-radius: 4px;
    background: #fff; color: #2881D7; font-size: 0.8rem; cursor: pointer;
  }}
  .dl-btn:hover {{ background: #2881D7; color: #fff; }}
  .dl-btn:disabled {{ border-color: #b2bec3; color: #b2bec3; cursor: default; background: #fff; }}
  .ai-comment-card {{
    display: flex; gap: 12px; align-items: flex-start;
    background: linear-gradient(135deg, #f0f7ff 0%, #f5f0ff 100%);
    border: 1px solid #d0d7f7; border-radius: 12px;
    padding: 14px 18px; margin-bottom: 16px;
    font-size: 0.88rem; line-height: 1.6; color: #2d3436;
  }}
  .ai-icon {{
    background: #2881D7; color: #fff; font-weight: 800; font-size: 0.75rem;
    border-radius: 6px; padding: 3px 7px; flex-shrink: 0;
  }}
  .budget-cell {{ cursor: pointer; color: #636e72; }}
  .budget-cell:hover {{ background: #f0f4ff; }}
  .budget-cell input {{
    width: 90px; padding: 2px 4px; border: 1px solid #2881D7; border-radius: 4px;
    font-size: 0.85rem; text-align: right; outline: none;
  }}
  .budget-bar-bg {{
    display: inline-block; width: 80px; height: 12px; background: #f1f2f6;
    border-radius: 6px; overflow: hidden; vertical-align: middle;
  }}
  .budget-bar {{ height: 100%; border-radius: 6px; transition: width 0.3s; }}
  .budget-pct {{ font-size: 0.78rem; margin-left: 4px; font-weight: 600; }}
  .progress-cell {{ white-space: nowrap; }}
  {_COLLAPSE_CSS}
  {_RESPONSIVE_CSS}
</style>
</head>
<body>
<div class="container">
  <div class="page-header">
    <h1>家計簿分析</h1>
    {_nav_html("/cf")}
  </div>
  <div class="month-picker">
    <button class="nav-btn" id="prev-month" title="前の月">&larr;</button>
    <select id="month-select" onchange="location.href='/cf?month='+this.value">
      {month_options}
    </select>
    <button class="nav-btn" id="next-month" title="次の月">&rarr;</button>
    {
        f'<span style="font-size:0.75rem;color:#636e72;margin-left:8px">（毎月{closing_day}日〜）</span>'
        if closing_day > 1
        else ""
    }
  </div>
  <div class="summary-cards">
    <div class="summary-card">
      <h3>支出合計</h3>
      <div class="amount" style="color:#2881D7">{total_expense:,.0f}円</div>
    </div>
    <div class="summary-card">
      <h3>収入合計</h3>
      <div class="amount" style="color:#e74c3c">{total_income:,.0f}円</div>
    </div>
    <div class="summary-card">
      <h3>収支</h3>
      <div class="amount {balance_css}">{balance_sign}{balance:,.0f}円</div>
    </div>
    {budget_total_html}
    {budget_remaining_html}
  </div>
  {
        f'<div class="ai-comment-card"><div class="ai-icon">AI</div><div class="ai-text">{ai_comment}</div></div>'
        if ai_comment
        else ""
    }
  {
        '<div style="background:#FFF8E1;border:1px solid #FFD54F;border-radius:8px;padding:8px 14px;margin-bottom:16px;font-size:0.85rem;color:#795548">&#x26A0; 当月はまだ途中のデータです。給与など月末に反映される項目が含まれていない場合があります。</div>'
        if is_partial_month
        else ""
    }
  <div class="grid">
    <div class="card" data-card-id="cf-category">
      <div class="card-header">
        <h2>カテゴリ別支出</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div class="pie-wrap" style="margin:12px 0">
          <canvas id="cf-pie" width="220" height="220"></canvas>
          <ul class="pie-legend" id="cf-legend"></ul>
        </div>
        <table>
          <tr><th>カテゴリ</th><th class="num">金額</th><th class="num">予算</th><th>消化率</th></tr>
          {cat_rows}
        </table>
      </div>
    </div>

    <div class="card" data-card-id="cf-top">
      <div class="card-header">
        <h2>{top_title}</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        {top_note}
        <table>
          <tr><th>日付</th><th>内容</th><th class="num">金額</th><th>カテゴリ</th><th>金融機関</th></tr>
          {top_rows}
        </table>
      </div>
    </div>

    {cat_details_html}

    {cat_trend_html}

    <div class="card full" data-card-id="cf-trend">
      <div class="card-header">
        <h2>月別支出推移</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        {
        '<canvas id="trend-chart" height="200"></canvas>'
        if trend
        else '<div style="color:#b2bec3;padding:20px 0">推移データがありません</div>'
    }
      </div>
    </div>

    {fe_html}

    {ib_html}

    <div class="card full" data-card-id="cf-download">
      <div class="card-header">
        <h2>過去月ダウンロード管理</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <select id="manual-year" style="padding:4px 8px;border:1px solid #dfe6e9;border-radius:6px;font-size:0.9rem">{
        year_options
    }</select>
          <select id="manual-month-sel" style="padding:4px 8px;border:1px solid #dfe6e9;border-radius:6px;font-size:0.9rem">{
        month_options
    }</select>
          <button class="dl-btn" onclick="fetchManualMonth()">取得</button>
          <span id="manual-msg" style="font-size:0.8rem;color:#636e72"></span>
        </div>
        <table>
          <tr><th>月</th><th>ステータス</th><th></th></tr>
          {dl_rows}
        </table>
      </div>
    </div>
  </div>
</div>
<div class="pie-tooltip" id="pie-tooltip"></div>

<script>
{_ESC_JS}
{_PIE_JS}

const cfPieData = {pie_data};
drawPieChart('cf-pie', 'cf-legend', cfPieData, 220);

// 収入内訳円グラフ
const incomePieData = {income_pie_data};
drawPieChart('income-pie', 'income-legend', incomePieData, 200);

// カテゴリ別月次推移（積み上げ棒グラフ）
const catTrendData = {cat_trend_json};
const catTrendCanvas = document.getElementById('cat-trend-chart');
if (catTrendData.months.length > 0 && catTrendCanvas) {{
  const ctx2 = catTrendCanvas.getContext('2d');
  const W2 = catTrendCanvas.parentElement.clientWidth - 40;
  catTrendCanvas.width = W2;
  catTrendCanvas.height = 300;
  catTrendCanvas.style.maxWidth = 'none';

  const cMonths = catTrendData.months;
  const cCats = catTrendData.categories;
  const cByMonth = catTrendData.by_month;
  const stackColors = {json.dumps(colors)};

  // 各月の合計を計算
  let maxStack = 0;
  cMonths.forEach(m => {{
    let total = 0;
    cCats.forEach(c => {{ total += (cByMonth[m] || {{}})[c] || 0; }});
    if (total > maxStack) maxStack = total;
  }});
  maxStack *= 1.1;

  const p2 = {{ left: 70, right: 20, top: 40, bottom: 30 }};
  const cW = W2 - p2.left - p2.right;
  const cH = 300 - p2.top - p2.bottom;
  const bGW = cW / cMonths.length;
  const bW2 = bGW * 0.6;

  // Y軸
  ctx2.strokeStyle = '#f1f2f6';
  ctx2.fillStyle = '#b2bec3';
  ctx2.font = '11px sans-serif';
  ctx2.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {{
    const y = p2.top + cH * (1 - i/4);
    const val = maxStack * i / 4;
    ctx2.beginPath();
    ctx2.moveTo(p2.left, y);
    ctx2.lineTo(W2 - p2.right, y);
    ctx2.stroke();
    ctx2.fillText((val/10000).toFixed(0) + '万', p2.left - 6, y + 4);
  }}

  // 積み上げ棒グラフ
  cMonths.forEach((m, mi) => {{
    const x = p2.left + mi * bGW + (bGW - bW2) / 2;
    let cumH = 0;
    cCats.forEach((c, ci) => {{
      const val = (cByMonth[m] || {{}})[c] || 0;
      const h = (val / maxStack) * cH;
      ctx2.fillStyle = stackColors[ci % stackColors.length];
      ctx2.fillRect(x, p2.top + cH - cumH - h, bW2, h);
      cumH += h;
    }});
    ctx2.fillStyle = '#636e72';
    ctx2.font = '11px sans-serif';
    ctx2.textAlign = 'center';
    ctx2.fillText(m.substring(5), x + bW2/2, p2.top + cH + 18);
  }});

  // 凡例（上位6カテゴリ）
  const legendCats = cCats.slice(0, 6);
  let lx = p2.left;
  legendCats.forEach((c, ci) => {{
    ctx2.fillStyle = stackColors[ci % stackColors.length];
    ctx2.fillRect(lx, 6, 10, 10);
    ctx2.fillStyle = '#2d3436';
    ctx2.font = '11px sans-serif';
    ctx2.textAlign = 'left';
    ctx2.fillText(c, lx + 13, 15);
    lx += ctx2.measureText(c).width + 24;
  }});
  if (cCats.length > 6) {{
    ctx2.fillStyle = '#b2bec3';
    ctx2.fillText('…他' + (cCats.length - 6) + '件', lx, 15);
  }}
}}

// カテゴリ別支出の詳細（直近Nヶ月）
const catDetailData = {cat_detail_json};
const catDetailSelect = document.getElementById('cat-detail-select');
const catDetailSort = document.getElementById('cat-detail-sort');
const catDetailRows = document.getElementById('cat-detail-rows');

function renderCategoryDetailRows(category, sortMode) {{
  if (!catDetailRows) return;
  const rows = [ ...((catDetailData.by_category || {{}})[category] || []) ];
  if (sortMode === 'date_desc') {{
    rows.sort((a, b) => (b.date || '').localeCompare(a.date || '') || Number(b.amount || 0) - Number(a.amount || 0));
  }} else if (sortMode === 'date_asc') {{
    rows.sort((a, b) => (a.date || '').localeCompare(b.date || '') || Number(b.amount || 0) - Number(a.amount || 0));
  }} else {{
    // default: fiscal month desc -> amount desc -> date desc
    rows.sort((a, b) =>
      (b.year_month || '').localeCompare(a.year_month || '') ||
      Number(b.amount || 0) - Number(a.amount || 0) ||
      (b.date || '').localeCompare(a.date || '')
    );
  }}
  if (!rows.length) {{
    catDetailRows.innerHTML = '<tr><td colspan="6" style="color:#999">明細データがありません</td></tr>';
    return;
  }}
  catDetailRows.innerHTML = rows.map(r => {{
    const dt = (r.date || '').slice(5);
    const inst = r.institution || '';
    const desc = r.description || '';
    const minor = r.minor_category || '';
    return '<tr>' +
      '<td class="col-month">' + esc(r.year_month || '') + '</td>' +
      '<td class="col-date">' + esc(dt) + '</td>' +
      '<td class="col-desc" title="' + esc(desc) + '">' + esc(desc) + '</td>' +
      '<td class="col-minor" title="' + esc(minor) + '">' + esc(minor) + '</td>' +
      '<td class="col-inst" style="color:#636e72;font-size:0.82rem" title="' + esc(inst) + '">' + esc(inst) + '</td>' +
      '<td class="num col-amount">' + Number(r.amount || 0).toLocaleString('ja-JP') + '円</td>' +
      '</tr>';
  }}).join('');
}}

if (catDetailSelect && catDetailRows && catDetailSort) {{
  renderCategoryDetailRows(catDetailSelect.value, catDetailSort.value);
  catDetailSelect.addEventListener('change', () => renderCategoryDetailRows(catDetailSelect.value, catDetailSort.value));
  catDetailSort.addEventListener('change', () => renderCategoryDetailRows(catDetailSelect.value, catDetailSort.value));
}}

// 月ナビゲーション
const msel = document.getElementById('month-select');
const months = Array.from(msel.options).map(o => o.value);
const midx = msel.selectedIndex;
const prevM = document.getElementById('prev-month');
const nextM = document.getElementById('next-month');
nextM.disabled = midx === 0;
prevM.disabled = midx === months.length - 1;
prevM.onclick = () => {{ if (midx < months.length - 1) location.href = '/cf?month=' + months[midx + 1]; }};
nextM.onclick = () => {{ if (midx > 0) location.href = '/cf?month=' + months[midx - 1]; }};

// 月別推移棒グラフ
const trendData = {trend_data};
const trendCanvas = document.getElementById('trend-chart');
if (trendData.length > 0 && trendCanvas) {{
  const ctx = trendCanvas.getContext('2d');
  const W = trendCanvas.parentElement.clientWidth - 40;
  trendCanvas.width = W;
  trendCanvas.height = 220;

  const labels = trendData.map(d => d.year_month.substring(5));
  const incomes = trendData.map(d => d.income);
  const expenses = trendData.map(d => d.expense);
  const maxVal = Math.max(...incomes, ...expenses) * 1.15;

  const padding = {{ left: 70, right: 20, top: 20, bottom: 30 }};
  const chartW = W - padding.left - padding.right;
  const chartH = 220 - padding.top - padding.bottom;
  const barGroupW = chartW / trendData.length;
  const barW = barGroupW * 0.3;

  // Y軸
  ctx.strokeStyle = '#f1f2f6';
  ctx.fillStyle = '#b2bec3';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {{
    const y = padding.top + chartH * (1 - i/4);
    const val = maxVal * i / 4;
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(W - padding.right, y);
    ctx.stroke();
    ctx.fillText((val/10000).toFixed(0) + '万', padding.left - 6, y + 4);
  }}

  // 棒グラフ
  trendData.forEach((d, i) => {{
    const x = padding.left + i * barGroupW + barGroupW * 0.1;
    const iH = (d.income / maxVal) * chartH;
    const eH = (d.expense / maxVal) * chartH;
    ctx.fillStyle = '#e74c3c';
    ctx.fillRect(x, padding.top + chartH - iH, barW, iH);
    ctx.fillStyle = '#2881D7';
    ctx.fillRect(x + barW + 2, padding.top + chartH - eH, barW, eH);
    ctx.fillStyle = '#636e72';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(labels[i], x + barW + 1, padding.top + chartH + 18);
  }});

  // 凡例
  ctx.fillStyle = '#e74c3c';
  ctx.fillRect(padding.left, 4, 12, 12);
  ctx.fillStyle = '#2d3436';
  ctx.font = '11px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillText('収入', padding.left + 16, 14);
  ctx.fillStyle = '#2881D7';
  ctx.fillRect(padding.left + 55, 4, 12, 12);
  ctx.fillStyle = '#2d3436';
  ctx.fillText('支出', padding.left + 71, 14);
}}

// ダウンロード
async function downloadMonth(ym, btn) {{
  btn.disabled = true;
  btn.textContent = '取得中...';
  try {{
    const r = await fetch('/api/cf/download', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{year_month: ym}})
    }});
    const result = await r.json();
    if (result.ok) {{
      btn.textContent = '完了';
      setTimeout(() => location.reload(), 1000);
    }} else {{
      btn.textContent = 'エラー';
      btn.disabled = false;
    }}
  }} catch(e) {{
    btn.textContent = 'エラー';
    btn.disabled = false;
  }}
}}

async function fetchManualMonth() {{
  const year = document.getElementById('manual-year').value;
  const month = document.getElementById('manual-month-sel').value;
  const msg = document.getElementById('manual-msg');
  const ym = year + '-' + month;
  if (!year || !month) {{ msg.textContent = '年月を選択してください'; return; }}
  // YYYY-MM format validation
  const today = new Date();
  const sel = new Date(ym + '-01');
  if (sel > today) {{ msg.textContent = '未来の月は取得できません'; return; }}
  msg.textContent = '取得中...';
  msg.style.color = '#636e72';
  try {{
    const r = await fetch('/api/cf/download', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{year_month: ym}})
    }});
    const result = await r.json();
    if (result.ok) {{
      msg.textContent = '取得完了';
      msg.style.color = '#0F7F30';
      setTimeout(() => location.reload(), 1000);
    }} else {{
      msg.textContent = result.error || 'エラーが発生しました';
      msg.style.color = '#DF3727';
    }}
  }} catch(e) {{
    msg.textContent = 'エラーが発生しました';
    msg.style.color = '#DF3727';
  }}
}}

// トースト通知
function showToast(msg, isError) {{
  const t = document.createElement('div');
  t.textContent = msg;
  t.style.cssText = 'position:fixed;top:20px;right:20px;padding:10px 20px;border-radius:8px;color:#fff;font-size:0.85rem;font-weight:600;z-index:9999;transition:opacity 0.3s;background:' + (isError ? '#DF3727' : '#0F7F30');
  document.body.appendChild(t);
  setTimeout(() => {{ t.style.opacity = '0'; setTimeout(() => t.remove(), 300); }}, 3000);
}}

// 予算クリック編集
document.querySelectorAll('.budget-cell').forEach(cell => {{
  cell.addEventListener('click', function(e) {{
    if (this.querySelector('input')) return;
    const cat = this.dataset.category;
    const amt = parseInt(this.dataset.amount) || '';
    const orig = this.innerHTML;
    const input = document.createElement('input');
    input.type = 'number';
    input.value = amt;
    input.placeholder = '予算額';
    let saving = false;
    this.textContent = '';
    this.appendChild(input);
    input.focus();
    input.select();
    const save = async () => {{
      if (saving) return;
      if (!cell.contains(input)) return;
      saving = true;
      const val = parseInt(input.value) || 0;
      try {{
        const res = await fetch('/api/cf/budget', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{category: cat, amount: val}})
        }});
        const result = await res.json();
        if (!res.ok || !result.ok) {{
          throw new Error(result.error || 'サーバーエラー');
        }}
        cell.dataset.amount = val;
        if (val > 0) {{
          cell.textContent = val.toLocaleString('ja-JP') + '円';
        }} else {{
          cell.textContent = '\u2014';
        }}
        // 消化率バーを更新
        const progressCell = cell.nextElementSibling;
        const amountText = cell.previousElementSibling.textContent;
        const actual = parseInt(amountText.replace(/[^0-9]/g, '')) || 0;
        if (val > 0) {{
          const pct = actual / val * 100;
          const barW = Math.min(pct, 100);
          const barColor = pct < 80 ? '#2881D7' : pct <= 100 ? '#FFD54F' : '#DF3727';
          progressCell.innerHTML = '<div class="budget-bar-bg"><div class="budget-bar" style="width:' + barW + '%;background:' + barColor + '"></div></div><span class="budget-pct" style="color:' + barColor + '">' + Math.round(pct) + '%</span>';
        }} else {{
          progressCell.innerHTML = '';
        }}
        // 予算残りサマリー更新
        updateBudgetRemaining();
      }} catch(err) {{
        cell.innerHTML = orig;
        showToast('予算の保存に失敗しました: ' + err.message, true);
      }}
    }};
    input.addEventListener('keydown', function(ev) {{
      if (ev.key === 'Enter') {{ ev.preventDefault(); save(); }}
      if (ev.key === 'Escape') {{ cell.innerHTML = orig; }}
    }});
    input.addEventListener('blur', save);
  }});
}});

function updateBudgetRemaining() {{
  const cells = document.querySelectorAll('.budget-cell');
  let budgetTotal = 0, actualTotal = 0;
  cells.forEach(c => {{
    const amt = parseInt(c.dataset.amount) || 0;
    if (amt > 0) {{
      budgetTotal += amt;
      const actualText = c.previousElementSibling.textContent;
      actualTotal += parseInt(actualText.replace(/[^0-9]/g, '')) || 0;
    }}
  }});
  const card = document.querySelector('[data-testid="budget-remaining"]');
  if (card) {{
    const remaining = budgetTotal - actualTotal;
    const amountEl = card.querySelector('.amount');
    const sign = remaining >= 0 ? '+' : '';
    amountEl.textContent = sign + remaining.toLocaleString('ja-JP') + '円';
    amountEl.style.color = remaining >= 0 ? '#0F7F30' : '#DF3727';
    card.style.display = budgetTotal > 0 ? '' : 'none';
  }}
  const totalCard = document.querySelector('[data-testid="budget-total"]');
  if (totalCard) {{
    const totalAmountEl = totalCard.querySelector('.amount');
    totalAmountEl.textContent = budgetTotal.toLocaleString('ja-JP') + '円';
    totalCard.style.display = budgetTotal > 0 ? '' : 'none';
    const subInfo = totalCard.querySelector('.sub-info');
    if (subInfo) {{
      const incomeText = document.querySelectorAll('.summary-card')[1]?.querySelector('.amount')?.textContent || '';
      const income = parseInt(incomeText.replace(/[^0-9]/g, '')) || 0;
      if (income > 0 && budgetTotal > 0) {{
        subInfo.textContent = '収入の ' + Math.round(budgetTotal / income * 100) + '%';
        subInfo.style.display = '';
      }} else {{
        subInfo.style.display = 'none';
      }}
    }}
  }}
}}

{_COLLAPSE_JS}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    db_path: str = str(DB_DEFAULT)
    demo: bool = False
    skip_update: bool = False
    portfolio_api_token: str = os.environ.get("PORTFOLIO_API_TOKEN", "")
    screener_base_url: str = os.environ.get("SCREENER_BASE_URL", "")

    def _send_html(self, html: str) -> None:
        """HTMLレスポンスを送信する。デモモード時はバナーを挿入。"""
        if self.demo:
            html = html.replace("<body>", "<body>" + _DEMO_BANNER, 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _send_private_json(self, payload: dict, status: int = 200) -> None:
        """キャッシュ・別オリジン共有を許可しないJSONレスポンスを送る。"""
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/api/status":
            payload = {
                "updating": _update_state["running"],
                "version": _update_state["version"],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())

        elif parsed.path == "/api/portfolio-context":
            if not self.portfolio_api_token:
                self._send_private_json({"error": "portfolio api is not configured"}, status=503)
            else:
                supplied = self.headers.get("Authorization", "")
                expected = f"Bearer {self.portfolio_api_token}"
                if not hmac.compare_digest(supplied, expected):
                    self._send_private_json({"error": "unauthorized"}, status=401)
                else:
                    context = _get_portfolio_context(self.db_path)
                    if context is None:
                        self._send_private_json({"error": "portfolio data unavailable"}, status=404)
                    else:
                        self._send_private_json(context)

        elif parsed.path == "/api/data":
            if self.demo:
                data = _demo_data()
            else:
                date = params.get("date", [None])[0]
                data = _get_data(self.db_path, date)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

        elif parsed.path == "/api/dates":
            if self.demo:
                dates = [_demo_data()["date"]]
            else:
                dates = _get_dates(self.db_path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(dates).encode())

        elif parsed.path == "/allocation":
            custom = None
            error = None
            allocation_keys = ("cash", "fund", "jp_stock", "us_stock")
            if any(key in params for key in allocation_keys):
                try:
                    custom = {key: float(params.get(key, ["0"])[0]) for key in allocation_keys}
                    if abs(sum(custom.values()) - 100) > 0.01:
                        raise ValueError("配分の合計は100%にしてください")
                    if any(value < 0 or value > 100 for value in custom.values()):
                        raise ValueError("配分は0〜100%で指定してください")
                except (TypeError, ValueError) as exc:
                    error = str(exc)
                    custom = None
            data = _demo_allocation_data(custom) if self.demo else _get_allocation_data(self.db_path, custom)
            self._send_html(_build_allocation_html(data, error=error))

        elif parsed.path == "/plan":
            # contrib パラメータがあれば設定に保存、なければDB設定を使用
            contrib = None
            if "contrib" in params:
                with contextlib.suppress(ValueError, TypeError):
                    contrib = float(params["contrib"][0])
            if self.demo:
                data = _demo_plan_data()
                ai_comment = "直近6ヶ月で資産は約1,970万円から2,150万円へ着実に増加しており、月平均+30万円の成長ペースです。月次収支は概ね黒字を維持していますが、12月のように支出が膨らむ月もあるため、臨時出費への備えも意識しましょう。モンテカルロ・シミュレーションでは、月5万円の積立を継続した場合、5年後の中央値は約3,120万円と見込まれ、長期的な資産形成は順調と言えます。"
            else:
                data = _get_plan_data(self.db_path, contrib)
                ai_comment = None
                if data:
                    try:
                        conn = get_connection(self.db_path)
                        try:
                            ai_comment = get_comment(conn, data["date"], "lifeplan")
                        finally:
                            conn.close()
                    except Exception:
                        pass
            html = _build_plan_html(data, self.skip_update, ai_comment=ai_comment)
            self._send_html(html)

        elif parsed.path == "/simulator":
            if self.demo:
                data = _demo_simulator_data()
            else:
                data = _get_simulator_data(self.db_path)
            html = _build_simulator_html(data, self.skip_update)
            self._send_html(html)

        elif parsed.path == "/":
            if self.demo:
                data = _demo_data()
                dates = [data["date"]]
                ai_comment = "総資産約2,150万円のポートフォリオは、株式・投資信託・預金・年金にバランスよく分散されています。前日比+4.2万円、前月比+28.5万円と堅調に推移しており、特にリスク資産（株式+投信）の貢献が大きいです。年間配当予測は約12.5万円（利回り約1.9%）で、高配当銘柄の追加や業種の偏り（電気機器が大きい）の分散を検討すると、より安定したポートフォリオになるでしょう。"
            else:
                date = params.get("date", [None])[0]
                dates = _get_dates(self.db_path)
                data = _get_data(self.db_path, date)
                ai_comment = None
                if data:
                    try:
                        conn = get_connection(self.db_path)
                        try:
                            ai_comment = get_comment(conn, data["date"], "dashboard")
                        finally:
                            conn.close()
                    except Exception:
                        pass
            # セッション切れチェック + 取得日時ステータス
            session_expired = None
            last_fetch_at = None
            next_run_at = None
            if self.demo:
                # デモモード: ?session_expired=1 で強制表示（見た目確認用）
                if params.get("session_expired", [""])[0]:
                    session_expired = "demo"
                last_fetch_at = f"{data['date']} 07:00"
                next_run_at = "明日 07:00"
            else:
                try:
                    conn = get_connection(self.db_path)
                    try:
                        session_expired = get_setting(conn, "session_expired")
                        last_fetch_raw = get_setting(conn, "last_fetch_at")
                        scheduler_enabled = get_setting(conn, "scheduler_enabled", "1") != "0"
                        scheduler_time = get_setting(conn, "scheduler_time", _SCHEDULER_DEFAULT_TIME)
                    finally:
                        conn.close()
                    if last_fetch_raw:
                        with contextlib.suppress(ValueError):
                            last_fetch_at = datetime.fromisoformat(last_fetch_raw).strftime("%Y-%m-%d %H:%M")
                    if self.skip_update or not scheduler_enabled:
                        next_run_at = "オフ"
                    else:
                        next_run_at = _next_scheduled_run(datetime.now(), scheduler_time).strftime("%m-%d %H:%M")
                except Exception:
                    pass
            html = _build_html(
                data,
                dates,
                self.skip_update,
                ai_comment=ai_comment,
                session_expired=session_expired,
                last_fetch_at=last_fetch_at,
                next_run_at=next_run_at,
                screener_base_url=self.screener_base_url,
            )
            self._send_html(html)

        elif parsed.path == "/cf":
            month = params.get("month", [None])[0]
            if self.demo:
                data = _demo_cf_data()
                ai_comment = "今月の支出は食費と日用品が予算を若干上回っていますが、全体では収支プラスを維持しています。固定費率は約40%と標準的で、通信費や保険の見直し余地があります。来月は食費の予算管理を意識すると、さらに貯蓄率を改善できるでしょう。"
                if month:
                    data["year_month"] = month
            else:
                data = _get_cf_data(self.db_path, month)
                ai_comment = None
                if data:
                    try:
                        conn = get_connection(self.db_path)
                        try:
                            ai_comment = get_comment(conn, data["year_month"], "cf")
                        finally:
                            conn.close()
                    except Exception:
                        pass
            html = _build_cf_html(data, self.skip_update, ai_comment=ai_comment)
            self._send_html(html)

        elif parsed.path == "/api/cf/months":
            if self.demo:
                data = _demo_cf_data()
                result = data.get("available_months", [])
            else:
                conn = get_connection(self.db_path)
                try:
                    closing_day = int(get_setting(conn, "closing_day", "1") or "1")
                    holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"
                    result = get_cf_available_months(conn, closing_day=closing_day, holiday_mode=holiday_mode)
                finally:
                    conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

        elif parsed.path == "/settings":
            saved = params.get("saved", [None])[0]
            html = _build_settings_html(self.db_path, saved=saved, skip_update=self.skip_update)
            self._send_html(html)

        elif parsed.path == "/api/export/snapshots":
            fmt = params.get("format", ["csv"])[0]
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT date, total_asset, by_class_json FROM snapshots ORDER BY date ASC"
                ).fetchall()
            finally:
                conn.close()
            if fmt == "json":
                data = [{"date": r[0], "total_asset": r[1], "by_class": json.loads(r[2])} for r in rows]
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="snapshots.json"')
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())
            else:
                import csv
                import io

                buf = io.StringIO()
                writer = csv.writer(buf)
                # ヘッダー: date, total_asset + 動的な資産クラス列
                all_classes: list[str] = []
                parsed_rows = []
                for r in rows:
                    by_class = json.loads(r[2])
                    parsed_rows.append((r[0], r[1], by_class))
                    for cls in by_class:
                        if cls not in all_classes:
                            all_classes.append(cls)
                writer.writerow(["date", "total_asset"] + all_classes)
                for date_str, total, by_class in parsed_rows:
                    writer.writerow([date_str, total] + [by_class.get(cls, 0) for cls in all_classes])
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="snapshots.csv"')
                self.end_headers()
                self.wfile.write(buf.getvalue().encode("utf-8-sig"))

        elif parsed.path == "/api/export/cashflows":
            fmt = params.get("format", ["csv"])[0]
            conn = get_connection(self.db_path)
            try:
                rows = conn.execute(
                    "SELECT year_month, income, expense FROM monthly_cashflows ORDER BY year_month ASC"
                ).fetchall()
            finally:
                conn.close()
            if fmt == "json":
                data = [{"year_month": r[0], "income": r[1], "expense": r[2], "balance": r[1] - r[2]} for r in rows]
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="cashflows.json"')
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())
            else:
                import csv
                import io

                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(["year_month", "income", "expense", "balance"])
                for r in rows:
                    writer.writerow([r[0], r[1], r[2], r[1] - r[2]])
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="cashflows.csv"')
                self.end_headers()
                self.wfile.write(buf.getvalue().encode("utf-8-sig"))

        elif parsed.path == "/api/export/cf":
            ym = params.get("month", [None])[0]
            fmt = params.get("format", ["csv"])[0]
            conn = get_connection(self.db_path)
            try:
                if ym:
                    rows = conn.execute(
                        "SELECT date, description, amount, major_category, minor_category, institution, memo FROM cf_transactions WHERE year_month = ? AND is_transfer = 0 ORDER BY date ASC",
                        (ym,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT date, description, amount, major_category, minor_category, institution, memo FROM cf_transactions WHERE is_transfer = 0 ORDER BY date ASC"
                    ).fetchall()
            finally:
                conn.close()
            columns = ["date", "description", "amount", "major_category", "minor_category", "institution", "memo"]
            if fmt == "json":
                data = [dict(zip(columns, r, strict=True)) for r in rows]
                fname = f"cf_{ym}.json" if ym else "cf_all.json"
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.end_headers()
                self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode())
            else:
                import csv
                import io

                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(columns)
                writer.writerows(rows)
                fname = f"cf_{ym}.csv" if ym else "cf_all.csv"
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                self.end_headers()
                self.wfile.write(buf.getvalue().encode("utf-8-sig"))

        elif parsed.path == "/api/ai-prompt":
            prompt_type = params.get("type", ["asset"])[0]
            text = self._build_ai_prompt(prompt_type)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(text.encode())

        elif parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>404 Not Found</h1></body></html>")

    def _build_ai_prompt(self, prompt_type: str) -> str:
        """AIチャット用のMarkdownプロンプトを生成する。"""
        # simulator は _get_simulator_data が内部で接続を管理するため先に分岐
        if prompt_type == "simulator":
            return self._ai_prompt_simulator()
        if prompt_type == "allocation":
            return self._ai_prompt_allocation()
        conn = get_connection(self.db_path)
        try:
            if prompt_type == "all":
                return self._ai_prompt_all(conn)
            elif prompt_type == "asset":
                return self._ai_prompt_asset(conn)
            elif prompt_type == "cf":
                return self._ai_prompt_cf(conn)
            elif prompt_type == "plan":
                return self._ai_prompt_plan(conn)
            else:
                return "不明なタイプです。"
        finally:
            conn.close()

    def _ai_prompt_allocation(self) -> str:
        """余剰資金の配分比較をAIへ相談するMarkdownを返す。"""
        data = _demo_allocation_data() if self.demo else _get_allocation_data(self.db_path)
        if not data:
            return "資産データがありません。"
        context = data["context"]
        detail = context["investable_detail"]
        scheduled_card_payment_total = float(detail.get("scheduled_card_payment_total", 0))
        lines = [
            f"# 余剰資金の配分シナリオ（{context['as_of']}時点）",
            "",
            f"- 投資可能額: **{context['investable_cash']:,.0f}円**",
            f"- 預金・現金: {detail['cash_balance']:,.0f}円",
            f"- 生活防衛資金: {detail['emergency_fund']:,.0f}円",
            f"- 予定支出: {detail['planned_expenses']:,.0f}円",
            f"- カード引き落とし予定: {scheduled_card_payment_total:,.0f}円",
            "",
            "## 比較案",
            "",
            "| 案 | 現金 | 投資信託 | 日本株 | 米国株 |",
            "|---|---:|---:|---:|---:|",
        ]
        for scenario in data["presets"]:
            amounts = scenario["allocation_amounts"]
            lines.append(
                f"| {scenario['name']} | {amounts['cash']:,.0f}円 | {amounts['fund']:,.0f}円 | "
                f"{amounts['jp_stock']:,.0f}円 | {amounts['us_stock']:,.0f}円 |"
            )
        lines += [
            "",
            "---",
            "",
            "投資信託・日本株・米国株・現金のどこへ振るか相談したいです。",
            "現在の資産構成、地域分散、集中リスク、予定支出を踏まえ、各案の長所・短所を比較してください。",
            "断定ではなく、追加で確認すべき条件と推奨額の範囲を示してください。",
        ]
        return "\n".join(lines)

    def _ai_prompt_all(self, conn: sqlite3.Connection) -> str:
        """AI相談用データ（全種類）を1つのMarkdownにまとめる。"""

        def _strip_section_instruction(text: str) -> str:
            """個別プロンプト末尾の依頼文（---以降）を除去してデータ部のみ残す。"""
            return text.rsplit("\n---\n", 1)[0].strip()

        def _strip_leading_title(text: str) -> str:
            """先頭見出し（# ...）を1行だけ除去して統合時の重複を防ぐ。"""
            lines = text.splitlines()
            if lines and lines[0].startswith("# "):
                lines = lines[1:]
                while lines and lines[0] == "":
                    lines = lines[1:]
            return "\n".join(lines).strip()

        sections = [
            ("資産分析", _strip_leading_title(_strip_section_instruction(self._ai_prompt_asset(conn)))),
            ("家計簿分析", _strip_leading_title(_strip_section_instruction(self._ai_prompt_cf(conn)))),
            ("ライフプラン", _strip_leading_title(_strip_section_instruction(self._ai_prompt_plan(conn)))),
            ("シミュレーター", _strip_leading_title(_strip_section_instruction(self._ai_prompt_simulator()))),
        ]

        lines = ["# AI相談用データ（全種類）", ""]
        for i, (title, text) in enumerate(sections):
            if i > 0:
                lines += ["", "---", ""]
            lines += [f"## {title}", "", text.strip()]
        lines += [
            "",
            "---",
            "",
            "上記は私の資産・家計簿・ライフプラン・シミュレーションの統合データです。総合的に分析し、",
            "優先順位付きで改善アクションを提案してください（短期: 3ヶ月 / 中期: 1年 / 長期: 3年以上）。",
        ]
        return "\n".join(lines)

    def _ai_prompt_asset(self, conn: sqlite3.Connection) -> str:
        """資産分析用プロンプトを生成する。"""
        row = conn.execute(
            "SELECT date, total_asset, by_class_json FROM snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "資産データがありません。"
        date, total, by_class_json = row[0], row[1], row[2]
        by_class = json.loads(by_class_json)

        lines = [
            f"# 資産データ（{date}時点）",
            "",
            f"総資産: **{total:,.0f}円**",
            "",
            "## 資産クラス別内訳",
            "",
            "| 資産クラス | 金額 | 割合 |",
            "|---|---:|---:|",
        ]
        for cls, amt in sorted(by_class.items(), key=lambda x: x[1], reverse=True):
            pct = amt / total * 100 if total else 0
            lines.append(f"| {cls} | {amt:,.0f}円 | {pct:.1f}% |")

        investable = calculate_investable_cash(
            conn,
            as_of=datetime.strptime(date, "%Y-%m-%d").date(),
            snapshot_date=date,
        )
        lines += [
            "",
            "## 投資可能額",
            "",
            f"- 投資可能額: **{investable['investable_cash']:,.0f}円**",
            f"- 預金・現金: {investable['cash_balance']:,.0f}円",
            f"- 生活防衛資金: {investable['emergency_fund']:,.0f}円",
            f"- 計画期間内の予定支出: {investable['planned_expenses']:,.0f}円",
            f"- カード引き落とし予定: {investable['scheduled_card_payment_total']:,.0f}円",
            f"- 追加確保額: {investable['additional_reserve']:,.0f}円",
            f"- 計画期間: {investable['planned_expense_horizon_months']}か月",
        ]

        # 保有銘柄
        holdings = conn.execute(
            "SELECT name, symbol_or_code, asset_class, value, quantity, unrealized_gain, unrealized_gain_pct FROM snapshot_holdings WHERE date = ? ORDER BY value DESC LIMIT 20",
            (date,),
        ).fetchall()
        if holdings:
            lines += [
                "",
                "## 保有銘柄（上位20件）",
                "",
                "| 銘柄 | 資産クラス | 評価額 | 損益 | 損益率 |",
                "|---|---|---:|---:|---:|",
            ]
            for h in holdings:
                name, code, ac, val, qty, gain, gain_pct = h
                gain_s = f"{gain:+,.0f}円" if gain is not None else "-"
                pct_s = f"{gain_pct:+.1f}%" if gain_pct is not None else "-"
                lines.append(f"| {name} | {ac} | {val:,.0f}円 | {gain_s} | {pct_s} |")

        lines += [
            "",
            "---",
            "",
            "上記は私の資産ポートフォリオです。以下の観点で分析・アドバイスをお願いします：",
            "1. ポートフォリオのバランス評価",
            "2. リスク分散の状況",
            "3. 投資可能額を投資信託・日本株・米国株・現金のどこへ振るか、根拠と金額を含む比較",
            "4. 改善提案",
        ]
        return "\n".join(lines)

    def _ai_prompt_cf(self, conn: sqlite3.Connection) -> str:
        """家計簿分析用プロンプトを生成する。"""
        closing_day = int(get_setting(conn, "closing_day", "1") or "1")
        holiday_mode = get_setting(conn, "closing_day_holiday", "none") or "none"

        # fiscal month ベースで最新月（取引データあり）を取得
        available = get_cf_available_months(conn, closing_day=closing_day, holiday_mode=holiday_mode)
        with_data = [m for m in available if m.get("has_data")]
        if not with_data:
            return "家計簿データがありません。"
        ym = with_data[0]["year_month"]

        summary = get_cf_category_summary(conn, ym, closing_day=closing_day, holiday_mode=holiday_mode)
        if not summary:
            return "家計簿データがありません。"

        lines = [
            f"# 家計簿データ（{ym}）",
            "",
            f"- 収入合計: **{summary['total_income']:,.0f}円**",
            f"- 支出合計: **{summary['total_expense']:,.0f}円**",
            f"- 収支: **{summary['balance']:+,.0f}円**",
            "",
        ]

        lines += ["## カテゴリ別支出", "", "| カテゴリ | 金額 | 割合 |", "|---|---:|---:|"]
        for c in summary["major_categories"]:
            pct = c["total"] / summary["total_expense"] * 100 if summary["total_expense"] else 0
            lines.append(f"| {c['name']} | {c['total']:,.0f}円 | {pct:.1f}% |")

        # 月別推移
        trend = get_cf_monthly_trend(conn, months=6, closing_day=closing_day, holiday_mode=holiday_mode)
        if trend:
            lines += ["", "## 月別推移（直近6ヶ月）", "", "| 月 | 収入 | 支出 | 収支 |", "|---|---:|---:|---:|"]
            for t in trend[-6:]:
                net = t["income"] - t["expense"]
                lines.append(f"| {t['year_month']} | {t['income']:,.0f}円 | {t['expense']:,.0f}円 | {net:+,.0f}円 |")

        lines += [
            "",
            "---",
            "",
            "上記は私の家計簿データです。以下の観点で分析・アドバイスをお願いします：",
            "1. 支出の傾向と改善ポイント",
            "2. 前月との比較（増減の要因）",
            "3. 節約の具体的提案",
        ]
        return "\n".join(lines)

    def _ai_prompt_plan(self, conn: sqlite3.Connection) -> str:
        """ライフプラン用プロンプトを生成する。"""
        row = conn.execute(
            "SELECT date, total_asset, by_class_json FROM snapshots ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "資産データがありません。"
        date, total = row[0], row[1]

        # 月次資産推移
        monthly_rows = conn.execute("SELECT date, total_asset FROM snapshots ORDER BY date ASC").fetchall()
        monthly_end: dict[str, float] = {}
        for d, t in monthly_rows:
            monthly_end[d[:7]] = t

        # 月次収支
        cashflows = get_cashflows(conn, limit=6)
        cashflows.reverse()

        lines = [f"# ライフプランデータ（{date}時点）", "", f"現在の総資産: **{total:,.0f}円**", ""]

        if monthly_end:
            lines += ["## 月次資産推移", "", "| 月 | 総資産 |", "|---|---:|"]
            for ym, t in sorted(monthly_end.items())[-6:]:
                lines.append(f"| {ym} | {t:,.0f}円 |")

        if cashflows:
            lines += ["", "## 月次収支", "", "| 月 | 収入 | 支出 | 収支 |", "|---|---:|---:|---:|"]
            for cf in cashflows:
                net = cf["income"] - cf["expense"]
                lines.append(f"| {cf['year_month']} | {cf['income']:,.0f}円 | {cf['expense']:,.0f}円 | {net:+,.0f}円 |")

        lines += [
            "",
            "---",
            "",
            "上記は私の資産・収支データです。以下の観点でライフプランのアドバイスをお願いします：",
            "1. 資産形成の進捗評価",
            "2. 収支バランスの改善点",
            "3. 将来の資産目標に向けた提案",
        ]
        return "\n".join(lines)

    def _ai_prompt_simulator(self) -> str:
        """シミュレーター用プロンプトを生成する。"""
        data = _get_simulator_data(self.db_path)
        return _build_ai_prompt_simulator(data)

    def _check_origin(self) -> bool:
        """Origin ヘッダを検証し、リクエスト先の Host と異なる場合のみ拒否する（CSRF 対策）。

        リバースプロキシ経由の独自ドメイン（例: money.home.arpa）でもアクセスできるよう、
        localhost 固定のホワイトリストではなく Host ヘッダとの一致判定にする。
        """
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        source = origin or referer
        host = self.headers.get("Host", "")
        source_host = urlparse(source).netloc if source else ""
        if source_host and source_host != host:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "forbidden"}).encode())
            return False
        return True

    def _json_error(self, status: int, message: str) -> None:
        """JSON エラーレスポンスを返すヘルパー。"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": False, "error": message}).encode())

    def do_POST(self) -> None:
        if not self._check_origin():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            post_params = parse_qs(body)
            setting_type = post_params.get("setting_type", ["gemini"])[0]
            # リダイレクト URL に反映するためホワイトリストで正規化
            if setting_type not in (
                "closing_day",
                "scheduler",
                "regional_exposure",
                "investable_cash",
                "scheduled_card_payment",
            ):
                setting_type = "gemini"

            if setting_type == "closing_day":
                # 締め日設定
                closing_day_str = post_params.get("closing_day", ["1"])[0].strip()
                try:
                    closing_day_val = max(1, min(31, int(closing_day_str)))
                except ValueError:
                    closing_day_val = 1
                holiday_mode_val = post_params.get("holiday_mode", ["none"])[0].strip()
                if holiday_mode_val not in ("none", "before", "after"):
                    holiday_mode_val = "none"
                conn = get_connection(self.db_path)
                try:
                    save_setting(conn, "closing_day", str(closing_day_val))
                    save_setting(conn, "closing_day_holiday", holiday_mode_val)
                    logger.info("締め日更新: %d日 (祝日: %s)", closing_day_val, holiday_mode_val)
                finally:
                    conn.close()
            elif setting_type == "scheduler":
                # 自動データ取得設定
                enabled_val = "1" if post_params.get("scheduler_enabled") else "0"
                time_str = post_params.get("scheduler_time", [_SCHEDULER_DEFAULT_TIME])[0].strip()
                try:
                    datetime.strptime(time_str, "%H:%M")
                except ValueError:
                    time_str = _SCHEDULER_DEFAULT_TIME
                conn = get_connection(self.db_path)
                try:
                    save_setting(conn, "scheduler_enabled", enabled_val)
                    save_setting(conn, "scheduler_time", time_str)
                    logger.info("スケジューラ設定更新: enabled=%s, time=%s", enabled_val, time_str)
                finally:
                    conn.close()
            elif setting_type == "regional_exposure":
                region_fields = {
                    "日本": "japan",
                    "米国": "us",
                    "先進国（日本・米国除く）": "developed",
                    "新興国": "emerging",
                    "その他": "other",
                }
                try:
                    count = max(0, min(500, int(post_params.get("exposure_count", ["0"])[0])))
                except ValueError:
                    count = 0
                config = {}
                for index in range(count):
                    holding_key = post_params.get(f"exposure_key_{index}", [""])[0].strip()
                    if not holding_key:
                        continue
                    config[holding_key] = {
                        region: float(post_params.get(f"region_{index}_{slug}", ["0"])[0])
                        for region, slug in region_fields.items()
                    }
                conn = get_connection(self.db_path)
                try:
                    save_regional_exposure_config(conn, config)
                except (ValueError, TypeError):
                    setting_type = "regional_error"
                finally:
                    conn.close()
            elif setting_type == "scheduled_card_payment":
                scheduled_action = post_params.get("scheduled_action", ["add"])[0].strip()
                conn = get_connection(self.db_path)
                try:
                    if scheduled_action == "disable":
                        payment_id = int(post_params.get("scheduled_payment_id", ["0"])[0])
                        if payment_id <= 0:
                            raise ValueError("予定IDが不正です")
                        disable_scheduled_card_payment(conn, payment_id)
                    else:
                        due_date = post_params.get("scheduled_due_date", [""])[0].strip()
                        card_name = post_params.get("scheduled_card_name", [""])[0].strip()
                        amount = float(post_params.get("scheduled_amount", ["0"])[0])
                        withdrawal_account = post_params.get("scheduled_withdrawal_account", [""])[0].strip()
                        memo = post_params.get("scheduled_memo", [""])[0].strip()
                        parsed_due_date = date.fromisoformat(due_date)
                        if parsed_due_date < date.today() or not card_name or amount <= 0 or amount > 1_000_000_000:
                            raise ValueError("引落予定日の未来日・カード名・正の金額が必要です")
                        create_scheduled_card_payment(
                            conn,
                            parsed_due_date.isoformat(),
                            card_name[:80],
                            amount,
                            withdrawal_account[:80],
                            memo[:160],
                        )
                except (TypeError, ValueError):
                    setting_type = "scheduled_card_payment_error"
                finally:
                    conn.close()
            elif setting_type == "investable_cash":

                def bounded_number(name: str, default: float, maximum: float) -> float:
                    try:
                        value = float(post_params.get(name, [str(default)])[0])
                    except (TypeError, ValueError):
                        value = default
                    return max(0.0, min(maximum, value))

                monthly_expense_val = bounded_number("monthly_living_expense", 0, 100_000_000)
                emergency_months_val = bounded_number("emergency_fund_months", 6, 60)
                horizon_months_val = bounded_number("planned_expense_horizon_months", 12, 120)
                additional_reserve_val = bounded_number("additional_cash_reserve", 0, 1_000_000_000)
                conn = get_connection(self.db_path)
                try:
                    save_setting(conn, "monthly_living_expense", str(monthly_expense_val))
                    save_setting(conn, "emergency_fund_months", str(emergency_months_val))
                    save_setting(conn, "planned_expense_horizon_months", str(int(horizon_months_val)))
                    save_setting(conn, "additional_cash_reserve", str(additional_reserve_val))
                finally:
                    conn.close()
            else:
                # Gemini APIキー設定
                api_key = post_params.get("gemini_api_key", [""])[0].strip()
                conn = get_connection(self.db_path)
                try:
                    if api_key:
                        save_setting(conn, "gemini_api_key", api_key)
                    else:
                        conn.execute("DELETE FROM settings WHERE key = 'gemini_api_key'")
                        conn.commit()
                finally:
                    conn.close()
                # キーが設定されたら即座にAIコメント生成を試みる（バックグラウンド）
                if api_key:
                    t = threading.Thread(target=_generate_ai_comments, args=(self.db_path,), daemon=True)
                    t.start()
            self.send_response(303)
            self.send_header("Location", f"/settings?saved={setting_type}")
            self.end_headers()

        elif parsed.path == "/api/life-events":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                title = str(req.get("title", "")).strip()
                amount = float(req.get("amount", 0))
                start_year = int(req.get("start_year", 0))
                repeat = int(req.get("repeat_every_years", 0))
                end_year = req.get("end_year")
                if end_year in ("", None):
                    end_year = None
                else:
                    end_year = int(end_year)
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            if not title or amount <= 0 or start_year < 1900:
                self._json_error(400, "入力値が不正です")
                return
            conn = get_connection(self.db_path)
            try:
                create_life_event(
                    conn=conn,
                    event_type="recurring" if repeat and repeat > 0 else "one_time",
                    title=title,
                    amount=amount,
                    start_year=start_year,
                    repeat_every_years=repeat if repeat > 0 else None,
                    end_year=end_year,
                    enabled=True,
                    note="",
                )
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/life-events/delete":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                event_id = int(req.get("id", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            conn = get_connection(self.db_path)
            try:
                delete_life_event(conn, event_id)
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/life-events/housing-template":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                purchase_year = int(req.get("purchase_year", 0))
                price = float(req.get("price", 0))
                down_payment = float(req.get("down_payment", 0))
                loan_years = int(req.get("loan_years", 0))
                annual_interest_rate = float(req.get("annual_interest_rate", 0))
                annual_maintenance = float(req.get("annual_maintenance", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            if purchase_year < 1900 or price <= 0 or loan_years <= 0:
                self._json_error(400, "入力値が不正です")
                return

            loan_amount = max(0.0, price - max(0.0, down_payment))
            monthly_rate = max(0.0, annual_interest_rate) / 100 / 12
            n_months = loan_years * 12
            if loan_amount <= 0:
                monthly_payment = 0.0
            elif monthly_rate == 0:
                monthly_payment = loan_amount / n_months
            else:
                p = (1 + monthly_rate) ** n_months
                monthly_payment = loan_amount * monthly_rate * p / (p - 1)
            annual_payment = monthly_payment * 12
            end_year = purchase_year + loan_years - 1

            conn = get_connection(self.db_path)
            try:
                if down_payment > 0:
                    create_life_event(
                        conn=conn,
                        event_type="one_time",
                        title="住宅購入 頭金",
                        amount=down_payment,
                        start_year=purchase_year,
                        repeat_every_years=None,
                        end_year=purchase_year,
                        enabled=True,
                        note="housing_template",
                    )
                if annual_payment > 0:
                    create_life_event(
                        conn=conn,
                        event_type="recurring",
                        title="住宅ローン返済（年額）",
                        amount=annual_payment,
                        start_year=purchase_year,
                        repeat_every_years=1,
                        end_year=end_year,
                        enabled=True,
                        note="housing_template",
                    )
                if annual_maintenance > 0:
                    create_life_event(
                        conn=conn,
                        event_type="recurring",
                        title="住宅維持費（年額）",
                        amount=annual_maintenance,
                        start_year=purchase_year,
                        repeat_every_years=1,
                        end_year=end_year,
                        enabled=True,
                        note="housing_template",
                    )
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/life-events/update":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                event_id = int(req.get("id", 0))
                title = str(req.get("title", "")).strip()
                amount = float(req.get("amount", 0))
                start_year = int(req.get("start_year", 0))
                repeat = int(req.get("repeat_every_years", 0))
                end_year = req.get("end_year")
                if end_year in ("", None):
                    end_year = None
                else:
                    end_year = int(end_year)
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            if event_id <= 0 or not title or amount <= 0 or start_year < 1900:
                self._json_error(400, "入力値が不正です")
                return
            conn = get_connection(self.db_path)
            try:
                update_life_event(
                    conn=conn,
                    event_id=event_id,
                    event_type="recurring" if repeat and repeat > 0 else "one_time",
                    title=title,
                    amount=amount,
                    start_year=start_year,
                    repeat_every_years=repeat if repeat > 0 else None,
                    end_year=end_year,
                    enabled=True,
                    note="",
                )
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/children":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                name = str(req.get("name", "")).strip()
                birth_year = int(req.get("birth_year", 0))
                birth_month = int(req.get("birth_month", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            if not name or birth_year < 1900 or not (1 <= birth_month <= 12):
                self._json_error(400, "入力値が不正です")
                return
            conn = get_connection(self.db_path)
            try:
                create_child_profile(conn, name=name, birth_year=birth_year, birth_month=birth_month)
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/children/update":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                child_id = int(req.get("id", 0))
                name = str(req.get("name", "")).strip()
                birth_year = int(req.get("birth_year", 0))
                birth_month = int(req.get("birth_month", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            if child_id <= 0 or not name or birth_year < 1900 or not (1 <= birth_month <= 12):
                self._json_error(400, "入力値が不正です")
                return
            conn = get_connection(self.db_path)
            try:
                children = list_children_profiles(conn, enabled_only=False)
                row = next((c for c in children if int(c["id"]) == child_id), None)
                plan = (row or {}).get("education_plan", {})
                update_child_profile(
                    conn=conn,
                    child_id=child_id,
                    name=name,
                    birth_year=birth_year,
                    birth_month=birth_month,
                    education_plan=plan,
                    enabled=True,
                )
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/children/delete":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                child_id = int(req.get("id", 0))
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            conn = get_connection(self.db_path)
            try:
                delete_child_profile(conn, child_id)
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/children/update-plan":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                child_id = int(req.get("id", 0))
                stage = str(req.get("stage", "")).strip()
                value = str(req.get("value", "")).strip()
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            if child_id <= 0 or stage not in {"kindergarten", "elementary", "junior_high", "high_school", "university"}:
                self._json_error(400, "入力値が不正です")
                return
            conn = get_connection(self.db_path)
            try:
                children = list_children_profiles(conn, enabled_only=False)
                row = next((c for c in children if int(c["id"]) == child_id), None)
                if not row:
                    self._json_error(404, "child not found")
                    return
                plan = dict(row.get("education_plan") or {})
                plan[stage] = value
                update_child_profile(
                    conn=conn,
                    child_id=child_id,
                    name=row["name"],
                    birth_year=int(row["birth_year"]),
                    birth_month=int(row["birth_month"]),
                    education_plan=plan,
                    enabled=bool(row.get("enabled", True)),
                )
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/life-settings":
            if self.demo:
                self._json_error(400, "デモモードでは変更できません")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                inflation_rate = float(req.get("inflation_rate", 0.01))
            except (json.JSONDecodeError, ValueError, TypeError):
                self._json_error(400, "invalid payload")
                return
            conn = get_connection(self.db_path)
            try:
                save_life_plan_inflation_rate(conn, inflation_rate)
            finally:
                conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        elif parsed.path == "/api/simulator/reset":
            # DB の simulator_params を削除して実データから再取得
            result_data: dict[str, object] = {"ok": False}
            try:
                if not self.demo:
                    conn = get_connection(self.db_path)
                    try:
                        # 保存済みパラメータを削除
                        save_setting(conn, "simulator_params", "")
                        # 実データから再取得
                        data = _get_simulator_data(self.db_path)
                        params = data["params"]
                        result_data = {
                            "ok": True,
                            "initial_investment": params["initial_investment"],
                            "safe_value": params["safe_value"],
                            "monthly_contribution": params["monthly_contribution"],
                        }
                    finally:
                        conn.close()
                else:
                    result_data = {
                        "ok": True,
                        "initial_investment": _SIMULATOR_DEFAULTS["initial_investment"],
                        "safe_value": _SIMULATOR_DEFAULTS["safe_value"],
                        "monthly_contribution": _SIMULATOR_DEFAULTS["monthly_contribution"],
                    }
            except Exception as e:
                result_data = {"ok": False, "error": str(e)}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result_data, ensure_ascii=False).encode())

        elif parsed.path == "/api/simulator":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
            except json.JSONDecodeError:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "invalid JSON"}).encode())
                return

            # パラメータ抽出とバリデーション
            try:
                ca = int(req.get("current_age", 35))
                ra = int(req.get("retirement_age", 65))
                ea = int(req.get("end_age", 95))
                inv = float(req.get("initial_investment", 5_000_000))
                sv = float(req.get("safe_value", 0))
                mc = float(req.get("monthly_contribution", 50_000))
                ar = float(req.get("annual_return", 0.05))
                av = float(req.get("annual_volatility", 0.15))
                mw = float(req.get("monthly_withdrawal", 200_000))
                ir = float(req.get("inflation_rate", 0.02))
                er = float(req.get("expense_ratio", 0.003))
                psa = int(req.get("pension_start_age", 65))
                mp = float(req.get("monthly_pension", 150_000))
                omi = float(req.get("other_monthly_income", 0))
                rea = int(req.get("reemployment_end_age", ra))
                rmi = float(req.get("reemployment_monthly_income", 0))
            except (ValueError, TypeError):
                self._json_error(400, "パラメータの値が不正です")
                return

            # 有限値チェック（inf/nan 防止）
            all_floats = [inv, sv, mc, ar, av, mw, ir, er, mp, omi, rmi]
            if not all(math.isfinite(v) for v in all_floats):
                self._json_error(400, "パラメータの値が不正です")
                return

            # 年齢整合性
            if not (ca <= ra <= ea):
                self._json_error(400, "現在の年齢 ≤ 退職年齢 ≤ 終了年齢 にしてください")
                return
            if not (60 <= psa <= 75):
                self._json_error(400, "年金受給開始年齢は60〜75歳の範囲にしてください")
                return
            if not (ra <= rea <= ea):
                self._json_error(400, "退職年齢 ≤ 再雇用終了年齢 ≤ 終了年齢 にしてください")
                return
            # 数値範囲チェック（UIのmin/maxと同じ制約）
            _MAX_LUMP = 200_000_000  # 一括金額上限（初期投資・安全資産）
            _MAX_MONTHLY = 1_000_000  # 月額上限（積立・取崩し）
            if inv < 0 or sv < 0 or mc < 0 or mw < 0 or mp < 0 or omi < 0 or rmi < 0:
                self._json_error(400, "金額は0以上にしてください")
                return
            if inv > _MAX_LUMP or sv > _MAX_LUMP:
                self._json_error(400, "金額が上限を超えています")
                return
            if mc > _MAX_MONTHLY or mw > _MAX_MONTHLY or rmi > _MAX_MONTHLY:
                self._json_error(400, "月額は100万円以下にしてください")
                return
            if mp > 500_000 or omi > 500_000:
                self._json_error(400, "月額収入は50万円以下にしてください")
                return
            if not (0.0 <= ar <= 0.15):
                self._json_error(400, "期待リターンは0〜15%の範囲にしてください")
                return
            if not (0.01 <= av <= 0.40):
                self._json_error(400, "ボラティリティは1〜40%の範囲にしてください")
                return
            if not (0.0 <= ir <= 0.10):
                self._json_error(400, "インフレ率は0〜10%の範囲にしてください")
                return
            if not (0.0 <= er <= 0.03):
                self._json_error(400, "信託報酬は0〜3%の範囲にしてください")
                return

            try:
                annual_event_expenses: dict[int, float] = {}
                if not self.demo:
                    conn = get_connection(self.db_path)
                    try:
                        annual_event_expenses = _annual_event_expenses_by_age(conn, current_age=ca, end_age=ea)
                    finally:
                        conn.close()

                result = run_lifecycle_simulation(
                    current_age=ca,
                    retirement_age=ra,
                    end_age=ea,
                    initial_investment=inv,
                    safe_value=sv,
                    monthly_contribution=mc,
                    annual_return=ar,
                    annual_volatility=av,
                    monthly_withdrawal=mw,
                    inflation_rate=ir,
                    expense_ratio=er,
                    pension_start_age=psa,
                    monthly_pension=mp,
                    other_monthly_income=omi,
                    reemployment_end_age=rea,
                    reemployment_monthly_income=rmi,
                    annual_event_expenses=annual_event_expenses,
                    rng_seed=42,
                )
                result_no_events = run_lifecycle_simulation(
                    current_age=ca,
                    retirement_age=ra,
                    end_age=ea,
                    initial_investment=inv,
                    safe_value=sv,
                    monthly_contribution=mc,
                    annual_return=ar,
                    annual_volatility=av,
                    monthly_withdrawal=mw,
                    inflation_rate=ir,
                    expense_ratio=er,
                    pension_start_age=psa,
                    monthly_pension=mp,
                    other_monthly_income=omi,
                    reemployment_end_age=rea,
                    reemployment_monthly_income=rmi,
                    annual_event_expenses={},
                    rng_seed=42,
                )
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": str(e)}).encode())
                return

            # 非デモ時はパラメータを保存
            if not self.demo:
                try:
                    conn = get_connection(self.db_path)
                    try:
                        save_setting(conn, "simulator_params", json.dumps(req, ensure_ascii=False))
                    finally:
                        conn.close()
                except Exception:
                    pass

            payload = {
                "ok": True,
                "yearly_balances": result.yearly_balances,
                "yearly_balances_no_events": result_no_events.yearly_balances,
                "depletion_probability": result.depletion_probability,
                "principal_loss_probability": result.principal_loss_probability,
                "total_principal": result.total_principal,
                "total_gains": result.total_gains,
                "total_tax": result.total_tax,
                "net_final": result.net_final,
                "net_final_no_events": result_no_events.net_final,
                "total_event_expense": sum(float(v) for v in annual_event_expenses.values()),
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())

        elif parsed.path == "/api/cf/download":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                ym = req.get("year_month", "")
                # YYYY-MM → year, month
                parts = ym.split("-")
                year, month = int(parts[0]), int(parts[1])
            except (json.JSONDecodeError, ValueError, IndexError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "invalid year_month"}).encode())
                return

            # バックグラウンドでダウンロード実行
            db_path = self.db_path

            def _download_cf():
                import asyncio
                from datetime import date as _d

                from src.parser.cf_csv import parse_cf_csv
                from src.scraper.fetch import RAW_DIR, create_context, fetch_cf_csv

                try:
                    raw_path = RAW_DIR / f"cf_{ym}"
                    raw_path.mkdir(parents=True, exist_ok=True)

                    async def _run():
                        pw, browser, context = await create_context(headless=True, accept_downloads=True)
                        try:
                            page = await context.new_page()
                            csv_path = await fetch_cf_csv(page, year, month, raw_path)
                            return csv_path
                        finally:
                            await browser.close()
                            await pw.stop()

                    csv_path = asyncio.run(_run())
                    if csv_path:
                        transactions = parse_cf_csv(csv_path)
                        if transactions:
                            conn = init_db(db_path)
                            try:
                                today_str = _d.today().isoformat()
                                save_cf_transactions(conn, transactions, today_str)
                                save_cf_csv_month(conn, ym, today_str, len(transactions))
                            finally:
                                conn.close()
                            logger.info("[cf] %s: %d件保存完了", ym, len(transactions))
                except Exception as e:
                    logger.error("[cf] %s ダウンロード失敗: %s", ym, e)

            t = threading.Thread(target=_download_cf, daemon=True)
            t.start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "year_month": ym}).encode())

        elif parsed.path == "/api/cf/budget":
            length = int(self.headers.get("Content-Length", 0))
            if length > 65536:
                self.send_response(413)
                self.end_headers()
                return
            body = self.rfile.read(length).decode()
            try:
                req = json.loads(body)
                category = str(req.get("category", "")).strip()
                amount = int(req.get("amount", 0) or 0)
            except (json.JSONDecodeError, ValueError, TypeError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "invalid request"}).encode())
                return

            # 入力検証
            if not category or len(category) > 50:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "invalid category"}).encode())
                return
            if amount < 0 or amount > 100_000_000:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "invalid amount"}).encode())
                return

            conn = get_connection(self.db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                budgets = get_budgets(conn)
                old_value = budgets.get(category)
                if amount > 0:
                    if category not in budgets and len(budgets) >= 50:
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"ok": False, "error": "too many budgets"}).encode())
                        return
                    budgets[category] = amount
                else:
                    budgets.pop(category, None)
                save_budgets(conn, budgets)
                logger.info("予算更新: %s = %d (旧値: %s)", category, amount, old_value)
            except Exception as e:
                logger.error("予算更新失敗: %s", e)
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "internal error"}).encode())
                return
            finally:
                conn.close()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs


def _should_update(db_path: str, max_age_hours: int = 6) -> bool:
    """前回取得から max_age_hours 以上経過していれば True。

    - last_fetch_at が存在しない場合、snapshots が1件以上あれば True
      （過去に取得成功している＝ログイン設定済み）
    - snapshots が0件なら False（初回セットアップ未完了の可能性）
    """
    conn = init_db(db_path)
    try:
        last = get_setting(conn, "last_fetch_at")
        has_snapshots = conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None
    finally:
        conn.close()

    if last is None:
        return has_snapshots

    elapsed = datetime.now() - datetime.fromisoformat(last)
    return elapsed.total_seconds() >= max_age_hours * 3600


def _parse_scheduler_time(value: str | None) -> tuple[int, int]:
    """ "HH:MM" 形式の文字列を (hour, minute) に変換する。不正値は (7, 0) にフォールバック。"""
    if value:
        with contextlib.suppress(ValueError):
            t = datetime.strptime(value, "%H:%M")
            return t.hour, t.minute
    return 7, 0


def _should_run_scheduled(now: datetime, scheduled_time: str | None, last_run_at: datetime | None) -> bool:
    """当日の予定時刻を過ぎていて、前回実行がその時刻より前なら True。

    経過時間（24時間以上）の比較ではなく「前回実行 < 当日の予定時刻」のスロット比較に
    することで、チェック間隔の粒度による実行時刻のずれ（drift）を防ぐ。
    サーバーが予定時刻に停止していた場合も、起動後の最初の判定で True になる（追いつき実行）。
    """
    hour, minute = _parse_scheduler_time(scheduled_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return False
    if last_run_at is None:
        return True
    return last_run_at < target


def _next_scheduled_run(now: datetime, scheduled_time: str | None) -> datetime:
    """次回の実行予定時刻。当日の予定時刻が未来ならそれ、過ぎていれば翌日。"""
    hour, minute = _parse_scheduler_time(scheduled_time)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < target:
        return target
    return target + timedelta(days=1)


def _needs_dividend_update(codes: list[str], path: Path | None = None) -> bool:
    """全保有銘柄の配当が当日取得済みでなければ True。"""
    from datetime import date as _date

    if path is None:
        path = Path(__file__).resolve().parents[2] / "data" / "dividends.json"
    if not path.exists():
        return True
    data = json.loads(path.read_text(encoding="utf-8"))
    today = _date.today().isoformat()
    return bool(codes) and not all(data.get(code, {}).get("fetched") == today for code in codes)


def _generate_ai_comments(db_path: str) -> None:
    """AIコメントを生成・保存する。失敗してもエラーを握りつぶす。"""
    try:
        generate_comments(db_path)
    except Exception as e:
        logger.error("[ai] AI分析エラー: %s", e)


def _bg_worker(db_path: str) -> None:
    """バックグラウンドでデータ取得・配当更新・AI分析を実行する。

    起動時更新スレッドとスケジューラスレッドの同時発火を _update_lock で1本に抑える。
    """
    if not _update_lock.acquire(blocking=False):
        logger.info("[auto] 更新は既に実行中 — スキップ")
        return
    try:
        _run_update_locked(db_path)
    finally:
        _update_lock.release()


def _run_update_locked(db_path: str) -> None:
    """データ取得・配当更新・AI分析の本体。_update_lock を取得済みの前提で呼ぶこと。"""
    _update_state["running"] = True
    try:
        import asyncio

        from src.daily import run

        # MoneyForwardの認証ページはヘッドレスChromiumを403にするため、
        # ローカルの表示セッション（DISPLAY）を使って日次取得する。
        asyncio.run(run(headless=False))

        # 更新成功 → セッション切れフラグをクリア
        conn = init_db(db_path)
        try:
            save_setting(conn, "session_expired", "")
            stock_codes = get_latest_stock_codes(conn)
        finally:
            conn.close()

        if _needs_dividend_update(stock_codes):
            from src.data.dividend_fetcher import update_all_dividends

            update_all_dividends(stock_codes)

        _generate_ai_comments(db_path)

        _update_state["version"] += 1
        logger.info("[auto] バックグラウンド更新完了")
    except Exception as e:
        # セッション切れを検知してDBにフラグを保存
        if "ログインページにリダイレクト" in str(e) or "sign_in" in str(e):
            try:
                conn = init_db(db_path)
                try:
                    save_setting(conn, "session_expired", datetime.now().isoformat())
                finally:
                    conn.close()
            except Exception:
                pass
            logger.error("[auto] セッション切れを検知: %s", e)
        else:
            logger.error("[auto] バックグラウンド更新失敗: %s", e)
    finally:
        _update_state["running"] = False


def _scheduler_tick(db_path: str, now: datetime) -> None:
    """設定を読み、実行時刻に達していればデータ取得を1回実行する。

    取得はスケジューラスレッド上で同期実行する（完了まで次の tick は遅れるが、
    試行時刻を先に保存するため二重実行は起きない）。
    実行時刻を当日中に後ろへ変更した場合、新しい時刻で同日もう1回実行される
    （人間がテストのため「数分後」に設定する操作を想定した意図的な仕様。
    直近1時間以内に取得済みなら _should_update が抑止する）。
    """
    conn = init_db(db_path)
    try:
        enabled = get_setting(conn, "scheduler_enabled", "1") != "0"
        scheduled_time = get_setting(conn, "scheduler_time", _SCHEDULER_DEFAULT_TIME)
        last_run_raw = get_setting(conn, "scheduler_last_run_at")
        last_fetch_raw = get_setting(conn, "last_fetch_at")
    finally:
        conn.close()

    if not enabled:
        return

    # 前回のスケジュール試行と起動時更新の成功時刻のうち、新しい方を「前回実行」とみなす
    candidates = []
    for raw in (last_run_raw, last_fetch_raw):
        if raw:
            with contextlib.suppress(ValueError):
                candidates.append(datetime.fromisoformat(raw))
    last_run_at = max(candidates) if candidates else None

    if not _should_run_scheduled(now, scheduled_time, last_run_at):
        return

    # 起動時更新と排他するため、ロックを取得してから試行時刻の保存・実行を行う
    # （running フラグの確認と実行の間に他スレッドが割り込むのを防ぐ）。
    # 取得できないときは試行時刻を保存せず、次の tick に判定を持ち越す —
    # ここで保存すると、起動時更新が失敗した場合に当日の再取得機会を失う。
    if not _update_lock.acquire(blocking=False):
        logger.info("[scheduler] 更新が既に実行中のため次回の判定に持ち越し")
        return
    try:
        # 実行前に試行時刻を保存する — 失敗時に毎分リトライし続けるのを防ぐ（再試行は翌日）
        conn = init_db(db_path)
        try:
            save_setting(conn, "scheduler_last_run_at", now.isoformat())
        finally:
            conn.close()

        if not _should_update(db_path, max_age_hours=1):
            result = "skipped"
            logger.info("[scheduler] データが新しいためスキップ")
        else:
            logger.info("[scheduler] 定時データ取得を開始します...")
            _run_update_locked(db_path)
            conn = init_db(db_path)
            try:
                fetched = get_setting(conn, "last_fetch_at")
            finally:
                conn.close()
            success = bool(fetched) and fetched != last_fetch_raw
            result = "success" if success else "failure"
            logger.info("[scheduler] 定時データ取得: %s", result)
    finally:
        _update_lock.release()

    conn = init_db(db_path)
    try:
        save_setting(conn, "scheduler_last_result", result)
    finally:
        conn.close()


def _scheduler_loop(db_path: str) -> None:
    """毎分設定を読み直し、実行時刻判定が True ならデータ取得を実行する常駐ループ。"""
    while True:
        time.sleep(_SCHEDULER_CHECK_INTERVAL)
        try:
            _scheduler_tick(db_path, now=datetime.now())
        except Exception as e:
            logger.error("[scheduler] tick エラー: %s", e)


def _start_scheduler(db_path: str) -> None:
    """スケジューラスレッドを起動する。"""
    t = threading.Thread(target=_scheduler_loop, args=(db_path,), daemon=True)
    t.start()
    logger.info("[scheduler] スケジューラを開始しました（毎分チェック）")


def _start_bg_update(db_path: str) -> None:
    """必要に応じてバックグラウンドでデータ更新を開始する。"""
    if _should_update(db_path):
        logger.info("[auto] バックグラウンドでデータ更新を開始します...")
        t = threading.Thread(target=_bg_worker, args=(db_path,), daemon=True)
        t.start()
    else:
        logger.info("[auto] データは最新です — スキップ")


def _kill_existing(port: int) -> None:
    """指定ポートを使用している既存プロセスを停止する。"""
    import subprocess

    try:
        result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True)
        pids = result.stdout.strip().split()
        if pids:
            logger.info("ポート %d の既存プロセス (PID: %s) を停止します...", port, ", ".join(pids))
            subprocess.run(["kill"] + pids)
            import time

            time.sleep(1)
    except FileNotFoundError:
        # lsof がない場合は fuser を試す
        try:
            subprocess.run(["fuser", "-k", f"{port}/tcp"], capture_output=True)
            import time

            time.sleep(1)
        except FileNotFoundError:
            pass


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
    )

    parser = argparse.ArgumentParser(description="資産ダッシュボード")
    parser.add_argument("--db", type=str, default=str(DB_DEFAULT))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="ダミーデータで表示（SNS共有用）")
    parser.add_argument("--skip-update", action="store_true", help="起動時の自動更新をスキップ")
    args = parser.parse_args()

    # 起動時にスキーマ初期化・マイグレーションを1回だけ実行
    conn = init_db(args.db)
    conn.close()

    skip_update = args.demo or args.skip_update
    if not skip_update:
        _start_bg_update(args.db)
        _start_scheduler(args.db)

    Handler.db_path = args.db
    Handler.demo = args.demo
    Handler.skip_update = skip_update

    try:
        server = HTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        if "Address already in use" in str(e):
            _kill_existing(args.port)
            server = HTTPServer(("127.0.0.1", args.port), Handler)
        else:
            raise
    mode = " [DEMO MODE]" if args.demo else ""
    logger.info("Dashboard%s: http://localhost:%d", mode, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("サーバー停止")
        server.shutdown()


if __name__ == "__main__":
    main()
