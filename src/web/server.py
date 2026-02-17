"""シンプルなWebダッシュボード。

標準ライブラリのみで動作する。
使い方: python -m src.web.server
"""

from __future__ import annotations

import argparse
import contextlib
import html as html_mod
import json
import logging
import sqlite3
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from src.analysis.ai_comment import generate_comments, get_comment
from src.analysis.compare import ComparisonResult, get_all_comparisons
from src.analysis.metrics import concentration_top_n, daily_volatility, max_drawdown
from src.data.stock_master import get_dividend, get_sector
from src.db.repository import (
    get_budgets,
    get_cashflows,
    get_cf_actual_savings,
    get_cf_available_months,
    get_cf_category_summary,
    get_cf_category_trend,
    get_cf_fixed_expenses,
    get_cf_income_breakdown,
    get_cf_income_trend,
    get_cf_monthly_trend,
    get_daily_assets,
    get_setting,
    save_budgets,
    save_cf_csv_month,
    save_cf_transactions,
    save_setting,
)
from src.db.schema import get_connection, init_db
from src.prediction.montecarlo import (
    RISK_CLASSES,
    PredictionRange,
    classify_pension_holdings,
    predict_no_contribution,
    predict_with_contribution,
)

logger = logging.getLogger(__name__)

DB_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "assets.db"

_update_state = {"running": False, "version": 0}


def _h(s: str) -> str:
    """HTML エスケープのショートカット。"""
    return html_mod.escape(str(s))


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
        ("/cf", "家計簿分析"),
        ("/plan", "ライフプラン"),
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
    const body = card.querySelector('.card-body');
    const btn = card.querySelector('.collapse-btn');
    if (!body || !btn) return;
    if (saved[id]) {
      card.classList.add('collapsed');
      btn.textContent = '\\u25B6';
    }
    btn.addEventListener('click', () => {
      const isCollapsed = card.classList.toggle('collapsed');
      btn.textContent = isCollapsed ? '\\u25B6' : '\\u25BC';
      const s = JSON.parse(localStorage.getItem('collapsed_cards') || '{}');
      if (isCollapsed) { s[id] = true; } else { delete s[id]; }
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
    dividends: list[dict] = []
    total_dividend = 0.0
    for h in holdings:
        if h["asset_class"] == "株式（現物）" and h["code"] and h["quantity"]:
            dps = get_dividend(h["code"])
            annual = dps * h["quantity"]
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
                        "dps": dps,
                        "annual": annual,
                        "current_yield": current_yield,
                        "acq_yield": acq_yield,
                    }
                )
    dividends.sort(key=lambda x: x["annual"], reverse=True)

    # 配当利回り別内訳（低配当0-2% / 中配当2-4% / 高配当4%超）
    yield_breakdown: dict[str, float] = {"低配当 (0-2%)": 0, "中配当 (2-4%)": 0, "高配当 (4%超)": 0}
    for d in dividends:
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

    # 業種別配当内訳
    sector_dividends: dict[str, dict] = {}
    for h in holdings:
        if h["asset_class"] == "株式（現物）" and h["code"] and h["quantity"]:
            sector = get_sector(h["code"])
            dps = get_dividend(h["code"])
            annual = dps * h["quantity"]
            if sector not in sector_dividends:
                sector_dividends[sector] = {"value": 0, "dividend": 0}
            sector_dividends[sector]["value"] += h["value"]
            sector_dividends[sector]["dividend"] += annual
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
        "yield_breakdown": yield_breakdown,
        "sector_dividends": sector_dividends,
        "volatility": vol,
        "max_drawdown": mdd,
        "concentration": conc,
        "comparisons": comparisons,
    }


def _avg_yield_html(dividends: list[dict]) -> str:
    """配当加重平均利回りの HTML 片を返す。"""
    total_value = 0.0
    total_div = 0.0
    for d in dividends:
        if d.get("current_yield") is not None and d["annual"] > 0:
            # 銘柄の評価額 = dps / (current_yield/100) * quantity
            # 簡易的に annual / (current_yield/100) で株式部分の評価額を逆算
            stock_value = d["annual"] / (d["current_yield"] / 100)
            total_value += stock_value
            total_div += d["annual"]
    if total_value > 0:
        avg_yield = total_div / total_value * 100
        return f"　加重平均利回り {avg_yield:.2f}%"
    return ""


def _build_html(
    data: dict, dates: list[str], skip_update: bool = False, ai_comment: str | None = None, demo: bool = False
) -> str:
    if not data:
        return "<html><body><h1>データがありません</h1></body></html>"

    date = data["date"]
    total = data["total_asset"]
    by_class = data["by_class"]
    accounts = data["accounts"]
    holdings = data["holdings"]
    sector_totals = data.get("sector_totals", {})
    dividends = data.get("dividends", [])
    total_dividend = data.get("total_dividend", 0)
    yield_breakdown = data.get("yield_breakdown", {})
    sector_dividends = data.get("sector_dividends", {})
    vol = data.get("volatility")
    mdd = data.get("max_drawdown")
    conc = data.get("concentration")
    comparisons = data.get("comparisons", [])

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
        if cls == "預金・現金・暗号資産":
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

    hold_rows = ""
    current_class = None
    for h in holdings:
        if h["asset_class"] != current_class:
            current_class = h["asset_class"]
            hold_rows += f'<tr class="group-header"><td colspan="6">{current_class}</td></tr>'
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
        hold_rows += f'<tr><td>{code}{_h(h["name"])}{qty}</td><td class="num">{h["value"]:,.0f}円</td>{gain_cell}{diff_cells}</tr>'

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
  <div class="date-picker">
    <button class="nav-btn" id="prev-btn" title="前の日">&larr;</button>
    <select id="date-select" onchange="location.href='/?date='+this.value">
      {date_options}
    </select>
    <button class="nav-btn" id="next-btn" title="次の日">&rarr;</button>
    <label>({len(dates)}日分のデータ)</label>
  </div>
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
        <tr><th>銘柄</th><th class="num">評価額</th><th class="num">損益</th><th class="num">前日比</th><th class="num">前月比</th><th class="num">前年比</th></tr>
        {hold_rows}
      </table>
      </div>
    </div>
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
{_COLLAPSE_JS}
</script>
</body>
</html>"""


def _demo_data() -> dict:
    """SNS共有用のダミーデータを生成する。"""
    from datetime import date, timedelta

    today = date.today().isoformat()

    by_class = {
        "預金・現金・暗号資産": 4_820_000,
        "株式（現物）": 6_350_000,
        "投資信託": 5_180_000,
        "不動産": 1_200_000,
        "年金": 3_950_000,
    }

    accounts = [
        {"name": "普通預金", "asset_class": "預金・現金・暗号資産", "balance": 2_150_000, "institution": "みずほ銀行"},
        {
            "name": "普通預金",
            "asset_class": "預金・現金・暗号資産",
            "balance": 1_380_000,
            "institution": "三井住友銀行",
        },
        {
            "name": "定期預金",
            "asset_class": "預金・現金・暗号資産",
            "balance": 1_000_000,
            "institution": "住信SBIネット銀行",
        },
        {"name": "円預金", "asset_class": "預金・現金・暗号資産", "balance": 245_000, "institution": "楽天銀行"},
        {"name": "Suica", "asset_class": "預金・現金・暗号資産", "balance": 3_200, "institution": "モバイルSuica"},
        {"name": "預り金", "asset_class": "預金・現金・暗号資産", "balance": 41_800, "institution": "SBI証券"},
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
            by_class_diff={"株式（現物）": 35_800, "投資信託": 12_500, "預金・現金・暗号資産": -6_000},
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
                "預金・現金・暗号資産": -15_000,
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
                "預金・現金・暗号資産": -90_000,
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

    return {
        "date": today,
        "total_asset": total_asset,
        "by_class": by_class,
        "accounts": accounts,
        "holdings": holdings,
        "sector_totals": demo_sectors,
        "dividends": demo_dividends,
        "total_dividend": sum(d["annual"] for d in demo_dividends),
        "yield_breakdown": demo_yield_breakdown,
        "sector_dividends": demo_sector_dividends,
        "volatility": 0.142,
        "max_drawdown": 3.8,
        "concentration": {"top_n": [], "concentration_pct": 32.5},
        "comparisons": demo_comparisons,
        "_sector_holdings": demo_sector_holdings,
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
        cf_savings = get_cf_actual_savings(conn)

        # 日次資産推移
        daily_assets = get_daily_assets(conn, months=6)
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
        predictions, pred_params = predict_no_contribution(db_path, risk_value, safe_value, class_values=class_values)
    except Exception:
        predictions, pred_params = [], {}

    # 成長予測（積立込み）
    try:
        predictions_c, pred_params_c = predict_with_contribution(
            db_path, risk_value, safe_value, monthly_contribution, class_values=class_values
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
    }


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
          <li><strong>手法:</strong> 幾何ブラウン運動（対数正規モデル）でリスク資産の月次リターンを生成し、10,000回のシミュレーションを実行</li>
          <li><strong>パラメータ:</strong> 過去の日次リターンから年率の期待リターンとボラティリティ（価格変動の大きさ）を推定。データが5日未満の場合はデフォルト値（リターン5%/年、ボラティリティ15%/年）を使用</li>
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


def _build_settings_html(db_path: str, saved: str | None = None) -> str:
    """設定ページのHTMLを生成する。"""
    import os

    conn = get_connection(db_path)
    try:
        db_key = get_setting(conn, "gemini_api_key", "")
    finally:
        conn.close()
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

    saved_msg = (
        '<div class="saved-msg">設定を保存しました。AIコメントをバックグラウンドで生成中です — 数十秒後にダッシュボードを開くと表示されます。</div>'
        if saved
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
</div>
</body>
</html>"""


def _get_cf_data(db_path: str, year_month: str | None = None) -> dict:
    """家計簿分析データを取得する。"""
    conn = get_connection(db_path)
    try:
        # 利用可能月
        available = get_cf_available_months(conn)
        if not available:
            return {}

        # デフォルト月 = 最新月
        if year_month is None:
            year_month = available[0]["year_month"]

        # カテゴリ集計
        summary = get_cf_category_summary(conn, year_month)

        # 月別推移
        trend = get_cf_monthly_trend(conn, months=12)

        # カテゴリ別月次推移
        category_trend = get_cf_category_trend(conn, months=6)

        # 固定費 vs 変動費
        fixed_expenses = get_cf_fixed_expenses(conn, months=3)

        # 収入内訳
        income_breakdown = get_cf_income_breakdown(conn, year_month)

        # 収入推移
        income_trend = get_cf_income_trend(conn, months=6)

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
        "fixed_expenses": fixed_expenses,
        "income_breakdown": income_breakdown,
        "income_trend": income_trend,
        "budgets": budgets,
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

    # 当月（途中データ）判定
    from datetime import date as _date

    _current_ym = _date.today().strftime("%Y-%m")
    is_partial_month = year_month == _current_ym

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
    budget_remaining_html = f"""
    <div class="summary-card" data-testid="budget-remaining" style="{budget_display_style}">
      <h3>予算残り</h3>
      <div class="amount" style="color:{remaining_color}">{remaining_sign}{remaining:,.0f}円</div>
    </div>"""

    # 高額支出テーブル
    top_rows = ""
    for t in top_expenses:
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

    balance_sign = "+" if balance >= 0 else ""
    balance_css = "plus" if balance >= 0 else "minus"

    # --- カテゴリ別月次推移データ ---
    cat_trend = data.get("category_trend", {})
    cat_trend_months = cat_trend.get("year_months", [])
    cat_trend_categories = cat_trend.get("categories", [])
    cat_trend_by_month = cat_trend.get("by_month", {})
    cat_trend_json = json.dumps(
        {"months": cat_trend_months, "categories": cat_trend_categories, "by_month": cat_trend_by_month},
        ensure_ascii=False,
    )

    # 差分テーブル
    diff_rows = ""
    if len(cat_trend_months) >= 2:
        last_m = cat_trend_months[-1]
        prev_m = cat_trend_months[-2]
        last_data = cat_trend_by_month.get(last_m, {})
        prev_data = cat_trend_by_month.get(prev_m, {})
        for cat in cat_trend_categories:
            cur = last_data.get(cat, 0)
            prev = prev_data.get(cat, 0)
            diff = cur - prev
            if diff == 0 and cur == 0:
                continue
            diff_sign = "+" if diff > 0 else ""
            diff_color = "color:#e74c3c" if diff > 0 else ("color:#2881D7" if diff < 0 else "")
            diff_rows += f'<tr><td>{_h(cat)}</td><td class="num">{cur:,.0f}円</td><td class="num">{prev:,.0f}円</td><td class="num" style="{diff_color}">{diff_sign}{diff:,.0f}円</td></tr>'

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
        <table>
          <tr><th>カテゴリ</th><th class="num">当月</th><th class="num">前月</th><th class="num">差分</th></tr>
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
        <h2>高額支出 TOP15</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <table>
          <tr><th>日付</th><th>内容</th><th class="num">金額</th><th>カテゴリ</th><th>金融機関</th></tr>
          {top_rows}
        </table>
      </div>
    </div>

    {ib_html}

    {fe_html}

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

    <div class="card full" data-card-id="cf-download">
      <div class="card-header">
        <h2>過去月ダウンロード管理</h2>
        <button class="collapse-btn">&#x25BC;</button>
      </div>
      <div class="card-body">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <input type="month" id="manual-month" style="padding:4px 8px;border:1px solid #dfe6e9;border-radius:6px;font-size:0.9rem">
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
  const input = document.getElementById('manual-month');
  const msg = document.getElementById('manual-msg');
  const ym = input.value;
  if (!ym) {{ msg.textContent = '年月を選択してください'; return; }}
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
}}

{_COLLAPSE_JS}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    db_path: str = str(DB_DEFAULT)
    demo: bool = False
    skip_update: bool = False

    def _send_html(self, html: str) -> None:
        """HTMLレスポンスを送信する。デモモード時はバナーを挿入。"""
        if self.demo:
            html = html.replace("<body>", "<body>" + _DEMO_BANNER, 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

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
            html = _build_html(data, dates, self.skip_update, ai_comment=ai_comment)
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
                    result = get_cf_available_months(conn)
                finally:
                    conn.close()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result, ensure_ascii=False).encode())

        elif parsed.path == "/settings":
            saved = params.get("saved", [None])[0]
            html = _build_settings_html(self.db_path, saved=saved)
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

        elif parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>404 Not Found</h1></body></html>")

    def _check_origin(self) -> bool:
        """Origin ヘッダを検証し、ローカルホストからのリクエストのみ許可する。"""
        origin = self.headers.get("Origin", "")
        referer = self.headers.get("Referer", "")
        source = origin or referer
        if source and not any(source.startswith(p) for p in ("http://localhost", "http://127.0.0.1")):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": False, "error": "forbidden"}).encode())
            return False
        return True

    def do_POST(self) -> None:
        if not self._check_origin():
            return
        parsed = urlparse(self.path)
        if parsed.path == "/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            post_params = parse_qs(body)
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
            self.send_header("Location", "/settings?saved=1")
            self.end_headers()

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


def _needs_dividend_update() -> bool:
    """dividends.json が存在しない or 当日取得でなければ True。"""
    from datetime import date as _date

    path = Path(__file__).resolve().parents[2] / "data" / "dividends.json"
    if not path.exists():
        return True
    data = json.loads(path.read_text(encoding="utf-8"))
    today = _date.today().isoformat()
    return not any(v.get("fetched") == today for v in data.values())


def _generate_ai_comments(db_path: str) -> None:
    """AIコメントを生成・保存する。失敗してもエラーを握りつぶす。"""
    try:
        generate_comments(db_path)
    except Exception as e:
        logger.error("[ai] AI分析エラー: %s", e)


def _bg_worker(db_path: str) -> None:
    """バックグラウンドでデータ取得・配当更新・AI分析を実行する。"""
    _update_state["running"] = True
    try:
        import asyncio

        from src.daily import run

        asyncio.run(run(headless=True))

        if _needs_dividend_update():
            from src.data.dividend_fetcher import update_all_dividends

            update_all_dividends()

        _generate_ai_comments(db_path)

        _update_state["version"] += 1
        logger.info("[auto] バックグラウンド更新完了")
    except Exception as e:
        logger.error("[auto] バックグラウンド更新失敗: %s", e)
    finally:
        _update_state["running"] = False


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
