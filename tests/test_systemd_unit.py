"""scripts/mf-tracker.service の構文・構造を検証するテスト。

systemd 本体を必要とせず、ユニットファイルを INI として解析して
必須セクション・キー・プレースホルダの存在を確認する。
実際に reboot 後に起動するかは人間が確認する（Issue #59 のチェックリスト）。
"""

from configparser import RawConfigParser
from pathlib import Path

import pytest

UNIT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "mf-tracker.service"


def _parse_unit() -> RawConfigParser:
    # systemd ユニットは INI 形式。% を含む値での補間エラーを避けるため Raw を使う。
    parser = RawConfigParser()
    parser.optionxform = str  # キーの大文字小文字を保持（systemd は case-sensitive）
    parser.read_string(UNIT_PATH.read_text(encoding="utf-8"))
    return parser


def test_unit_file_exists() -> None:
    assert UNIT_PATH.is_file(), f"ユニットテンプレートがない: {UNIT_PATH}"


def test_required_sections() -> None:
    parser = _parse_unit()
    for section in ("Unit", "Service", "Install"):
        assert parser.has_section(section), f"[{section}] セクションがない"


def test_service_keys() -> None:
    parser = _parse_unit()
    assert parser.get("Service", "Type") == "simple"
    assert parser.has_option("Service", "WorkingDirectory")
    assert parser.has_option("Service", "ExecStart")
    # サーバーをモジュール起動していること
    assert "-m src.web.server" in parser.get("Service", "ExecStart")


def test_install_wantedby() -> None:
    parser = _parse_unit()
    # ユーザーサービスとして自動起動するには default.target に紐づける
    assert parser.get("Install", "WantedBy") == "default.target"


def test_placeholders_present() -> None:
    text = UNIT_PATH.read_text(encoding="utf-8")
    # install.sh が絶対パスへ置換するためのトークン
    assert "__WORKDIR__" in text
    assert "__PYTHON__" in text


def test_substituted_unit_has_no_placeholders() -> None:
    # install.sh の sed 置換を模して、置換後に未解決トークンが残らないことを確認
    text = UNIT_PATH.read_text(encoding="utf-8")
    substituted = text.replace("__WORKDIR__", "/home/user/asset-dashboard").replace(
        "__PYTHON__", "/home/user/asset-dashboard/.venv/bin/python"
    )
    assert "__WORKDIR__" not in substituted
    assert "__PYTHON__" not in substituted

    parser = RawConfigParser()
    parser.optionxform = str
    parser.read_string(substituted)
    exec_start = parser.get("Service", "ExecStart")
    assert exec_start.startswith("/home/user/asset-dashboard/.venv/bin/python")
    assert parser.get("Service", "WorkingDirectory") == "/home/user/asset-dashboard"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
