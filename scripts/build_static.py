"""静的 HTML を生成するビルドスクリプト。

既存の _build_*_html() を流用して dist/（または指定先）に出力する。
シミュレーターページは fetch('/api/simulator') を JS Monte Carlo に置き換える。

Usage:
    python -m scripts.build_static
    python -m scripts.build_static --mode demo --output dist
    python -m scripts.build_static --mode live --db-path data/assets.db --output dist
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

# server.py からデモデータ生成関数と HTML ビルド関数を import
from src.web.server import (
    _build_cf_html,
    _build_html,
    _build_plan_html,
    _build_simulator_html,
    _demo_cf_data,
    _demo_data,
    _demo_plan_data,
    _demo_simulator_data,
    _get_cf_data,
    _get_data,
    _get_dates,
    _get_plan_data,
    _get_simulator_data,
)

DOCS_DIR = Path(__file__).resolve().parents[1] / "dist"
REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_URL = "https://github.com/hide10/asset-dashboard"

# GitHub Pages 用デモバナー
_STATIC_BANNER = (
    '<div style="background:#DF3727;color:#fff;text-align:center;padding:8px 12px;'
    'font-size:0.8rem;font-weight:700;letter-spacing:0.05em">'
    "DEMO — 表示データはすべてダミーです "
    f'<a href="{REPO_URL}" style="color:#fff;text-decoration:underline;margin-left:8px">'
    "GitHub</a></div>"
)

# --- ナビリンク変換マッピング ---
_NAV_LINK_MAP = {
    'href="/"': 'href="index.html"',
    'href="/cf"': 'href="cf.html"',
    'href="/plan"': 'href="plan.html"',
    'href="/simulator"': 'href="simulator.html"',
}

# デモリセットボタンのデフォルト値
_DEMO_INITIAL_INVESTMENT = 11_530_000
_DEMO_SAFE_VALUE = 9_970_000
_DEMO_MONTHLY_CONTRIBUTION = 50_000


# ---------------------------------------------------------------------------
# Monte Carlo JavaScript（Python の run_lifecycle_simulation() を 1:1 移植）
# ---------------------------------------------------------------------------
_MONTECARLO_JS = r"""
<script>
// --- Mulberry32 PRNG (seeded 32-bit PRNG) ---
function mulberry32(seed) {
  let s = seed | 0;
  return function() {
    s = (s + 0x6D2B79F5) | 0;
    let t = Math.imul(s ^ (s >>> 15), 1 | s);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// --- Box-Muller 変換 (standard normal) ---
function gaussRandom(rng) {
  let u1, u2;
  do { u1 = rng(); } while (u1 === 0);
  u2 = rng();
  return Math.sqrt(-2.0 * Math.log(u1)) * Math.cos(2.0 * Math.PI * u2);
}

// --- ライフサイクル Monte Carlo シミュレーション ---
function runLifecycleSimulationJS(params) {
  const {
    current_age, retirement_age, end_age,
    initial_investment, monthly_contribution,
    annual_return, annual_volatility,
    monthly_withdrawal,
    inflation_rate = 0.0,
    expense_ratio = 0.0,
    pension_start_age = 65,
    monthly_pension = 0.0,
    other_monthly_income = 0.0,
    tax_rate = 0.20315,
    safe_value = 0.0,
    simulations = 2000,
    rng_seed = 42,
  } = params;

  const rng = mulberry32(rng_seed);

  // Drift adjustment: inflation + expense ratio
  const drift = (1 + annual_return) / ((1 + inflation_rate) * (1 + expense_ratio)) - 1;
  const monthlyDrift = drift / 12;
  const monthlyVol = annual_volatility / Math.sqrt(12);

  const totalYears = end_age - current_age;
  const totalMonths = totalYears * 12;
  const accumulationMonths = (retirement_age - current_age) * 12;

  // Total principal
  const totalPrincipal = initial_investment + safe_value + monthly_contribution * accumulationMonths;

  // Store yearly balances per simulation
  const simYearly = [];
  let depletedCount = 0;
  let principalLossCount = 0;
  const simFinal = [];
  const simTaxTotal = [];

  for (let s = 0; s < simulations; s++) {
    let risk = initial_investment;
    let safe = safe_value;
    let costBasis = initial_investment;
    let taxCumulative = 0.0;
    let depleted = false;
    const yearlyValues = [];

    for (let month = 0; month < totalMonths; month++) {
      if (depleted) {
        if (month % 12 === 11) yearlyValues.push(0.0);
        continue;
      }

      // GBM growth (risk asset only)
      const z = gaussRandom(rng);
      const growth = Math.exp((monthlyDrift - 0.5 * monthlyVol * monthlyVol) + monthlyVol * z);
      risk = risk * growth;

      if (month < accumulationMonths) {
        // Accumulation phase
        risk += monthly_contribution;
        costBasis += monthly_contribution;
      } else {
        // Drawdown phase
        const yearInSim = Math.floor(month / 12);
        const age = current_age + yearInSim;

        // Income (pension + other) -> safe asset
        let income = other_monthly_income;
        if (age >= pension_start_age) income += monthly_pension;
        safe += income;

        // Tax on risk asset gains
        const withdrawal = monthly_withdrawal;
        if (risk > 0 && costBasis < risk) {
          const gainRatio = (risk - costBasis) / risk;
          const riskWithdrawal = Math.min(risk, Math.max(0.0, withdrawal - safe));
          if (riskWithdrawal > 0) {
            const tax = riskWithdrawal * gainRatio * tax_rate;
            taxCumulative += tax;
            risk -= tax;
          }
        }

        // Withdraw: safe first, then risk
        if (safe >= withdrawal) {
          safe -= withdrawal;
        } else {
          const remainder = withdrawal - safe;
          safe = 0.0;
          if (risk > 0) {
            costBasis -= remainder * (costBasis / risk);
            if (costBasis < 0) costBasis = 0;
          }
          risk -= remainder;
        }

        if (risk + safe <= 0) {
          risk = 0.0;
          safe = 0.0;
          depleted = true;
        }
      }

      // Record at year-end
      if (month % 12 === 11) {
        yearlyValues.push(Math.max(risk + safe, 0.0));
      }
    }

    const totalValue = Math.max(risk + safe, 0.0);
    simYearly.push(yearlyValues);
    simFinal.push(totalValue);
    simTaxTotal.push(taxCumulative);

    if (depleted) depletedCount++;
    if (totalValue < totalPrincipal) principalLossCount++;
  }

  // Percentile aggregation per year
  const yearlyBalances = [];
  for (let yi = 0; yi < totalYears; yi++) {
    const age = current_age + yi + 1;
    const values = [];
    for (let s = 0; s < simulations; s++) {
      if (yi < simYearly[s].length) values.push(simYearly[s][yi]);
    }
    values.sort((a, b) => a - b);
    const n = values.length;
    if (n === 0) continue;
    yearlyBalances.push({
      age: age,
      p10: values[Math.floor(n * 0.10)],
      p25: values[Math.floor(n * 0.25)],
      p50: values[Math.floor(n * 0.50)],
      p75: values[Math.floor(n * 0.75)],
      p90: values[Math.floor(n * 0.90)],
    });
  }

  // P50 financial summary
  simFinal.sort((a, b) => a - b);
  simTaxTotal.sort((a, b) => a - b);
  const n = simFinal.length;
  const netFinalP50 = simFinal[Math.floor(n * 0.50)];
  const totalTaxP50 = simTaxTotal[Math.floor(n * 0.50)];
  const totalGains = netFinalP50 + totalTaxP50 - totalPrincipal;

  return {
    ok: true,
    yearly_balances: yearlyBalances,
    depletion_probability: depletedCount / simulations,
    principal_loss_probability: principalLossCount / simulations,
    total_principal: totalPrincipal,
    total_gains: totalGains,
    total_tax: totalTaxP50,
    net_final: netFinalP50,
  };
}
</script>
"""


def _rewrite_nav_links(html: str) -> str:
    """ナビリンクをサーバーパスから相対ファイルパスに変換する。"""
    for old, new in _NAV_LINK_MAP.items():
        html = html.replace(old, new)
    # 設定ページのリンクを除去（静的版には設定ページがない）
    html = re.sub(r'<a href="/settings"[^>]*>設定</a>', "", html)
    return html


def _inject_banner(html: str) -> str:
    """<body> 直後にデモバナーを挿入する。"""
    return html.replace("<body>", "<body>\n" + _STATIC_BANNER, 1)


def _postprocess_common(html: str, include_banner: bool = True) -> str:
    """全ページ共通の後処理。"""
    html = _rewrite_nav_links(html)
    if include_banner:
        html = _inject_banner(html)
    return html


def _build_recalc_js_replacement() -> str:
    """recalcSimulator() の fetch 版を JS Monte Carlo 版に置き換える関数本体。"""
    return """\
async function recalcSimulator() {
  if (_recalcInFlight) return;
  _recalcInFlight = true;
  const loading = document.getElementById('sim-loading');
  loading.style.display = 'inline';

  const params = {};
  const fields = ['current_age','retirement_age','end_age','initial_investment','safe_value','monthly_contribution',
    'annual_return','annual_volatility','monthly_withdrawal','inflation_rate','expense_ratio',
    'pension_start_age','monthly_pension','other_monthly_income'];
  fields.forEach(f => {
    const el = document.getElementById(f);
    params[f] = el.classList.contains('money-input') ? parseMoney(el.value) : parseFloat(el.value);
  });

  try {
    const data = runLifecycleSimulationJS(params);
    if (!data.ok) {
      if (data.error) alert(data.error);
    } else {
      updateSummary(data);
      updateProjection(data.yearly_balances, params.retirement_age);
      _initBalances = data.yearly_balances;
      drawFanChart(data.yearly_balances, params.retirement_age);
    }
  } catch(e) {
    console.error('Simulator error:', e);
  } finally {
    _recalcInFlight = false;
    loading.style.display = 'none';
  }
}"""


def _build_reset_js_replacement() -> str:
    """resetFromData() の fetch 版をデモデフォルト値版に置き換える関数本体。"""
    ii = f"{_DEMO_INITIAL_INVESTMENT:,}"
    sv = f"{_DEMO_SAFE_VALUE:,}"
    mc = f"{_DEMO_MONTHLY_CONTRIBUTION:,}"
    return f"""\
async function resetFromData() {{
  if (!confirm('パラメータをデフォルト値に戻します。\\n他のパラメータはそのまま維持されます。')) return;
  const btn = document.getElementById('sim-reset-btn');
  const loading = document.getElementById('sim-loading');
  btn.disabled = true;
  loading.style.display = 'inline';
  try {{
    const ii = document.getElementById('initial_investment');
    const sv = document.getElementById('safe_value');
    const mc = document.getElementById('monthly_contribution');
    if (ii) ii.value = '{ii}';
    if (sv) sv.value = '{sv}';
    if (mc) mc.value = '{mc}';
    await recalcSimulator();
  }} catch(e) {{
    console.error('Reset error:', e);
  }} finally {{
    btn.disabled = false;
    loading.style.display = 'none';
  }}
}}"""


def _postprocess_simulator(html: str) -> str:
    """シミュレーター HTML の後処理: API fetch を JS Monte Carlo に置換。"""
    # 1. recalcSimulator() を置換
    #    async function recalcSimulator() { ... } の全体をマッチ
    html = _replace_js_function(html, "recalcSimulator", _build_recalc_js_replacement())

    # 2. resetFromData() を置換
    html = _replace_js_function(html, "resetFromData", _build_reset_js_replacement())

    # 3. ボタンテキスト変更
    html = html.replace("実データから再取得", "デフォルトに戻す")

    # 4. Monte Carlo JS を </head> 前に挿入
    html = html.replace("</head>", _MONTECARLO_JS + "\n</head>", 1)

    return html


def _replace_js_function(html: str, func_name: str, replacement: str) -> str:
    """HTML 内の JavaScript 関数定義をブレースマッチングで置換する。

    async function funcName() { ... } を replacement に置き換える。
    """
    # 関数の開始位置を見つける
    pattern = re.compile(rf"(async\s+)?function\s+{re.escape(func_name)}\s*\(")
    match = pattern.search(html)
    if not match:
        return html

    start = match.start()

    # 最初の { を見つける
    brace_start = html.index("{", match.end())
    depth = 1
    pos = brace_start + 1
    while depth > 0 and pos < len(html):
        ch = html[pos]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "'" or ch == '"' or ch == "`":
            # 文字列リテラルをスキップ
            quote = ch
            pos += 1
            while pos < len(html) and html[pos] != quote:
                if html[pos] == "\\":
                    pos += 1  # エスケープ文字をスキップ
                pos += 1
        pos += 1

    end = pos  # } の次の位置

    return html[:start] + replacement + html[end:]


def _postprocess_plan(html: str) -> str:
    """プランページの後処理: updateContrib() の location.href 遷移を無効化。"""
    # updateContrib() の location.href をコメントアウト
    html = html.replace(
        "location.href = url.toString();",
        "// location.href = url.toString(); // disabled in static demo",
    )
    return html


def _prepare_output_dir(output_dir: Path) -> Path:
    """出力先を安全に初期化する。

    デフォルトの dist/ は生成物として削除してよいが、任意指定された
    空でない既存ディレクトリは誤削除を避けるため拒否する。
    """
    target = output_dir.resolve()
    default_target = DOCS_DIR.resolve()
    dangerous_targets = {
        REPO_ROOT.resolve(),
        REPO_ROOT.resolve().parent,
        Path.home().resolve(),
        Path("/").resolve(),
        Path.cwd().resolve(),
    }
    if target in dangerous_targets:
        raise ValueError(f"危険な出力先です: {target}")

    if target.exists():
        if not target.is_dir():
            raise ValueError(f"出力先がディレクトリではありません: {target}")
        if any(target.iterdir()):
            if target != default_target:
                raise ValueError(f"出力先が空ではありません。既存ファイルを保護するため削除しません: {target}")
            shutil.rmtree(target)
        else:
            target.rmdir()

    target.mkdir(parents=True)
    return target


def build(output_dir: Path | None = None, mode: str = "demo", db_path: str = "data/assets.db") -> Path:
    """静的 HTML をビルドして output_dir に出力する。"""
    target = _prepare_output_dir(output_dir or DOCS_DIR)
    is_demo = mode == "demo"

    # .nojekyll を作成
    (target / ".nojekyll").touch()

    if is_demo:
        # --- ダッシュボード（demo） ---
        dash_data = _demo_data()
        dash_dates = [dash_data["date"]]
        ai_comment = (
            "総資産約2,150万円のポートフォリオは、株式・投資信託・預金・年金にバランスよく分散されています。"
            "前日比+4.2万円、前月比+28.5万円と堅調に推移しており、特にリスク資産（株式+投信）の貢献が大きいです。"
            "年間配当予測は約12.5万円（利回り約1.9%）で、高配当銘柄の追加や業種の偏り（電気機器が大きい）"
            "の分散を検討すると、より安定したポートフォリオになるでしょう。"
        )
        dash_html = _build_html(dash_data, dash_dates, skip_update=True, ai_comment=ai_comment, demo=True)
    else:
        # --- ダッシュボード（live） ---
        dash_data = _get_data(db_path)
        dash_dates = _get_dates(db_path)
        if not dash_data:
            raise RuntimeError("ダッシュボード用データがありません。先に src.daily を実行してください。")
        dash_html = _build_html(dash_data, dash_dates, skip_update=True, ai_comment=None, demo=False)
    dash_html = _postprocess_common(dash_html, include_banner=is_demo)
    (target / "index.html").write_text(dash_html, encoding="utf-8")

    # --- 家計簿分析 ---
    if is_demo:
        cf_data = _demo_cf_data()
        cf_ai = (
            "今月の支出は食費と日用品が予算を若干上回っていますが、全体では収支プラスを維持しています。"
            "固定費率は約40%と標準的で、通信費や保険の見直し余地があります。"
            "来月は食費の予算管理を意識すると、さらに貯蓄率を改善できるでしょう。"
        )
        cf_html = _build_cf_html(cf_data, skip_update=True, ai_comment=cf_ai)
    else:
        cf_data = _get_cf_data(db_path)
        if not cf_data:
            raise RuntimeError("家計簿用データがありません。先に家計簿CSVを取得してください。")
        cf_html = _build_cf_html(cf_data, skip_update=True, ai_comment=None)
    cf_html = _postprocess_common(cf_html, include_banner=is_demo)
    (target / "cf.html").write_text(cf_html, encoding="utf-8")

    # --- ライフプラン ---
    if is_demo:
        plan_data = _demo_plan_data()
        plan_ai = (
            "直近6ヶ月で資産は約1,970万円から2,150万円へ着実に増加しており、月平均+30万円の成長ペースです。"
            "月次収支は概ね黒字を維持していますが、12月のように支出が膨らむ月もあるため、"
            "臨時出費への備えも意識しましょう。モンテカルロ・シミュレーションでは、月5万円の積立を継続した場合、"
            "5年後の中央値は約3,120万円と見込まれ、長期的な資産形成は順調と言えます。"
        )
        plan_html = _build_plan_html(plan_data, skip_update=True, ai_comment=plan_ai)
    else:
        plan_data = _get_plan_data(db_path)
        if not plan_data:
            raise RuntimeError("ライフプラン用データがありません。先に src.daily を実行してください。")
        plan_html = _build_plan_html(plan_data, skip_update=True, ai_comment=None)
    plan_html = _postprocess_common(plan_html, include_banner=is_demo)
    plan_html = _postprocess_plan(plan_html)
    (target / "plan.html").write_text(plan_html, encoding="utf-8")

    # --- シミュレーター ---
    sim_data = _demo_simulator_data() if is_demo else _get_simulator_data(db_path)
    sim_html = _build_simulator_html(sim_data, skip_update=True)
    sim_html = _postprocess_common(sim_html, include_banner=is_demo)
    sim_html = _postprocess_simulator(sim_html)
    (target / "simulator.html").write_text(sim_html, encoding="utf-8")

    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="静的HTMLビルド")
    parser.add_argument("--output", type=Path, default=DOCS_DIR, help="出力先ディレクトリ（デフォルト: dist/）")
    parser.add_argument("--mode", choices=["demo", "live"], default="demo", help="demo か live を選択")
    parser.add_argument("--db-path", type=str, default="data/assets.db", help="live モードで読むDBパス")
    args = parser.parse_args()

    output = build(output_dir=args.output, mode=args.mode, db_path=args.db_path)
    files = sorted(output.iterdir())
    print(f"Built {len(files)} files in {output}/:")
    for f in files:
        size = f.stat().st_size
        print(f"  {f.name:20s} {size:>8,} bytes")
