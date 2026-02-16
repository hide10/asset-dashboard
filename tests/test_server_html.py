"""server.py のHTML生成テスト — デモデータで構造を検証。

ブラウザでの目視確認が必要な項目はスキップし、
HTML構造・データの存在・ナビゲーション・折りたたみを自動検証する。
"""

import re

import pytest

from src.web.server import (
    _build_cf_html,
    _build_html,
    _build_plan_html,
    _build_settings_html,
    _demo_cf_data,
    _demo_data,
    _demo_plan_data,
    _nav_html,
)

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
        for label in ["ダッシュボード", "家計簿分析", "ライフプラン", "設定"]:
            assert label in html

    def test_no_arrows_in_nav(self):
        """ナビゲーション内に矢印文字がない。"""
        for active in ["/", "/cf", "/plan", "/settings"]:
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

    def test_settings_has_toolbar(self):
        html = _build_settings_html("dummy_db", saved=False)
        nav = self._extract_nav(html)
        assert 'class="active">設定' in nav

    def test_no_old_nav_link_class(self):
        """旧 .nav-link クラスが残っていない。"""
        for gen in [
            lambda: _build_html(_demo_data(), [_demo_data()["date"]]),
            lambda: _build_cf_html(_demo_cf_data()),
            lambda: _build_plan_html(_demo_plan_data()),
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
        expected = {"plan-daily-assets", "plan-totals", "plan-cashflow", "plan-pred", "plan-pred-c"}
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


# --- 過去月取得フォーム (#15) ---


class TestManualMonthFetch:
    """任意の過去月を指定してCSV取得するUIが存在する。"""

    def test_month_input_exists(self):
        html = _build_cf_html(_demo_cf_data())
        assert 'type="month"' in html
        assert 'id="manual-month"' in html

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
