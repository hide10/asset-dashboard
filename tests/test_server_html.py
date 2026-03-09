"""server.py のHTML生成テスト — デモデータで構造を検証。

ブラウザでの目視確認が必要な項目はスキップし、
HTML構造・データの存在・ナビゲーション・折りたたみを自動検証する。
"""

import re

import pytest

from src.db.schema import init_db
from src.web.server import (
    Handler,
    _build_ai_prompt_simulator,
    _build_cf_html,
    _build_html,
    _build_plan_html,
    _build_settings_html,
    _build_simulator_html,
    _demo_cf_data,
    _demo_data,
    _demo_plan_data,
    _demo_simulator_data,
    _nav_html,
)


def _build_settings_html_for_test(tmp_path) -> str:
    db_path = tmp_path / "settings_test.db"
    conn = init_db(str(db_path))
    conn.close()
    return _build_settings_html(str(db_path), saved=False)


# --- ナビゲーションツールバー (#13) ---


class TestNavToolbar:
    """全ページでツールバー形式のナビゲーションが正しく表示される。"""

    def _extract_nav(self, html: str) -> str:
        m = re.search(r'<div class="nav-toolbar">.*?</div>', html)
        assert m, "nav-toolbar not found in HTML"
        return m.group(0)

    def test_nav_html_active(self):
        html = _nav_html("/cf")
        assert 'class="active">家計簿分析' in html
        assert 'class="active">ダッシュボード' not in html

    def test_nav_html_all_pages(self):
        html = _nav_html("/")
        for label in ["ダッシュボード", "家計簿分析", "ライフプラン", "シミュレーター", "設定"]:
            assert label in html

    def test_nav_html_simulator_active(self):
        html = _nav_html("/simulator")
        assert 'class="active">シミュレーター' in html

    def test_simulator_has_toolbar(self):
        html = _build_simulator_html(_demo_simulator_data())
        nav = self._extract_nav(html)
        assert 'class="active">シミュレーター' in nav

    def test_no_arrows_in_nav(self):
        """ナビゲーション内に矢印文字がない。"""
        for active in ["/", "/cf", "/plan", "/simulator", "/settings"]:
            nav = _nav_html(active)
            assert "&larr;" not in nav
            assert "&rarr;" not in nav
            assert "\u2190" not in nav  # ←
            assert "\u2192" not in nav  # →

    def test_dashboard_has_toolbar(self):
        html = _build_html(_demo_data(), [_demo_data()["date"]])
        nav = self._extract_nav(html)
        assert 'class="active">ダッシュボード' in nav

    def test_cf_has_toolbar(self):
        html = _build_cf_html(_demo_cf_data())
        nav = self._extract_nav(html)
        assert 'class="active">家計簿分析' in nav

    def test_plan_has_toolbar(self):
        html = _build_plan_html(_demo_plan_data())
        nav = self._extract_nav(html)
        assert 'class="active">ライフプラン' in nav

    def test_settings_has_toolbar(self, tmp_path):
        html = _build_settings_html_for_test(tmp_path)
        nav = self._extract_nav(html)
        assert 'class="active">設定' in nav

    def test_no_old_nav_link_class(self):
        """旧 .nav-link クラスが残っていない。"""
        for gen in [
            lambda: _build_html(_demo_data(), [_demo_data()["date"]]),
            lambda: _build_cf_html(_demo_cf_data()),
            lambda: _build_plan_html(_demo_plan_data()),
            lambda: _build_simulator_html(_demo_simulator_data()),
        ]:
            html = gen()
            assert "nav-link" not in html


# --- 折りたたみ (#14) ---


class TestCollapse:
    """折りたたみがカード縮小方式で動作する。"""

    def test_collapse_css_targets_card(self):
        """CSSが card-body ではなく card 本体に collapsed を付与する形式。"""
        html = _build_cf_html(_demo_cf_data())
        assert "[data-card-id].collapsed > .card-body" in html
        assert "[data-card-id].collapsed { padding-bottom: 8px" in html

    def test_collapse_js_toggles_card(self):
        """JSが card.classList.toggle('collapsed') を使用。"""
        html = _build_cf_html(_demo_cf_data())
        assert "card.classList.toggle('collapsed')" in html

    def test_all_cf_cards_have_collapse(self):
        """全CFカードに data-card-id と collapse-btn がある。"""
        html = _build_cf_html(_demo_cf_data())
        card_ids = re.findall(r'data-card-id="([^"]+)"', html)
        expected = {"cf-category", "cf-top", "cf-cat-trend", "cf-fixed", "cf-income", "cf-trend", "cf-download"}
        assert expected.issubset(set(card_ids)), f"Missing: {expected - set(card_ids)}"
        # 各カードに collapse-btn がある
        for cid in expected:
            pattern = f'data-card-id="{cid}"[^>]*>.*?collapse-btn'
            assert re.search(pattern, html, re.DOTALL), f"collapse-btn missing for {cid}"

    def test_all_dashboard_cards_have_collapse(self):
        """全ダッシュボードカードに data-card-id と collapse-btn がある。"""
        html = _build_html(_demo_data(), [_demo_data()["date"]])
        card_ids = re.findall(r'data-card-id="([^"]+)"', html)
        expected = {"dash-class", "dash-accounts", "dash-sector", "dash-dividend", "dash-holdings"}
        assert expected.issubset(set(card_ids)), f"Missing: {expected - set(card_ids)}"
        for cid in expected:
            pattern = f'data-card-id="{cid}"[^>]*>.*?collapse-btn'
            assert re.search(pattern, html, re.DOTALL), f"collapse-btn missing for {cid}"

    def test_all_plan_cards_have_collapse(self):
        """全ライフプランカードに data-card-id と collapse-btn がある。"""
        html = _build_plan_html(_demo_plan_data())
        card_ids = re.findall(r'data-card-id="([^"]+)"', html)
        expected = {"plan-daily-assets", "plan-cashflow", "plan-pred", "plan-pred-c"}
        assert expected.issubset(set(card_ids)), f"Missing: {expected - set(card_ids)}"
        for cid in expected:
            pattern = f'data-card-id="{cid}"[^>]*>.*?collapse-btn'
            assert re.search(pattern, html, re.DOTALL), f"collapse-btn missing for {cid}"


# --- 家計簿分析カード ---


class TestCfCards:
    """家計簿分析ページの各カードが正しいデータを含む。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.data = _demo_cf_data()
        self.html = _build_cf_html(self.data)

    def test_summary_cards(self):
        assert "支出合計" in self.html
        assert "収入合計" in self.html
        assert "収支" in self.html

    def test_category_pie_chart(self):
        assert 'id="cf-pie"' in self.html
        assert "drawPieChart('cf-pie'" in self.html

    def test_category_trend_chart(self):
        assert 'id="cat-trend-chart"' in self.html
        assert "カテゴリ別月次推移" in self.html

    def test_category_trend_diff_table(self):
        """差分テーブルに前月比が含まれる。"""
        assert "差分" in self.html
        assert "直近6ヶ月平均" in self.html
        assert "先月比(現時点)" in self.html
        assert "先月比(着地予測)" in self.html
        assert "期間進捗" in self.html

    def test_fixed_vs_variable(self):
        assert "固定費 vs 変動費" in self.html
        assert "固定費率" in self.html or "固定費" in self.html

    def test_fixed_expense_items(self):
        """固定費テーブルに項目がある。"""
        assert "家賃・地代" in self.html
        assert "生命保険" in self.html

    def test_income_breakdown(self):
        assert "収入の内訳" in self.html
        assert 'id="income-pie"' in self.html
        assert "drawPieChart('income-pie'" in self.html

    def test_income_stability(self):
        """収入安定度インジケータが表示される。"""
        assert "収入安定度" in self.html

    def test_monthly_trend_chart(self):
        assert 'id="trend-chart"' in self.html

    def test_download_management(self):
        assert "過去月ダウンロード管理" in self.html

    def test_top_expenses_filtered_label(self):
        assert "高額支出 TOP15（生活支出）" in self.html
        assert "積立・管理費等を除外して表示しています" in self.html

    def test_top_expenses_filters_investment_like_item(self):
        d = _demo_cf_data()
        d["summary"]["top_expenses"].insert(
            0,
            {
                "date": d["year_month"] + "-04",
                "description": "サワカミ積立",
                "amount": 120000,
                "major_category": "保険",
                "minor_category": "投資信託",
                "institution": "サワカミ",
            },
        )
        html = _build_cf_html(d)
        assert "サワカミ積立" not in html

    def test_top_expenses_filters_meijiyasuda_variant(self):
        d = _demo_cf_data()
        d["summary"]["top_expenses"].insert(
            0,
            {
                "date": d["year_month"] + "-06",
                "description": "メイジヤスダセイメイ 年金積立",
                "amount": 98000,
                "major_category": "保険",
                "minor_category": "個人年金保険",
                "institution": "メイジヤスダセイメイ",
            },
        )
        html = _build_cf_html(d)
        assert "メイジヤスダセイメイ 年金積立" not in html


# --- 過去月取得フォーム (#15) ---


class TestManualMonthFetch:
    """任意の過去月を指定してCSV取得するUIが存在する。"""

    def test_year_month_select_exists(self):
        html = _build_cf_html(_demo_cf_data())
        assert 'id="manual-year"' in html
        assert 'id="manual-month-sel"' in html
        # 現在年が選択肢に含まれる
        from datetime import datetime

        assert f'value="{datetime.now().year}"' in html

    def test_fetch_button_exists(self):
        html = _build_cf_html(_demo_cf_data())
        assert "fetchManualMonth" in html

    def test_future_month_validation(self):
        """JSで未来月のバリデーションがある。"""
        html = _build_cf_html(_demo_cf_data())
        assert "未来の月は取得できません" in html


# --- ライフプラン貯蓄実績バナー ---


class TestPlanSavingsBanner:
    def test_banner_present(self):
        data = _demo_plan_data()
        html = _build_plan_html(data)
        assert "家計簿実績" in html
        assert "14.6%" in html

    def test_banner_absent_without_data(self):
        data = _demo_plan_data()
        data["cf_savings"] = None
        html = _build_plan_html(data)
        assert "家計簿実績" not in html


# --- 日次資産推移 (#18) ---


class TestDailyAssets:
    """日次資産推移カードが正しく表示される。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.data = _demo_plan_data()
        self.html = _build_plan_html(self.data)

    def test_daily_chart_card_exists(self):
        """日次資産推移カードが存在する。"""
        assert 'data-card-id="plan-daily-assets"' in self.html
        assert "資産推移（実績）" in self.html

    def test_daily_chart_canvas(self):
        """Canvas 要素が存在する。"""
        assert 'id="daily-chart"' in self.html

    def test_period_buttons(self):
        """期間切替ボタンが存在する。"""
        assert "period-btn" in self.html
        for label in ["1M", "3M", "6M", "1Y", "ALL"]:
            assert f">{label}</button>" in self.html


# --- 予算設定と予算対比表示 (#2) ---


class TestBudget:
    """予算列・消化率バー・予算残りサマリーが正しく表示される。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.data = _demo_cf_data()
        self.html = _build_cf_html(self.data)

    def test_budget_column_exists(self):
        """テーブルに「予算」ヘッダがある。"""
        assert "<th" in self.html
        assert "予算" in self.html

    def test_budget_progress_bar(self):
        """消化率バーが存在する。"""
        assert "budget-bar" in self.html
        assert "budget-bar-bg" in self.html

    def test_budget_remaining_summary(self):
        """予算残りサマリーカードが存在する。"""
        assert "予算残り" in self.html
        assert 'data-testid="budget-remaining"' in self.html

    def test_budget_total_summary(self):
        """予算合計サマリーカードが存在する。"""
        assert "予算合計" in self.html
        assert 'data-testid="budget-total"' in self.html

    def test_budget_total_income_ratio(self):
        """予算合計カードに収入比が表示される。"""
        assert "収入の" in self.html
        assert "%" in self.html

    def test_budget_remaining_prev_month(self):
        """予算残りカードに前月実績が表示される。"""
        assert "先月実績:" in self.html

    def test_budget_cell_click_editable(self):
        """予算セルにクリック編集用のdata属性とクラスがある。"""
        assert "budget-cell" in self.html
        assert 'data-category="食費"' in self.html

    def test_budget_save_api_in_js(self):
        """JavaScriptに予算保存APIの呼び出しがある。"""
        assert "/api/cf/budget" in self.html


# --- デモデータ構造 ---


class TestDemoData:
    """デモデータが必要なキーを含む。"""

    def test_cf_demo_keys(self):
        d = _demo_cf_data()
        required = {
            "year_month",
            "summary",
            "trend",
            "available_months",
            "category_trend",
            "fixed_expenses",
            "income_breakdown",
            "income_trend",
        }
        assert required.issubset(set(d.keys()))

    def test_cf_category_trend_structure(self):
        d = _demo_cf_data()
        ct = d["category_trend"]
        assert len(ct["year_months"]) == 6
        assert len(ct["categories"]) > 0
        assert len(ct["by_month"]) == 6
        assert ct["avg_months"] == 6
        assert "住宅" in ct["avg_by_category"]

    def test_cf_fixed_expenses_structure(self):
        d = _demo_cf_data()
        fe = d["fixed_expenses"]
        assert len(fe["fixed"]) > 0
        assert fe["fixed_total"] > 0
        assert 0 < fe["fixed_ratio"] < 100

    def test_cf_income_breakdown_structure(self):
        d = _demo_cf_data()
        ib = d["income_breakdown"]
        assert len(ib["items"]) > 0
        assert ib["total"] > 0

    def test_cf_demo_has_budgets(self):
        d = _demo_cf_data()
        assert "budgets" in d
        assert isinstance(d["budgets"], dict)
        assert len(d["budgets"]) > 0
        assert "食費" in d["budgets"]

    def test_plan_demo_has_savings(self):
        d = _demo_plan_data()
        assert "cf_savings" in d
        assert d["cf_savings"]["months_used"] == 6

    def test_plan_demo_has_daily_assets(self):
        d = _demo_plan_data()
        assert "daily_assets" in d
        assert len(d["daily_assets"]) == 180
        assert "date" in d["daily_assets"][0]
        assert "total" in d["daily_assets"][0]
        assert "by_class" in d["daily_assets"][0]

    def test_plan_demo_has_dividend_history(self):
        d = _demo_plan_data()
        assert "dividend_history" in d
        dh = d["dividend_history"]
        assert len(dh["monthly"]) > 0
        assert len(dh["annual"]) > 0
        assert "year_month" in dh["monthly"][0]
        assert "amount" in dh["monthly"][0]

    def test_plan_dividend_card(self):
        html = _build_plan_html(_demo_plan_data())
        assert "配当・分配金実績" in html
        assert 'data-card-id="plan-dividends"' in html
        assert "div-chart" in html


# --- Flex レイアウト（カード独立高さ） ---


class TestFlexLayout:
    """Grid ではなく Flexbox を使い、カードが独立した高さを持つ。"""

    def test_dashboard_uses_flex(self):
        html = _build_html(_demo_data(), [_demo_data()["date"]])
        assert "display: flex; flex-wrap: wrap;" in html or "display: flex;flex-wrap: wrap;" in html
        assert "align-items: flex-start" in html

    def test_cf_uses_flex(self):
        html = _build_cf_html(_demo_cf_data())
        assert "display: flex; flex-wrap: wrap;" in html or "display: flex;flex-wrap: wrap;" in html
        assert "align-items: flex-start" in html

    def test_plan_uses_flex(self):
        html = _build_plan_html(_demo_plan_data())
        assert "display: flex; flex-wrap: wrap;" in html or "display: flex;flex-wrap: wrap;" in html
        assert "align-items: flex-start" in html

    def test_no_display_grid(self):
        """display: grid が .grid クラスに使われていない。"""
        for gen in [
            lambda: _build_html(_demo_data(), [_demo_data()["date"]]),
            lambda: _build_cf_html(_demo_cf_data()),
            lambda: _build_plan_html(_demo_plan_data()),
        ]:
            html = gen()
            assert (
                "display: grid" not in html or "display: grid" not in html.split(".grid")[1].split("}")[0]
                if ".grid" in html
                else True
            )

    def test_card_has_explicit_width(self):
        """カードに calc(50% - 10px) の幅が設定されている。"""
        html = _build_cf_html(_demo_cf_data())
        assert "width: calc(50% - 10px)" in html

    def test_full_card_has_100_width(self):
        """full クラスのカードは width: 100%。"""
        html = _build_cf_html(_demo_cf_data())
        assert ".full" in html
        # full の width が 100% に設定されている
        full_match = re.search(r"\.full\s*\{[^}]*width:\s*100%", html)
        assert full_match, ".full { width: 100% } not found"


# --- ダウンロード管理の表示 ---


class TestDownloadStatus:
    """ダウンロード管理で fetched=None や row_count=None を正しく処理する。"""

    def test_no_none_in_download_section(self):
        """デモデータのダウンロード管理に 'None' が表示されない。"""
        html = _build_cf_html(_demo_cf_data())
        # ダウンロード管理セクションを抽出
        dl_match = re.search(r"過去月ダウンロード管理.*?</table>", html, re.DOTALL)
        assert dl_match, "ダウンロード管理セクションが見つからない"
        dl_section = dl_match.group(0)
        assert "None" not in dl_section

    def test_download_shows_fetched_date(self):
        """取得済みの月に日付が表示される。"""
        html = _build_cf_html(_demo_cf_data())
        assert "取得済" in html


class TestSessionExpiredBanner:
    """セッション切れバナーの表示テスト。"""

    def test_banner_shown_when_expired(self):
        data = _demo_data()
        html = _build_html(data, [data["date"]], session_expired="2026-02-17T12:00:00")
        assert "セッション切れ" in html
        assert "再ログイン" in html

    def test_banner_hidden_when_not_expired(self):
        data = _demo_data()
        html = _build_html(data, [data["date"]], session_expired=None)
        assert "セッション切れ" not in html

    def test_banner_hidden_when_empty_string(self):
        data = _demo_data()
        html = _build_html(data, [data["date"]], session_expired="")
        assert "セッション切れ" not in html


class TestAiChatExport:
    """AIチャット用データエクスポートが設定ページに表示される。"""

    def test_settings_has_ai_chat_section(self, tmp_path):
        html = _build_settings_html_for_test(tmp_path)
        assert "AIチャット用データ" in html

    def test_settings_has_copy_buttons(self, tmp_path):
        html = _build_settings_html_for_test(tmp_path)
        assert "copyAiPrompt" in html
        assert "一括コピー（総合分析）" in html
        assert "copyAiPrompt('all',this)" in html
        assert "資産分析" in html
        assert "家計簿分析" in html
        assert "ライフプラン" in html
        assert "シミュレーター" in html

    def test_simulator_prompt_has_sections(self):
        """シミュレーター用プロンプトに前提条件・結果・年齢別テーブルが含まれる。"""
        data = _demo_simulator_data()
        md = _build_ai_prompt_simulator(data)
        assert "# ライフサイクル・シミュレーション結果" in md
        assert "## 前提条件" in md
        assert "## シミュレーション結果（モンテカルロ法" in md
        assert "## 年齢別資産残高（パーセンタイル）" in md
        assert "資産枯渇確率" in md
        assert "元本割れ確率" in md

    def test_simulator_prompt_has_params(self):
        """シミュレーター用プロンプトに全パラメータが含まれる。"""
        data = _demo_simulator_data()
        md = _build_ai_prompt_simulator(data)
        assert "現在の年齢" in md
        assert "退職年齢" in md
        assert "リスク資産" in md
        assert "安全資産" in md
        assert "毎月の積立額" in md
        assert "期待リターン" in md
        assert "年金月額" in md

    def test_simulator_prompt_has_age_rows(self):
        """シミュレーター用プロンプトに年齢別残高の行がある。"""
        data = _demo_simulator_data()
        md = _build_ai_prompt_simulator(data)
        # 開始年齢（35歳）と終了年齢（95歳）が含まれる
        assert "35歳" in md
        assert "95歳" in md
        # 退職年齢（65歳）が含まれる
        assert "65歳" in md

    def test_all_prompt_has_single_integrated_instruction(self, tmp_path):
        db_path = tmp_path / "all_prompt_test.db"
        conn = init_db(str(db_path))
        conn.close()

        handler = Handler.__new__(Handler)
        handler.db_path = str(db_path)
        md = Handler._build_ai_prompt(handler, "all")

        assert "## 資産分析" in md
        assert "## 家計簿分析" in md
        assert "## ライフプラン" in md
        assert "## シミュレーター" in md
        assert "統合データです。総合的に分析し" in md
        assert "優先順位付きで改善アクションを提案してください" in md
        assert md.count("以下の観点で") == 0
        assert "# 資産データ（" not in md
        assert "# 家計簿データ（" not in md
        assert "# ライフプランデータ（" not in md
        assert "# ライフサイクル・シミュレーション結果" not in md


class TestClosingDaySetting:
    """締め日設定が設定ページに表示される。"""

    def test_settings_has_closing_day_section(self, tmp_path):
        html = _build_settings_html_for_test(tmp_path)
        assert "家計簿の締め日" in html
        assert "closing_day" in html

    def test_settings_has_day_options(self, tmp_path):
        html = _build_settings_html_for_test(tmp_path)
        assert "1日（暦月）" in html
        assert "25日" in html
        assert "31日" in html

    def test_cf_page_no_period_note_default(self):
        """デフォルト(closing_day=1)では期間注記なし。"""
        data = _demo_cf_data()
        html = _build_cf_html(data)
        assert "毎月" not in html or "毎月1日" not in html

    def test_settings_has_holiday_mode_options(self, tmp_path):
        """祝日調整のラジオボタンが表示される。"""
        html = _build_settings_html_for_test(tmp_path)
        assert "土日祝日の扱い" in html
        assert "変更しない" in html
        assert "設定日前の平日" in html
        assert "設定日後の平日" in html
        assert 'name="holiday_mode"' in html


# --- Favicon (#37) ---


class TestFavicon:
    """全ページに favicon が設定されている。"""

    def test_dashboard_has_favicon(self):
        html = _build_html(_demo_data(), [_demo_data()["date"]])
        assert 'rel="icon"' in html

    def test_cf_has_favicon(self):
        html = _build_cf_html(_demo_cf_data())
        assert 'rel="icon"' in html

    def test_plan_has_favicon(self):
        html = _build_plan_html(_demo_plan_data())
        assert 'rel="icon"' in html

    def test_simulator_has_favicon(self):
        html = _build_simulator_html(_demo_simulator_data())
        assert 'rel="icon"' in html

    def test_settings_has_favicon(self, tmp_path):
        html = _build_settings_html_for_test(tmp_path)
        assert 'rel="icon"' in html


# --- 比較ラベルの動的表示 (#38) ---


class TestComparisonHeaders:
    """保有銘柄テーブルのヘッダーが比較結果に応じて動的に変わる。"""

    def test_demo_has_three_comparison_headers(self):
        """デモデータ（十分なデータ量）では前日比・前月比・前年比の3列。"""
        html = _build_html(_demo_data(), [_demo_data()["date"]])
        assert "前日比" in html
        assert "前月比" in html
        assert "前年比" in html


# --- 予測期間6パターン (#19) ---


class TestPrediction6Periods:
    """予測期間が1/3/5/10/20/30年の6パターンに拡張されている。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.data = _demo_plan_data()
        self.html = _build_plan_html(self.data)

    def test_demo_predictions_count(self):
        """デモデータの predictions が6要素。"""
        assert len(self.data["predictions"]) == 6

    def test_demo_predictions_contrib_count(self):
        """デモデータの predictions_contrib が6要素。"""
        assert len(self.data["predictions_contrib"]) == 6

    def test_html_has_long_term_labels(self):
        """HTML に「10年後」「20年後」「30年後」が存在する。"""
        assert "10年後" in self.html
        assert "20年後" in self.html
        assert "30年後" in self.html

    def test_html_has_short_term_labels(self):
        """HTML に「1年後」「3年後」「5年後」も引き続き存在する。"""
        assert "1年後" in self.html
        assert "3年後" in self.html
        assert "5年後" in self.html


# --- シミュレーターページ (#43) ---


class TestSimulator:
    """シミュレーターページの HTML 構造テスト。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.data = _demo_simulator_data()
        self.html = _build_simulator_html(self.data)

    def test_all_cards_exist(self):
        """全カード（sim-params, sim-summary, sim-projection）が存在。"""
        for cid in ["sim-params", "life-events", "sim-summary", "sim-projection"]:
            assert f'data-card-id="{cid}"' in self.html, f"Card {cid} missing"

    def test_collapse_buttons_on_all_cards(self):
        """全カードに折りたたみボタンがある。"""
        for cid in ["sim-params", "life-events", "sim-summary", "sim-projection"]:
            pattern = f'data-card-id="{cid}"[^>]*>.*?collapse-btn'
            assert re.search(pattern, self.html, re.DOTALL), f"collapse-btn missing for {cid}"

    def test_parameter_sliders_exist(self):
        """年齢系スライダーが存在する。"""
        for fid in ["current_age", "retirement_age", "end_age", "pension_start_age"]:
            assert f'id="{fid}"' in self.html, f"Slider {fid} missing"

    def test_parameter_number_inputs_exist(self):
        """金額系の数値入力が存在する。"""
        for fid in ["initial_investment", "monthly_contribution", "monthly_withdrawal", "monthly_pension"]:
            assert f'id="{fid}"' in self.html, f"Number input {fid} missing"

    def test_return_sliders_exist(self):
        """リターン系スライダーが存在する。"""
        for fid in ["annual_return", "annual_volatility", "inflation_rate", "expense_ratio"]:
            assert f'id="{fid}"' in self.html, f"Slider {fid} missing"

    def test_all_simulator_fields_have_info_help(self):
        """シミュレーターの全入力項目に i ヘルプがある。"""
        labels = [
            "現在の年齢",
            "退職年齢",
            "シミュレーション終了年齢",
            "リスク資産額",
            "安全資産額",
            "月額積立",
            "月額取崩し（生活費）",
            "期待リターン（年率）",
            "ボラティリティ（年率）",
            "インフレ率",
            "信託報酬",
            "年金受給開始年齢",
            "月額年金",
            "年金以外の月額収入",
        ]
        for label in labels:
            assert f'{label} <span class="sim-info-btn"' in self.html

    def test_recalc_button_exists(self):
        """「再計算」ボタンが存在する。"""
        assert "再計算" in self.html
        assert "recalcSimulator" in self.html

    def test_api_endpoint_in_js(self):
        """/api/simulator が JS 内に含まれる。"""
        assert "/api/simulator" in self.html
        assert "/api/life-events" in self.html
        assert "/api/life-events/housing-template" in self.html
        assert "/api/life-events/update" in self.html
        assert "/api/children" in self.html
        assert "/api/children/update" in self.html
        assert "/api/children/update-plan" in self.html
        assert "/api/life-settings" in self.html

    def test_life_event_section_labels(self):
        """ライフイベント管理UIの主要ラベルが表示される。"""
        assert "ライフイベント管理" in self.html
        assert "イベント追加" in self.html
        assert "住宅テンプレート" in self.html
        assert "子ども登録（教育費自動反映）" in self.html
        assert "イベント影響（最終残高差）" in self.html
        assert "期間内イベント支出合計" in self.html

    def test_summary_values_displayed(self):
        """財務サマリーの値が表示される。"""
        assert "投入元本" in self.html
        assert "運用益" in self.html
        assert "税金合計" in self.html
        assert "最終残高" in self.html

    def test_probabilities_displayed(self):
        """枯渇確率と元本割れ確率が表示される。"""
        assert "枯渇確率" in self.html
        assert "元本割れ確率" in self.html

    def test_projection_table_has_percentiles(self):
        """年次パーセンタイル表に P10/P25/P50/P75/P90 がある。"""
        assert "P10" in self.html
        assert "P25" in self.html
        assert "P50" in self.html
        assert "P75" in self.html
        assert "P90" in self.html

    def test_projection_table_has_age_rows(self):
        """年齢行がテーブルに含まれる。"""
        assert "36歳" in self.html  # current_age + 1
        assert "65歳" in self.html  # retirement_age

    def test_has_favicon(self):
        """favicon が設定されている。"""
        assert 'rel="icon"' in self.html

    def test_uses_flex_layout(self):
        """Flexbox レイアウトを使用している。"""
        assert "display: flex; flex-wrap: wrap;" in self.html or "display: flex;flex-wrap: wrap;" in self.html

    def test_demo_data_structure(self):
        """デモデータが正しい構造を持つ。"""
        assert "params" in self.data
        assert "result" in self.data
        result = self.data["result"]
        assert len(result.yearly_balances) > 0
        assert result.total_principal > 0


class TestFundTotalCard:
    """投資信託 評価額・取得価額推移カードのテスト。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        data = _demo_data()
        self.html = _build_html(data, [data["date"]], skip_update=True, demo=True)

    def test_card_exists(self):
        """投資信託カードが存在する。"""
        assert 'data-card-id="dash-fund-total"' in self.html

    def test_card_title(self):
        """カードタイトルが表示されている。"""
        assert "投資信託 評価額・取得価額推移" in self.html

    def test_canvas_exists(self):
        """グラフ用 canvas が存在する。"""
        assert 'id="fund-total-chart"' in self.html

    def test_range_buttons(self):
        """期間切り替えボタンが存在する。"""
        assert "3ヶ月" in self.html
        assert "6ヶ月" in self.html
        assert "1年" in self.html

    def test_summary_values(self):
        """評価額と取得価額のサマリーが表示されている。"""
        assert "取得価額" in self.html
        assert "評価損益" in self.html

    def test_demo_data_has_fund_history(self):
        """デモデータに fund_total_history が含まれる。"""
        data = _demo_data()
        fth = data.get("fund_total_history", [])
        assert len(fth) == 365
        assert "total_value" in fth[0]
        assert "total_cost" in fth[0]

    def test_no_card_when_empty(self):
        """fund_total_history が空ならカードは表示されない。"""
        data = _demo_data()
        data["fund_total_history"] = []
        html = _build_html(data, [data["date"]], skip_update=True, demo=True)
        assert 'data-card-id="dash-fund-total"' not in html
