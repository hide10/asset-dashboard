"""シンプルなWebダッシュボード。

標準ライブラリのみで動作する。
使い方: python -m src.web.server
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from src.data.stock_master import get_sector, get_dividend
from src.analysis.compare import get_all_comparisons, ComparisonResult
from src.prediction.montecarlo import predict_no_contribution, predict_with_contribution, RISK_CLASSES, PredictionRange
from src.db.schema import init_db
from src.db.repository import get_cashflows, get_setting, save_setting
from src.analysis.ai_comment import generate_comments, get_comment

DB_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "assets.db"

_update_state = {"running": False, "completed_at": None}


def _get_dates(db_path: str) -> list[str]:
    """利用可能な日付一覧を返す（新しい順）。"""
    conn = init_db(db_path)
    rows = conn.execute("SELECT date FROM snapshots ORDER BY date DESC").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _get_data(db_path: str, date: str | None = None) -> dict:
    conn = init_db(db_path)
    if date is None:
        row = conn.execute("SELECT date FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
        if not row:
            conn.close()
            return {}
        date = row[0]

    row = conn.execute(
        "SELECT total_asset, by_class_json FROM snapshots WHERE date = ?", (date,)
    ).fetchone()
    if not row:
        conn.close()
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
        {"name": r[0], "code": r[1], "asset_class": r[2], "value": r[3], "quantity": r[4], "position": r[5],
         "acquisition_price": r[6], "current_price": r[7]}
        for r in conn.execute(
            "SELECT name, symbol_or_code, asset_class, value, quantity, position, acquisition_price, current_price FROM snapshot_holdings WHERE date = ? ORDER BY asset_class, value DESC",
            (date,),
        ).fetchall()
    ]

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
                dividends.append({
                    "code": h["code"],
                    "name": h["name"],
                    "quantity": h["quantity"],
                    "dps": dps,
                    "annual": annual,
                    "current_yield": current_yield,
                    "acq_yield": acq_yield,
                })
    dividends.sort(key=lambda x: x["annual"], reverse=True)

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


def _build_html(data: dict, dates: list[str], skip_update: bool = False, ai_comment: str | None = None) -> str:
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
    comparisons = data.get("comparisons", [])

    # 日付セレクタ
    date_options = ""
    for d in dates:
        sel = ' selected' if d == date else ''
        date_options += f'<option value="{d}"{sel}>{d}</option>'

    # クラス別 rows
    class_rows = ""
    colors = ["#2881D7", "#DF3727", "#FCAD4C", "#0F7F30", "#008986", "#9C39B6"]
    for i, (cls, amt) in enumerate(by_class.items()):
        ratio = amt / total * 100 if total else 0
        color = colors[i % len(colors)]
        class_rows += f"""
        <tr>
          <td><span class="dot" style="background:{color}"></span>{cls}</td>
          <td class="num">{amt:,.0f}円</td>
          <td class="num">{ratio:.1f}%</td>
          <td><div class="bar" style="width:{ratio*2}px;background:{color}"></div></td>
        </tr>"""

    # 円グラフ用データ
    pie_data = json.dumps([
        {"label": cls, "value": amt, "color": colors[i % len(colors)]}
        for i, (cls, amt) in enumerate(by_class.items())
    ], ensure_ascii=False)

    # 口座 rows
    acc_rows = ""
    for a in accounts:
        label = f'{a["institution"]} / {a["name"]}' if a["institution"] and a["institution"] != a["name"] else a["name"]
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
            hold_rows += f'<tr class="group-header"><td colspan="5">{current_class}</td></tr>'
        code = f'<span class="code">{h["code"]}</span> ' if h["code"] else ""
        qty = f' <span class="qty">x{h["quantity"]:,.0f}</span>' if h["quantity"] else ""
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
        hold_rows += f'<tr><td>{code}{h["name"]}{qty}</td><td class="num">{h["value"]:,.0f}円</td>{diff_cells}</tr>'

    # 業種別円グラフ用データ
    sector_colors = ["#2881D7", "#DF3727", "#FCAD4C", "#0F7F30", "#008986",
                     "#9C39B6", "#FF5266", "#80BD45", "#FF689A", "#1FBBDB",
                     "#FD9441", "#6C5CE7", "#00B894"]
    sector_pie_data = json.dumps([
        {"label": sec, "value": amt, "color": sector_colors[i % len(sector_colors)]}
        for i, (sec, amt) in enumerate(sector_totals.items())
    ], ensure_ascii=False)

    stock_total = sum(sector_totals.values())
    sector_rows = ""
    for i, (sec, amt) in enumerate(sector_totals.items()):
        ratio = amt / stock_total * 100 if stock_total else 0
        color = sector_colors[i % len(sector_colors)]
        sector_rows += f"""
        <tr>
          <td><span class="dot" style="background:{color}"></span>{sec}</td>
          <td class="num">{amt:,.0f}円</td>
          <td class="num">{ratio:.1f}%</td>
        </tr>"""

    # 配当予測 rows
    div_rows = ""
    for d in dividends:
        cur_y = f'{d["current_yield"]:.2f}%' if d.get("current_yield") is not None else "-"
        acq_y = f'{d["acq_yield"]:.2f}%' if d.get("acq_yield") is not None else "-"
        div_rows += f'<tr><td><span class="code">{d["code"]}</span> {d["name"]}</td>'
        div_rows += f'<td class="num">{d["quantity"]:,.0f}</td>'
        div_rows += f'<td class="num">{d["dps"]:,.1f}円</td>'
        div_rows += f'<td class="num">{d["annual"]:,.0f}円</td>'
        div_rows += f'<td class="num">{cur_y}</td>'
        div_rows += f'<td class="num">{acq_y}</td></tr>'

    # 比較カード HTML 生成
    compare_cards_html = ""
    for comp in comparisons:
        if comp.total_diff is not None:
            sign = "+" if comp.total_diff >= 0 else ""
            css = "plus" if comp.total_diff >= 0 else "minus"
            ratio_str = f'{sign}{comp.total_ratio:.2f}%' if comp.total_ratio is not None else ""
            # クラス別差分
            class_diff_html = ""
            if comp.by_class_diff:
                for cls_name, diff in sorted(comp.by_class_diff.items(), key=lambda x: abs(x[1]), reverse=True):
                    s = "+" if diff >= 0 else ""
                    c = "plus" if diff >= 0 else "minus"
                    class_diff_html += f'<div class="class-diff {c}">{cls_name} {s}{diff:,.0f}</div>'
            compare_cards_html += f'''
    <div class="compare-card">
      <h3>{comp.label}</h3>
      <div class="diff {css}">{sign}{comp.total_diff:,.0f}円</div>
      <div class="ratio {css}">{ratio_str}</div>
      <div class="compare-date">vs {comp.compare_date}</div>
      {class_diff_html}
    </div>'''
        else:
            compare_cards_html += f'''
    <div class="compare-card">
      <h3>{comp.label}</h3>
      <div class="no-data">データ不足</div>
    </div>'''


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
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
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
  .full {{ grid-column: 1 / -1; }}
  canvas {{ max-width: 280px; margin: 0 auto; display: block; }}
  .pie-wrap {{ display: flex; align-items: center; gap: 20px; }}
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
  .hold-table th {{ white-space: nowrap; }}
  .hold-table td:nth-child(n+3) {{ font-size: 0.82rem; }}
  .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }}
  .nav-link {{
    color: #2881D7; text-decoration: none; font-size: 0.9rem; font-weight: 600;
    padding: 6px 14px; border: 1px solid #2881D7; border-radius: 6px;
  }}
  .nav-link:hover {{ background: #2881D7; color: #fff; }}
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
    <div style="display:flex;gap:8px">
      <a href="/settings" class="nav-link" style="font-size:0.8rem;padding:6px 10px;border-color:#b2bec3;color:#636e72">設定</a>
      <a href="/plan" class="nav-link">ライフプラン &rarr;</a>
    </div>
  </div>
  <div class="date-picker">
    <button class="nav-btn" id="prev-btn" title="前の日">&larr;</button>
    <select id="date-select" onchange="location.href='/?date='+this.value">
      {date_options}
    </select>
    <button class="nav-btn" id="next-btn" title="次の日">&rarr;</button>
    <label>({len(dates)}日分のデータ)</label>
  </div>
  <div class="total">現在の総資産: <strong>{total:,.0f}</strong> 円 <span style="font-size:0.85rem;color:#b2bec3">({date}時点)</span></div>
  {f'<div class="ai-comment-card"><div class="ai-icon">AI</div><div class="ai-text">{ai_comment}</div></div>' if ai_comment else ''}

  <div class="compare-cards">
    {compare_cards_html}
  </div>
  <div class="grid">
    <div class="card">
      <h2>資産クラス別内訳</h2>
      <div class="pie-wrap">
        <canvas id="pie" width="220" height="220"></canvas>
        <ul class="pie-legend" id="legend"></ul>
      </div>
      <table style="margin-top:16px">
        {class_rows}
      </table>
    </div>

    <div class="card">
      <h2>口座一覧 ({len(accounts)})</h2>
      <table>
        <tr><th>口座</th><th class="num">残高</th></tr>
        {acc_rows}
      </table>
    </div>

    <div class="card">
      <h2>株式 業種別内訳</h2>
      <div class="pie-wrap">
        <canvas id="sector-pie" width="220" height="220"></canvas>
        <ul class="pie-legend" id="sector-legend"></ul>
      </div>
      <table style="margin-top:16px">
        {sector_rows}
      </table>
    </div>

    <div class="card">
      <h2>年間配当予測</h2>
      <div class="dividend-total">{total_dividend:,.0f}<span> 円/年</span></div>
      <div class="dividend-monthly">月平均 {total_dividend/12:,.0f}円{_avg_yield_html(dividends)}</div>
      <table style="margin-top:12px">
        <tr><th>銘柄</th><th class="num">保有数</th><th class="num">配当/株</th><th class="num">年間配当</th><th class="num">利回り</th><th class="num">取得利回り</th></tr>
        {div_rows}
      </table>
    </div>

    <div class="card full">
      <h2>保有銘柄 ({len(holdings)})</h2>
      <table class="hold-table">
        <tr><th>銘柄</th><th class="num">評価額</th><th class="num">前日比</th><th class="num">前月比</th><th class="num">前年比</th></tr>
        {hold_rows}
      </table>
    </div>
  </div>
</div>

<script>
// 円グラフ描画
const data = {pie_data};
const canvas = document.getElementById('pie');
const ctx = canvas.getContext('2d');
const cx = 110, cy = 110, r = 100;
let startAngle = -Math.PI / 2;
const total = data.reduce((s, d) => s + d.value, 0);
const legend = document.getElementById('legend');

data.forEach(d => {{
  const sliceAngle = (d.value / total) * 2 * Math.PI;
  ctx.beginPath();
  ctx.moveTo(cx, cy);
  ctx.arc(cx, cy, r, startAngle, startAngle + sliceAngle);
  ctx.closePath();
  ctx.fillStyle = d.color;
  ctx.fill();
  startAngle += sliceAngle;

  const li = document.createElement('li');
  li.innerHTML = '<span class="dot" style="background:' + d.color + '"></span>' + d.label;
  legend.appendChild(li);
}});

// 業種別円グラフ描画
function drawPie(canvasId, legendId, chartData) {{
  const c = document.getElementById(canvasId);
  const x = c.getContext('2d');
  const w = 110, h = 110, rad = 100;
  let angle = -Math.PI / 2;
  const t = chartData.reduce((s, d) => s + d.value, 0);
  const leg = document.getElementById(legendId);
  chartData.forEach(d => {{
    const sl = (d.value / t) * 2 * Math.PI;
    x.beginPath(); x.moveTo(w, h); x.arc(w, h, rad, angle, angle + sl);
    x.closePath(); x.fillStyle = d.color; x.fill();
    angle += sl;
    const li = document.createElement('li');
    li.innerHTML = '<span class="dot" style="background:' + d.color + '"></span>' + d.label;
    leg.appendChild(li);
  }});
}}
const sectorData = {sector_pie_data};
if (sectorData.length > 0) drawPie('sector-pie', 'sector-legend', sectorData);

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

{'// reload polling' + """
const loadedAt = new Date().toISOString();
const pollId = setInterval(async () => {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    if (s.completed_at && s.completed_at > loadedAt) {
      document.getElementById('reload-banner').style.display = 'flex';
      clearInterval(pollId);
    }
  } catch(e) {}
}, 5000);
""" if not skip_update else ''}
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
        {"name": "普通預金", "asset_class": "預金・現金・暗号資産", "balance": 1_380_000, "institution": "三井住友銀行"},
        {"name": "定期預金", "asset_class": "預金・現金・暗号資産", "balance": 1_000_000, "institution": "住信SBIネット銀行"},
        {"name": "円預金", "asset_class": "預金・現金・暗号資産", "balance": 245_000, "institution": "楽天銀行"},
        {"name": "Suica", "asset_class": "預金・現金・暗号資産", "balance": 3_200, "institution": "モバイルSuica"},
        {"name": "預り金", "asset_class": "預金・現金・暗号資産", "balance": 41_800, "institution": "SBI証券"},
    ]

    holdings = [
        {"name": "トヨタ自動車",      "code": "7203", "asset_class": "株式（現物）", "value": 1_260_000, "quantity": 300, "acquisition_price": 3_800, "current_price": 4_200},
        {"name": "ソニーグループ",    "code": "6758", "asset_class": "株式（現物）", "value": 980_000,   "quantity": 100, "acquisition_price": 8_500, "current_price": 9_800},
        {"name": "三菱商事",          "code": "8058", "asset_class": "株式（現物）", "value": 875_000,   "quantity": 100, "acquisition_price": 7_200, "current_price": 8_750},
        {"name": "信越化学工業",      "code": "4063", "asset_class": "株式（現物）", "value": 720_000,   "quantity": 100, "acquisition_price": 6_500, "current_price": 7_200},
        {"name": "日立製作所",        "code": "6501", "asset_class": "株式（現物）", "value": 685_000,   "quantity": 200, "acquisition_price": 2_800, "current_price": 3_425},
        {"name": "キーエンス",        "code": "6861", "asset_class": "株式（現物）", "value": 650_000,   "quantity": 10,  "acquisition_price": 58_000, "current_price": 65_000},
        {"name": "任天堂",            "code": "7974", "asset_class": "株式（現物）", "value": 580_000,   "quantity": 100, "acquisition_price": 5_200, "current_price": 5_800},
        {"name": "ダイキン工業",      "code": "6367", "asset_class": "株式（現物）", "value": 350_000,   "quantity": 100, "acquisition_price": 3_000, "current_price": 3_500},
        {"name": "INPEX",             "code": "1605", "asset_class": "株式（現物）", "value": 250_000,   "quantity": 500, "acquisition_price": 420,   "current_price": 500},
        {"name": "eMAXIS Slim 全世界株式(オルカン)",            "code": "", "asset_class": "投資信託", "value": 2_480_000, "quantity": 680000, "acquisition_price": None, "current_price": None},
        {"name": "eMAXIS Slim 米国株式(S&P500)",               "code": "", "asset_class": "投資信託", "value": 1_850_000, "quantity": 520000, "acquisition_price": None, "current_price": None},
        {"name": "ニッセイ外国株式インデックスファンド",        "code": "", "asset_class": "投資信託", "value": 850_000,   "quantity": 290000, "acquisition_price": None, "current_price": None},
        {"name": "不動産クラウドファンディング",                "code": "", "asset_class": "不動産",   "value": 1_200_000, "quantity": None, "acquisition_price": None, "current_price": None},
        {"name": "企業型確定拠出年金",                          "code": "", "asset_class": "年金",     "value": 2_800_000, "quantity": None, "acquisition_price": None, "current_price": None},
        {"name": "iDeCo（先進国株式）",                         "code": "", "asset_class": "年金",     "value": 850_000,   "quantity": None, "acquisition_price": None, "current_price": None},
        {"name": "個人年金保険",                                "code": "", "asset_class": "年金",     "value": 300_000,   "quantity": None, "acquisition_price": None, "current_price": None},
    ]

    # 業種別
    demo_sectors = {
        "輸送用機器": 1_260_000, "電気機器": 2_315_000, "卸売業": 875_000,
        "化学": 720_000, "その他製品": 580_000, "機械": 350_000, "鉱業": 250_000,
    }
    demo_sectors = dict(sorted(demo_sectors.items(), key=lambda x: x[1], reverse=True))

    # 配当予測
    demo_dividends = [
        {"code": "7203", "name": "トヨタ自動車",   "quantity": 300, "dps": 75,   "annual": 22_500, "current_yield": 75/4200*100,  "acq_yield": 75/3800*100},
        {"code": "6758", "name": "ソニーグループ", "quantity": 100, "dps": 85,   "annual": 8_500,  "current_yield": 85/9800*100,  "acq_yield": 85/8500*100},
        {"code": "8058", "name": "三菱商事",       "quantity": 100, "dps": 100,  "annual": 10_000, "current_yield": 100/8750*100, "acq_yield": 100/7200*100},
        {"code": "4063", "name": "信越化学工業",   "quantity": 100, "dps": 120,  "annual": 12_000, "current_yield": 120/7200*100, "acq_yield": 120/6500*100},
        {"code": "6501", "name": "日立製作所",     "quantity": 200, "dps": 52,   "annual": 10_400, "current_yield": 52/3425*100,  "acq_yield": 52/2800*100},
        {"code": "6861", "name": "キーエンス",     "quantity": 10,  "dps": 300,  "annual": 3_000,  "current_yield": 300/65000*100,"acq_yield": 300/58000*100},
        {"code": "7974", "name": "任天堂",         "quantity": 100, "dps": 183,  "annual": 18_300, "current_yield": 183/5800*100, "acq_yield": 183/5200*100},
        {"code": "6367", "name": "ダイキン工業",   "quantity": 100, "dps": 100,  "annual": 10_000, "current_yield": 100/3500*100, "acq_yield": 100/3000*100},
        {"code": "1605", "name": "INPEX",          "quantity": 500, "dps": 60,   "annual": 30_000, "current_yield": 60/500*100,   "acq_yield": 60/420*100},
    ]
    demo_dividends.sort(key=lambda x: x["annual"], reverse=True)

    total_asset = sum(by_class.values())

    # 比較デモデータ
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_month = (date.today() - timedelta(days=30)).isoformat()
    last_year = (date.today() - timedelta(days=365)).isoformat()

    # holding_diffs は (asset_class, name, current_value) でルックアップされる
    daily_hdiffs = [
        {"name": "トヨタ自動車",   "asset_class": "株式（現物）", "current": 1_260_000, "diff":  18_000},
        {"name": "ソニーグループ", "asset_class": "株式（現物）", "current":   980_000, "diff":  12_500},
        {"name": "三菱商事",       "asset_class": "株式（現物）", "current":   875_000, "diff":   8_200},
        {"name": "信越化学工業",   "asset_class": "株式（現物）", "current":   720_000, "diff":  -5_400},
        {"name": "日立製作所",     "asset_class": "株式（現物）", "current":   685_000, "diff":   9_800},
        {"name": "キーエンス",     "asset_class": "株式（現物）", "current":   650_000, "diff":  -3_200},
        {"name": "任天堂",         "asset_class": "株式（現物）", "current":   580_000, "diff":   4_100},
        {"name": "ダイキン工業",   "asset_class": "株式（現物）", "current":   350_000, "diff":  -2_500},
        {"name": "INPEX",          "asset_class": "株式（現物）", "current":   250_000, "diff":  -5_700},
        {"name": "eMAXIS Slim 全世界株式(オルカン)",      "asset_class": "投資信託", "current": 2_480_000, "diff":  8_300},
        {"name": "eMAXIS Slim 米国株式(S&P500)",         "asset_class": "投資信託", "current": 1_850_000, "diff":  5_200},
        {"name": "ニッセイ外国株式インデックスファンド",  "asset_class": "投資信託", "current":   850_000, "diff":  -1_000},
    ]
    monthly_hdiffs = [
        {"name": "トヨタ自動車",   "asset_class": "株式（現物）", "current": 1_260_000, "diff":  72_000},
        {"name": "ソニーグループ", "asset_class": "株式（現物）", "current":   980_000, "diff":  45_000},
        {"name": "三菱商事",       "asset_class": "株式（現物）", "current":   875_000, "diff":  32_000},
        {"name": "信越化学工業",   "asset_class": "株式（現物）", "current":   720_000, "diff": -18_000},
        {"name": "日立製作所",     "asset_class": "株式（現物）", "current":   685_000, "diff":  28_000},
        {"name": "キーエンス",     "asset_class": "株式（現物）", "current":   650_000, "diff":  15_000},
        {"name": "任天堂",         "asset_class": "株式（現物）", "current":   580_000, "diff":  22_000},
        {"name": "ダイキン工業",   "asset_class": "株式（現物）", "current":   350_000, "diff":  -8_000},
        {"name": "INPEX",          "asset_class": "株式（現物）", "current":   250_000, "diff": -12_000},
        {"name": "eMAXIS Slim 全世界株式(オルカン)",      "asset_class": "投資信託", "current": 2_480_000, "diff":  52_000},
        {"name": "eMAXIS Slim 米国株式(S&P500)",         "asset_class": "投資信託", "current": 1_850_000, "diff":  38_000},
        {"name": "ニッセイ外国株式インデックスファンド",  "asset_class": "投資信託", "current":   850_000, "diff":   5_000},
        {"name": "企業型確定拠出年金",                    "asset_class": "年金",     "current": 2_800_000, "diff":  18_000},
        {"name": "iDeCo（先進国株式）",                   "asset_class": "年金",     "current":   850_000, "diff":   7_000},
    ]
    yearly_hdiffs = [
        {"name": "トヨタ自動車",   "asset_class": "株式（現物）", "current": 1_260_000, "diff": 320_000},
        {"name": "ソニーグループ", "asset_class": "株式（現物）", "current":   980_000, "diff": 215_000},
        {"name": "三菱商事",       "asset_class": "株式（現物）", "current":   875_000, "diff": 195_000},
        {"name": "信越化学工業",   "asset_class": "株式（現物）", "current":   720_000, "diff": 140_000},
        {"name": "日立製作所",     "asset_class": "株式（現物）", "current":   685_000, "diff": 285_000},
        {"name": "キーエンス",     "asset_class": "株式（現物）", "current":   650_000, "diff": 180_000},
        {"name": "任天堂",         "asset_class": "株式（現物）", "current":   580_000, "diff": 125_000},
        {"name": "ダイキン工業",   "asset_class": "株式（現物）", "current":   350_000, "diff":  60_000},
        {"name": "INPEX",          "asset_class": "株式（現物）", "current":   250_000, "diff": 130_000},
        {"name": "eMAXIS Slim 全世界株式(オルカン)",      "asset_class": "投資信託", "current": 2_480_000, "diff": 680_000},
        {"name": "eMAXIS Slim 米国株式(S&P500)",         "asset_class": "投資信託", "current": 1_850_000, "diff": 520_000},
        {"name": "ニッセイ外国株式インデックスファンド",  "asset_class": "投資信託", "current":   850_000, "diff":  80_000},
        {"name": "企業型確定拠出年金",                    "asset_class": "年金",     "current": 2_800_000, "diff": 420_000},
        {"name": "iDeCo（先進国株式）",                   "asset_class": "年金",     "current":   850_000, "diff": 160_000},
    ]

    demo_comparisons = [
        ComparisonResult(
            label="前日比", target_date=today, compare_date=yesterday,
            total_diff=42_300, total_ratio=0.20,
            by_class_diff={"株式（現物）": 35_800, "投資信託": 12_500, "預金・現金・暗号資産": -6_000},
            account_diffs=[], holding_diffs=daily_hdiffs,
        ),
        ComparisonResult(
            label="前月比", target_date=today, compare_date=last_month,
            total_diff=285_000, total_ratio=1.35,
            by_class_diff={"株式（現物）": 180_000, "投資信託": 95_000, "年金": 25_000, "預金・現金・暗号資産": -15_000},
            account_diffs=[], holding_diffs=monthly_hdiffs,
        ),
        ComparisonResult(
            label="前年比", target_date=today, compare_date=last_year,
            total_diff=3_420_000, total_ratio=18.9,
            by_class_diff={"株式（現物）": 1_650_000, "投資信託": 1_280_000, "年金": 580_000, "預金・現金・暗号資産": -90_000},
            account_diffs=[], holding_diffs=yearly_hdiffs,
        ),
    ]

    return {
        "date": today,
        "total_asset": total_asset,
        "by_class": by_class,
        "accounts": accounts,
        "holdings": holdings,
        "sector_totals": demo_sectors,
        "dividends": demo_dividends,
        "total_dividend": sum(d["annual"] for d in demo_dividends),
        "comparisons": demo_comparisons,
    }


def _calc_monthly_totals(conn: sqlite3.Connection) -> list[dict]:
    """スナップショットから月末の総資産推移を返す。

    各月の最終スナップショットを採用する。
    Returns: [{"year_month": "2026-02", "total": ...}, ...] 古い順
    """
    rows = conn.execute(
        "SELECT date, total_asset FROM snapshots ORDER BY date ASC"
    ).fetchall()
    if not rows:
        return []

    # 月ごとの最終値を集める
    monthly_end: dict[str, float] = {}
    for date_str, total in rows:
        ym = date_str[:7]  # "YYYY-MM"
        monthly_end[ym] = total  # 後勝ちで最終日の値が残る

    return [
        {"year_month": ym, "total": monthly_end[ym]}
        for ym in sorted(monthly_end.keys())
    ]


def _get_plan_data(db_path: str, monthly_contribution: float | None = None) -> dict:
    """月次収支 + 成長予測データを取得する。"""
    conn = init_db(db_path)

    # 積立額: 引数指定があればDBに保存、なければDBから読む
    if monthly_contribution is not None:
        save_setting(conn, "monthly_contribution", str(int(monthly_contribution)))
    else:
        monthly_contribution = float(get_setting(conn, "monthly_contribution", "50000"))

    # 最新スナップショット情報を取得
    row = conn.execute("SELECT date, total_asset, by_class_json FROM snapshots ORDER BY date DESC LIMIT 1").fetchone()
    if not row:
        conn.close()
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

    conn.close()

    # リスク資産 / 安全資産の分離
    risk_value = sum(v for cls, v in by_class.items() if cls in RISK_CLASSES)
    safe_value = total_asset - risk_value

    # 成長予測（追加投資なし）
    try:
        predictions, pred_params = predict_no_contribution(db_path, risk_value, safe_value)
    except Exception:
        predictions, pred_params = [], {}

    # 成長予測（積立込み）
    try:
        predictions_c, pred_params_c = predict_with_contribution(db_path, risk_value, safe_value, monthly_contribution)
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
    }


def _build_plan_html(data: dict, skip_update: bool = False, ai_comment: str | None = None) -> str:
    """ライフプランニングページの HTML を生成する。"""
    if not data:
        return "<html><body><h1>データがありません</h1><p><a href='/'>ダッシュボードに戻る</a></p></body></html>"

    date = data["date"]
    total_asset = data["total_asset"]
    cashflows = data.get("cashflows", [])
    monthly_totals = data.get("monthly_totals", [])
    predictions = data.get("predictions", [])
    pred_params = data.get("pred_params", {})
    predictions_c = data.get("predictions_contrib", [])
    pred_params_c = data.get("pred_params_contrib", {})
    monthly_contribution = data.get("monthly_contribution", 50000)

    # --- セクション1: 月次資産推移 ---
    totals_chart_data = json.dumps(monthly_totals, ensure_ascii=False)

    totals_rows = ""
    for mt in monthly_totals:
        totals_rows += f'<tr><td>{mt["year_month"]}</td><td class="num">{mt["total"]:,.0f}円</td></tr>'

    # --- セクション2: 月次収支（MF集計、参考） ---
    cf_chart_data = json.dumps(cashflows, ensure_ascii=False)

    cf_rows = ""
    for cf in cashflows:
        net = cf["income"] - cf["expense"]
        sign = "+" if net >= 0 else ""
        css = "plus" if net >= 0 else "minus"
        cf_rows += f'''<tr>
          <td>{cf["year_month"]}</td>
          <td class="num">{cf["income"]:,.0f}円</td>
          <td class="num">{cf["expense"]:,.0f}円</td>
          <td class="num {css}">{sign}{net:,.0f}円</td>
        </tr>'''

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
        note = "※ デフォルトパラメータ使用（データ蓄積中）" if is_est else f'※ {pred_params.get("data_points", 0)}日分のデータから推定'
        annual_ret = pred_params.get("annual_return", 0) * 100
        annual_vol = pred_params.get("annual_volatility", 0) * 100
        p_risk = pred_params.get("risk_value", 0)
        p_safe = pred_params.get("safe_value", 0)
        pred_html = f'''
    <div class="card">
      <div class="card-header">
        <h2>成長予測（追加投資なし）</h2>
        <button class="info-btn" onclick="document.getElementById('pred-info').classList.toggle('show')" title="予測手法について">?</button>
      </div>
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
    </div>'''

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
    <div class="card">
      <h2>成長予測（積立込み）</h2>
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
  .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
  h1 {{ font-size: 1.5rem; }}
  .nav-link {{
    color: #2881D7; text-decoration: none; font-size: 0.9rem; font-weight: 600;
    padding: 6px 14px; border: 1px solid #2881D7; border-radius: 6px;
  }}
  .nav-link:hover {{ background: #2881D7; color: #fff; }}
  .total {{ font-size: 1.4rem; font-weight: 700; color: #636e72; margin-bottom: 24px; }}
  .total strong {{ color: #2d3436; font-size: 1.8rem; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }}
  .card {{ background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .card h2 {{ font-size: 1.1rem; margin-bottom: 12px; color: #2d3436; }}
  .full {{ grid-column: 1 / -1; }}
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
    <a href="/" class="nav-link">&larr; ダッシュボード</a>
  </div>
  <div class="total">現在の総資産: <strong>{total_asset:,.0f}</strong> 円 <span style="font-size:0.85rem;color:#b2bec3">({date}時点)</span></div>
  {f'<div class="ai-comment-card"><div class="ai-icon">AI</div><div class="ai-text">{ai_comment}</div></div>' if ai_comment else ''}

  <div class="grid">
    <div class="card full">
      <h2>月次資産推移</h2>
      {'<canvas id="totals-chart" height="200"></canvas>' if monthly_totals else ''}
      {f"""<table style="margin-top:16px">
        <tr><th>月</th><th class="num">月末総資産</th></tr>
        {totals_rows}
      </table>""" if monthly_totals else '<div class="no-data">複数月のスナップショットが必要です。日次取得を続けるとデータが蓄積されます。</div>'}
    </div>

    <div class="card full">
      <h2>月次収支</h2>
      {'<canvas id="cf-chart" height="200"></canvas>' if cashflows else ''}
      {f"""<table style="margin-top:16px">
        <tr><th>月</th><th class="num">収入</th><th class="num">支出</th><th class="num">収支</th></tr>
        {cf_rows}
      </table>
      <div class="pred-note" style="margin-top:8px">※ 支出には積立投資・貯蓄性の振替を含みます</div>""" if cashflows else '<div class="no-data">月次収支データがありません。<code>python -m src.daily</code> を実行すると取得されます。</div>'}
    </div>

    {pred_html}

    {pred_contrib_html}
  </div>
</div>

<script>
// 月次資産推移（折れ線グラフ）
const totalsData = {totals_chart_data};
const totalsCanvas = document.getElementById('totals-chart');
if (totalsData.length > 0 && totalsCanvas) {{
  const ctx = totalsCanvas.getContext('2d');
  const W = totalsCanvas.parentElement.clientWidth - 40;
  totalsCanvas.width = W;
  totalsCanvas.height = 220;

  const labels = totalsData.map(d => d.year_month.substring(5));
  const values = totalsData.map(d => d.total);
  const minVal = Math.min(...values) * 0.95;
  const maxVal = Math.max(...values) * 1.05;
  const range = maxVal - minVal || 1;

  const padding = {{ left: 80, right: 20, top: 20, bottom: 30 }};
  const chartW = W - padding.left - padding.right;
  const chartH = 220 - padding.top - padding.bottom;

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

  // 折れ線
  ctx.strokeStyle = '#2881D7';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  values.forEach((v, i) => {{
    const x = padding.left + (chartW / (values.length - 1 || 1)) * i;
    const y = padding.top + chartH * (1 - (v - minVal) / range);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();

  // ドット + ラベル
  values.forEach((v, i) => {{
    const x = padding.left + (chartW / (values.length - 1 || 1)) * i;
    const y = padding.top + chartH * (1 - (v - minVal) / range);
    ctx.fillStyle = '#2881D7';
    ctx.beginPath(); ctx.arc(x, y, 4, 0, Math.PI*2); ctx.fill();
    ctx.fillStyle = '#636e72';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(labels[i], x, padding.top + chartH + 18);
  }});
}}

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

{'// reload polling' + """
const loadedAt = new Date().toISOString();
const pollId = setInterval(async () => {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    if (s.completed_at && s.completed_at > loadedAt) {
      document.getElementById('reload-banner').style.display = 'flex';
      clearInterval(pollId);
    }
  } catch(e) {}
}, 5000);
""" if not skip_update else ''}
</script>
</body>
</html>"""


def _build_settings_html(db_path: str, saved: str | None = None) -> str:
    """設定ページのHTMLを生成する。"""
    import os
    conn = init_db(db_path)
    db_key = get_setting(conn, "gemini_api_key", "")
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

    saved_msg = '<div class="saved-msg">設定を保存しました。AIコメントをバックグラウンドで生成中です — 数十秒後にダッシュボードを開くと表示されます。</div>' if saved else ''

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
  .page-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }}
  h1 {{ font-size: 1.5rem; }}
  .nav-link {{
    color: #2881D7; text-decoration: none; font-size: 0.9rem; font-weight: 600;
    padding: 6px 14px; border: 1px solid #2881D7; border-radius: 6px;
  }}
  .nav-link:hover {{ background: #2881D7; color: #fff; }}
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
    <a href="/" class="nav-link">&larr; ダッシュボード</a>
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
</div>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    db_path: str = str(DB_DEFAULT)
    demo: bool = False
    skip_update: bool = False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/api/status":
            payload = {
                "updating": _update_state["running"],
                "completed_at": _update_state["completed_at"],
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
                try:
                    contrib = float(params["contrib"][0])
                except (ValueError, TypeError):
                    pass
            if self.demo:
                data = _demo_plan_data()
                ai_comment = "直近6ヶ月で資産は約1,970万円から2,150万円へ着実に増加しており、月平均+30万円の成長ペースです。月次収支は概ね黒字を維持していますが、12月のように支出が膨らむ月もあるため、臨時出費への備えも意識しましょう。モンテカルロ・シミュレーションでは、月5万円の積立を継続した場合、5年後の中央値は約3,120万円と見込まれ、長期的な資産形成は順調と言えます。"
            else:
                data = _get_plan_data(self.db_path, contrib)
                ai_comment = None
                if data:
                    try:
                        conn = init_db(self.db_path)
                        ai_comment = get_comment(conn, data["date"], "lifeplan")
                        conn.close()
                    except Exception:
                        pass
            html = _build_plan_html(data, self.skip_update, ai_comment=ai_comment)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

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
                        conn = init_db(self.db_path)
                        ai_comment = get_comment(conn, data["date"], "dashboard")
                        conn.close()
                    except Exception:
                        pass
            html = _build_html(data, dates, self.skip_update, ai_comment=ai_comment)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        elif parsed.path == "/settings":
            saved = params.get("saved", [None])[0]
            html = _build_settings_html(self.db_path, saved=saved)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

        elif parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()

        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h1>404 Not Found</h1></body></html>")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/settings":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            post_params = parse_qs(body)
            api_key = post_params.get("gemini_api_key", [""])[0].strip()
            conn = init_db(self.db_path)
            if api_key:
                save_setting(conn, "gemini_api_key", api_key)
            else:
                conn.execute("DELETE FROM settings WHERE key = 'gemini_api_key'")
                conn.commit()
            conn.close()
            # キーが設定されたら即座にAIコメント生成を試みる（バックグラウンド）
            if api_key:
                t = threading.Thread(target=_generate_ai_comments, args=(self.db_path,), daemon=True)
                t.start()
            self.send_response(303)
            self.send_header("Location", "/settings?saved=1")
            self.end_headers()
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
    last = get_setting(conn, "last_fetch_at")
    has_snapshots = conn.execute("SELECT 1 FROM snapshots LIMIT 1").fetchone() is not None
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
        print(f"[ai] AI分析エラー: {e}")


def _bg_worker(db_path: str) -> None:
    """バックグラウンドでデータ取得・配当更新・AI分析を実行する。"""
    _update_state["running"] = True
    try:
        import asyncio
        from src.daily import run
        asyncio.run(run())

        if _needs_dividend_update():
            from src.data.dividend_fetcher import update_all_dividends
            update_all_dividends()

        _generate_ai_comments(db_path)

        _update_state["completed_at"] = datetime.now().isoformat()
        print("[auto] バックグラウンド更新完了")
    except Exception as e:
        print(f"[auto] バックグラウンド更新失敗: {e}")
    finally:
        _update_state["running"] = False


def _start_bg_update(db_path: str) -> None:
    """必要に応じてバックグラウンドでデータ更新を開始する。"""
    if _should_update(db_path):
        print("[auto] バックグラウンドでデータ更新を開始します...")
        t = threading.Thread(target=_bg_worker, args=(db_path,), daemon=True)
        t.start()
    else:
        print("[auto] データは最新です — スキップ")


def _kill_existing(port: int) -> None:
    """指定ポートを使用している既存プロセスを停止する。"""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"], capture_output=True, text=True
        )
        pids = result.stdout.strip().split()
        if pids:
            print(f"ポート {port} の既存プロセス (PID: {', '.join(pids)}) を停止します...")
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
    parser = argparse.ArgumentParser(description="資産ダッシュボード")
    parser.add_argument("--db", type=str, default=str(DB_DEFAULT))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="ダミーデータで表示（SNS共有用）")
    parser.add_argument("--skip-update", action="store_true",
                        help="起動時の自動更新をスキップ")
    args = parser.parse_args()

    skip_update = args.demo or args.skip_update
    if not skip_update:
        _start_bg_update(args.db)

    Handler.db_path = args.db
    Handler.demo = args.demo
    Handler.skip_update = skip_update

    try:
        server = HTTPServer(("0.0.0.0", args.port), Handler)
    except OSError as e:
        if "Address already in use" in str(e):
            _kill_existing(args.port)
            server = HTTPServer(("0.0.0.0", args.port), Handler)
        else:
            raise
    mode = " [DEMO MODE]" if args.demo else ""
    print(f"Dashboard{mode}: http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバー停止")
        server.shutdown()


if __name__ == "__main__":
    main()
