"""シンプルなWebダッシュボード。

標準ライブラリのみで動作する。
使い方: python -m src.web.server
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from src.data.stock_master import get_sector, get_dividend
from src.analysis.compare import get_all_comparisons, ComparisonResult
from src.prediction.montecarlo import predict_no_contribution, predict_with_contribution

DB_DEFAULT = Path(__file__).resolve().parents[2] / "data" / "assets.db"


def _get_dates(db_path: str) -> list[str]:
    """利用可能な日付一覧を返す（新しい順）。"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT date FROM snapshots ORDER BY date DESC").fetchall()
    conn.close()
    return [r[0] for r in rows]


def _get_data(db_path: str, date: str | None = None, monthly_contribution: float = 50000) -> dict:
    conn = sqlite3.connect(db_path)
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
        {"name": r[0], "code": r[1], "asset_class": r[2], "value": r[3], "quantity": r[4]}
        for r in conn.execute(
            "SELECT name, symbol_or_code, asset_class, value, quantity FROM snapshot_holdings WHERE date = ? ORDER BY asset_class, value DESC",
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
                dividends.append({
                    "code": h["code"],
                    "name": h["name"],
                    "quantity": h["quantity"],
                    "dps": dps,
                    "annual": annual,
                })
    dividends.sort(key=lambda x: x["annual"], reverse=True)

    # 比較データ
    comparisons = get_all_comparisons(db_path, date)

    # 成長予測（追加投資なし）
    try:
        predictions, pred_params = predict_no_contribution(db_path)
    except Exception:
        predictions, pred_params = [], {}

    # 成長予測（積立込み）
    try:
        predictions_c, pred_params_c = predict_with_contribution(db_path, monthly_contribution)
    except Exception:
        predictions_c, pred_params_c = [], {}

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
        "predictions": predictions,
        "pred_params": pred_params,
        "predictions_contrib": predictions_c,
        "pred_params_contrib": pred_params_c,
        "monthly_contribution": monthly_contribution,
    }


def _build_html(data: dict, dates: list[str]) -> str:
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
    predictions = data.get("predictions", [])
    pred_params = data.get("pred_params", {})
    predictions_c = data.get("predictions_contrib", [])
    pred_params_c = data.get("pred_params_contrib", {})
    monthly_contribution = data.get("monthly_contribution", 50000)

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

    # 銘柄 rows (クラス別グループ)
    hold_rows = ""
    current_class = None
    for h in holdings:
        if h["asset_class"] != current_class:
            current_class = h["asset_class"]
            hold_rows += f'<tr class="group-header"><td colspan="4">{current_class}</td></tr>'
        code = f'<span class="code">{h["code"]}</span> ' if h["code"] else ""
        qty = f' <span class="qty">x{h["quantity"]:,.0f}</span>' if h["quantity"] else ""
        hold_rows += f'<tr><td>{code}{h["name"]}{qty}</td><td class="num">{h["value"]:,.0f}円</td></tr>'

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
        div_yield = d["annual"] / (d["dps"] / d["dps"] * d["annual"] / d["quantity"] * d["quantity"]) * 100 if d["annual"] else 0
        div_rows += f'<tr><td><span class="code">{d["code"]}</span> {d["name"]}</td>'
        div_rows += f'<td class="num">{d["quantity"]:,.0f}</td>'
        div_rows += f'<td class="num">{d["dps"]:,.1f}円</td>'
        div_rows += f'<td class="num">{d["annual"]:,.0f}円</td></tr>'

    # 比較カード HTML 生成
    compare_cards_html = ""
    compare_detail_html = ""
    for comp in comparisons:
        if comp.total_diff is not None:
            sign = "+" if comp.total_diff >= 0 else ""
            css = "plus" if comp.total_diff >= 0 else "minus"
            ratio_str = f'{sign}{comp.total_ratio:.2f}%' if comp.total_ratio is not None else ""
            compare_cards_html += f'''
    <div class="compare-card">
      <h3>{comp.label}</h3>
      <div class="diff {css}">{sign}{comp.total_diff:,.0f}円</div>
      <div class="ratio {css}">{ratio_str}</div>
      <div class="compare-date">vs {comp.compare_date}</div>
    </div>'''
            # 詳細（クラス別・上位変動）
            if comp.by_class_diff or comp.holding_diffs:
                detail_rows = ""
                for cls_name, diff in sorted(comp.by_class_diff.items(), key=lambda x: abs(x[1]), reverse=True):
                    s = "+" if diff >= 0 else ""
                    c = "plus" if diff >= 0 else "minus"
                    detail_rows += f'<tr><td>{cls_name}</td><td class="num {c}">{s}{diff:,.0f}円</td></tr>'
                hold_detail = ""
                for hd in comp.holding_diffs[:5]:
                    s = "+" if hd["diff"] >= 0 else ""
                    c = "plus" if hd["diff"] >= 0 else "minus"
                    code_s = f'<span class="code">{hd["code"]}</span> ' if hd["code"] else ""
                    hold_detail += f'<tr><td>{code_s}{hd["name"]}</td><td class="num {c}">{s}{hd["diff"]:,.0f}円</td></tr>'
                compare_detail_html += f'''
    <details class="compare-detail">
      <summary>{comp.label}の詳細（vs {comp.compare_date}）</summary>
      <table>{detail_rows}{hold_detail}</table>
    </details>'''
        else:
            compare_cards_html += f'''
    <div class="compare-card">
      <h3>{comp.label}</h3>
      <div class="no-data">データ不足</div>
    </div>'''

    # 成長予測 HTML 生成
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
          <li><strong>手法:</strong> 幾何ブラウン運動（対数正規モデル）で月次リターンを生成し、10,000回のシミュレーションを実行</li>
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
      <div class="pred-note">{note}<br>期待リターン {annual_ret:.1f}%/年　ボラティリティ {annual_vol:.1f}%/年</div>
    </div>'''

    # 積立込み成長予測 HTML 生成
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
  .total {{ font-size: 2.2rem; font-weight: 700; color: #2d3436; margin-bottom: 24px; }}
  .total span {{ font-size: 1rem; color: #636e72; font-weight: 400; }}
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
  .plus {{ color: #e74c3c; }}
  .minus {{ color: #2881D7; }}
  .no-data {{ color: #b2bec3; font-size: 0.9rem; }}
  .pred-table {{ margin-top: 12px; }}
  .pred-table th {{ font-size: 0.8rem; }}
  .pred-note {{ font-size: 0.75rem; color: #b2bec3; margin-top: 8px; }}
  .compare-detail {{ margin-top: 12px; }}
  .compare-detail summary {{ cursor: pointer; font-size: 0.85rem; color: #636e72; }}
  .compare-detail table {{ margin-top: 8px; }}
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
</style>
</head>
<body>
<div class="container">
  <h1>資産ダッシュボード</h1>
  <div class="date-picker">
    <button class="nav-btn" id="prev-btn" title="前の日">&larr;</button>
    <select id="date-select" onchange="location.href='/?date='+this.value">
      {date_options}
    </select>
    <button class="nav-btn" id="next-btn" title="次の日">&rarr;</button>
    <label>({len(dates)}日分のデータ)</label>
  </div>
  <div class="total">{total:,.0f}<span> 円</span></div>

  <div class="compare-cards">
    {compare_cards_html}
  </div>
  {compare_detail_html}

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
      <div class="dividend-monthly">月平均 {total_dividend/12:,.0f}円</div>
      <table style="margin-top:12px">
        <tr><th>銘柄</th><th class="num">保有数</th><th class="num">配当/株</th><th class="num">年間配当</th></tr>
        {div_rows}
      </table>
    </div>

    {pred_html}

    {pred_contrib_html}

    <div class="card full">
      <h2>保有銘柄 ({len(holdings)})</h2>
      <table>
        <tr><th>銘柄</th><th class="num">評価額</th></tr>
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

// 積立額変更
function updateContrib() {{
  const v = document.getElementById('contrib-input').value;
  const url = new URL(window.location);
  url.searchParams.set('contrib', v);
  location.href = url.toString();
}}
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
        {"name": "トヨタ自動車",      "code": "7203", "asset_class": "株式（現物）", "value": 1_260_000, "quantity": 300},
        {"name": "ソニーグループ",    "code": "6758", "asset_class": "株式（現物）", "value": 980_000,   "quantity": 100},
        {"name": "三菱商事",          "code": "8058", "asset_class": "株式（現物）", "value": 875_000,   "quantity": 100},
        {"name": "信越化学工業",      "code": "4063", "asset_class": "株式（現物）", "value": 720_000,   "quantity": 100},
        {"name": "日立製作所",        "code": "6501", "asset_class": "株式（現物）", "value": 685_000,   "quantity": 200},
        {"name": "キーエンス",        "code": "6861", "asset_class": "株式（現物）", "value": 650_000,   "quantity": 10},
        {"name": "任天堂",            "code": "7974", "asset_class": "株式（現物）", "value": 580_000,   "quantity": 100},
        {"name": "ダイキン工業",      "code": "6367", "asset_class": "株式（現物）", "value": 350_000,   "quantity": 100},
        {"name": "INPEX",             "code": "1605", "asset_class": "株式（現物）", "value": 250_000,   "quantity": 500},
        {"name": "eMAXIS Slim 全世界株式(オルカン)",            "code": "", "asset_class": "投資信託", "value": 2_480_000, "quantity": 680000},
        {"name": "eMAXIS Slim 米国株式(S&P500)",               "code": "", "asset_class": "投資信託", "value": 1_850_000, "quantity": 520000},
        {"name": "ニッセイ外国株式インデックスファンド",        "code": "", "asset_class": "投資信託", "value": 850_000,   "quantity": 290000},
        {"name": "不動産クラウドファンディング",                "code": "", "asset_class": "不動産",   "value": 1_200_000, "quantity": None},
        {"name": "企業型確定拠出年金",                          "code": "", "asset_class": "年金",     "value": 2_800_000, "quantity": None},
        {"name": "iDeCo（先進国株式）",                         "code": "", "asset_class": "年金",     "value": 850_000,   "quantity": None},
        {"name": "個人年金保険",                                "code": "", "asset_class": "年金",     "value": 300_000,   "quantity": None},
    ]

    # 業種別
    demo_sectors = {
        "輸送用機器": 1_260_000, "電気機器": 2_315_000, "卸売業": 875_000,
        "化学": 720_000, "その他製品": 580_000, "機械": 350_000, "鉱業": 250_000,
    }
    demo_sectors = dict(sorted(demo_sectors.items(), key=lambda x: x[1], reverse=True))

    # 配当予測
    demo_dividends = [
        {"code": "7203", "name": "トヨタ自動車",   "quantity": 300, "dps": 75,   "annual": 22_500},
        {"code": "6758", "name": "ソニーグループ", "quantity": 100, "dps": 85,   "annual": 8_500},
        {"code": "8058", "name": "三菱商事",       "quantity": 100, "dps": 100,  "annual": 10_000},
        {"code": "4063", "name": "信越化学工業",   "quantity": 100, "dps": 120,  "annual": 12_000},
        {"code": "6501", "name": "日立製作所",     "quantity": 200, "dps": 52,   "annual": 10_400},
        {"code": "6861", "name": "キーエンス",     "quantity": 10,  "dps": 300,  "annual": 3_000},
        {"code": "7974", "name": "任天堂",         "quantity": 100, "dps": 183,  "annual": 18_300},
        {"code": "6367", "name": "ダイキン工業",   "quantity": 100, "dps": 100,  "annual": 10_000},
        {"code": "1605", "name": "INPEX",          "quantity": 500, "dps": 60,   "annual": 30_000},
    ]
    demo_dividends.sort(key=lambda x: x["annual"], reverse=True)

    total_asset = sum(by_class.values())

    # 比較デモデータ
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    last_month = (date.today() - timedelta(days=30)).isoformat()
    last_year = (date.today() - timedelta(days=365)).isoformat()

    demo_comparisons = [
        ComparisonResult(
            label="前日比", target_date=today, compare_date=yesterday,
            total_diff=42_300, total_ratio=0.20,
            by_class_diff={"株式（現物）": 35_800, "投資信託": 12_500, "預金・現金・暗号資産": -6_000},
            account_diffs=[], holding_diffs=[
                {"name": "トヨタ自動車", "code": "7203", "diff": 18_000, "current": 1_260_000, "previous": 1_242_000},
                {"name": "ソニーグループ", "code": "6758", "diff": 12_500, "current": 980_000, "previous": 967_500},
            ],
        ),
        ComparisonResult(
            label="前月比", target_date=today, compare_date=last_month,
            total_diff=285_000, total_ratio=1.35,
            by_class_diff={"株式（現物）": 180_000, "投資信託": 95_000, "年金": 25_000, "預金・現金・暗号資産": -15_000},
            account_diffs=[], holding_diffs=[],
        ),
        ComparisonResult(
            label="前年比", target_date=today, compare_date=last_year,
            total_diff=3_420_000, total_ratio=18.9,
            by_class_diff={"株式（現物）": 1_650_000, "投資信託": 1_280_000, "年金": 580_000, "預金・現金・暗号資産": -90_000},
            account_diffs=[], holding_diffs=[],
        ),
    ]

    # 成長予測デモデータ
    from src.prediction.montecarlo import PredictionRange
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
    }
    # 積立込み予測デモデータ（月5万円）
    demo_predictions_c = [
        PredictionRange(years=1, p10=20_400_000, p50=22_700_000, p90=25_400_000),
        PredictionRange(years=3, p10=20_000_000, p50=26_500_000, p90=35_200_000),
        PredictionRange(years=5, p10=20_800_000, p50=31_200_000, p90=47_500_000),
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
        "predictions": demo_predictions,
        "pred_params": demo_pred_params,
        "predictions_contrib": demo_predictions_c,
        "pred_params_contrib": demo_pred_params,
        "monthly_contribution": 50000,
    }


class Handler(BaseHTTPRequestHandler):
    db_path: str = str(DB_DEFAULT)
    demo: bool = False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if parsed.path == "/api/data":
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

        else:
            if self.demo:
                data = _demo_data()
                dates = [data["date"]]
            else:
                date = params.get("date", [None])[0]
                try:
                    contrib = float(params.get("contrib", [50000])[0])
                except (ValueError, TypeError):
                    contrib = 50000
                dates = _get_dates(self.db_path)
                data = _get_data(self.db_path, date, monthly_contribution=contrib)
            html = _build_html(data, dates)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode())

    def log_message(self, format, *args):
        pass  # suppress access logs


def main() -> None:
    parser = argparse.ArgumentParser(description="資産ダッシュボード")
    parser.add_argument("--db", type=str, default=str(DB_DEFAULT))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--demo", action="store_true", help="ダミーデータで表示（SNS共有用）")
    args = parser.parse_args()

    Handler.db_path = args.db
    Handler.demo = args.demo
    server = HTTPServer(("0.0.0.0", args.port), Handler)
    mode = " [DEMO MODE]" if args.demo else ""
    print(f"Dashboard{mode}: http://localhost:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバー停止")
        server.shutdown()


if __name__ == "__main__":
    main()
