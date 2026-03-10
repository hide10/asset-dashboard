"""ビルドスクリプトの出力を検証するテスト。"""

from __future__ import annotations

import re

import pytest

from scripts.build_static import build

EXPECTED_FILES = {"index.html", "cf.html", "plan.html", "simulator.html", ".nojekyll"}


@pytest.fixture(scope="module")
def output_dir(tmp_path_factory):
    """ビルドを1回だけ実行して出力ディレクトリを返す。"""
    tmp = tmp_path_factory.mktemp("dist")
    build(output_dir=tmp, mode="demo")
    return tmp


@pytest.fixture(scope="module")
def html_files(output_dir):
    """各 HTML ファイルの内容を dict で返す。"""
    result = {}
    for name in ["index.html", "cf.html", "plan.html", "simulator.html"]:
        path = output_dir / name
        if path.exists():
            result[name] = path.read_text(encoding="utf-8")
    return result


class TestBuildOutput:
    """ビルド結果のファイル構成テスト。"""

    def test_all_files_generated(self, output_dir):
        """4 HTML + .nojekyll の 5 ファイルが生成される。"""
        names = {f.name for f in output_dir.iterdir()}
        assert names == EXPECTED_FILES

    def test_html_files_not_empty(self, html_files):
        """全 HTML ファイルが空でない。"""
        for name, content in html_files.items():
            assert len(content) > 1000, f"{name} is too small"


class TestSimulatorPage:
    """シミュレーターページの検証。"""

    def test_no_api_fetch(self, html_files):
        """fetch('/api/simulator') が含まれない。"""
        sim = html_files["simulator.html"]
        assert "fetch('/api/simulator'" not in sim

    def test_has_js_montecarlo(self, html_files):
        """runLifecycleSimulationJS 関数が含まれる。"""
        sim = html_files["simulator.html"]
        assert "runLifecycleSimulationJS" in sim

    def test_recalc_uses_js(self, html_files):
        """recalcSimulator() が JS Monte Carlo を呼ぶ。"""
        sim = html_files["simulator.html"]
        # recalcSimulator 内で runLifecycleSimulationJS が呼ばれる
        recalc_match = re.search(r"function recalcSimulator\(\).*?^}", sim, re.DOTALL | re.MULTILINE)
        assert recalc_match, "recalcSimulator() not found"
        assert "runLifecycleSimulationJS" in recalc_match.group()

    def test_reset_no_fetch(self, html_files):
        """resetFromData() が fetch を使わない。"""
        sim = html_files["simulator.html"]
        reset_match = re.search(r"function resetFromData\(\).*?^}", sim, re.DOTALL | re.MULTILINE)
        assert reset_match, "resetFromData() not found"
        assert "fetch" not in reset_match.group()

    def test_reset_button_text(self, html_files):
        """リセットボタンのテキストが「デフォルトに戻す」。"""
        sim = html_files["simulator.html"]
        assert "デフォルトに戻す" in sim
        assert "実データから再取得" not in sim


class TestNavLinks:
    """ナビリンクの相対パス変換テスト。"""

    def test_relative_links(self, html_files):
        """全ページのナビリンクが相対パスである。"""
        for name, content in html_files.items():
            # href="/" は残っていてはいけない（ナビ内）
            nav_section = re.search(r'<div class="nav-toolbar">.*?</div>', content)
            assert nav_section, f"{name}: nav-toolbar not found"
            nav = nav_section.group()
            assert 'href="index.html"' in nav, f"{name}: missing index.html link"
            assert 'href="cf.html"' in nav, f"{name}: missing cf.html link"
            assert 'href="plan.html"' in nav, f"{name}: missing plan.html link"
            assert 'href="simulator.html"' in nav, f"{name}: missing simulator.html link"

    def test_no_settings_link(self, html_files):
        """設定ページのリンクが除去されている。"""
        for name, content in html_files.items():
            assert 'href="/settings"' not in content, f"{name}: settings link found"


class TestNoPolling:
    """ポーリング JS が含まれないことを確認。"""

    def test_no_setinterval(self, html_files):
        """全ページに setInterval が含まれない。"""
        for name, content in html_files.items():
            assert "setInterval" not in content, f"{name}: setInterval found"


class TestDemoBanner:
    """デモバナーの存在確認。"""

    def test_banner_present(self, html_files):
        """全ページにデモバナーが含まれる。"""
        for name, content in html_files.items():
            assert "DEMO" in content, f"{name}: DEMO banner missing"
            assert "GitHub" in content, f"{name}: GitHub link missing in banner"


class TestPlanPage:
    """プランページの検証。"""

    def test_updatecontrib_disabled(self, html_files):
        """updateContrib() の location.href 遷移が無効化されている。"""
        plan = html_files["plan.html"]
        assert "// location.href = url.toString();" in plan
