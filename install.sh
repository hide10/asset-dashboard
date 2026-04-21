#!/usr/bin/env bash
set -euo pipefail

# MoneyForward ME 資産トラッカー — ワンコマンドインストール
# Usage: bash install.sh

PYTHON_MIN="3.11"
VENV_DIR=".venv"

# --- ヘルパー関数 -----------------------------------------------------------

info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*"; exit 1; }

# バージョン文字列を比較 (a >= b なら 0)
ver_gte() {
    printf '%s\n%s' "$1" "$2" | sort -V | head -n1 | grep -qx "$2"
}

# --- Python チェック ---------------------------------------------------------

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || continue
            if ver_gte "$ver" "$PYTHON_MIN"; then
                PYTHON_CMD="$cmd"
                PYTHON_VER="$ver"
                return 0
            fi
        fi
    done
    return 1
}

info "Python $PYTHON_MIN 以上を検索中..."
if ! find_python; then
    error "Python $PYTHON_MIN 以上が見つかりません。
  インストール方法:
    Ubuntu/Debian : sudo apt install python3
    macOS         : brew install python@3.12
    その他        : https://www.python.org/downloads/"
fi
ok "Python $PYTHON_VER ($PYTHON_CMD)"

# --- uv チェック・インストール -----------------------------------------------

if ! command -v uv &>/dev/null && [ -f "$HOME/.local/bin/uv" ]; then
    export PATH="$HOME/.local/bin:$PATH"
fi

if command -v uv &>/dev/null; then
    ok "uv $(uv --version 2>/dev/null | head -1)"
else
    info "uv をインストール中..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv &>/dev/null; then
        error "uv のインストールに失敗しました。手動でインストールしてください: https://docs.astral.sh/uv/"
    fi
    ok "uv インストール完了"
fi

# --- 仮想環境 ---------------------------------------------------------------

if [ -d "$VENV_DIR" ]; then
    info "既存の仮想環境を検出 ($VENV_DIR)"
else
    info "仮想環境を作成中..."
    uv venv "$VENV_DIR" --python "$PYTHON_CMD"
fi
ok "仮想環境: $VENV_DIR"

# --- 依存インストール -------------------------------------------------------

info "依存パッケージをインストール中..."
uv pip install -e . --python "$VENV_DIR/bin/python"

info "開発用パッケージをインストール中..."
uv pip install -e '.[dev]' --python "$VENV_DIR/bin/python"
ok "依存インストール完了"

# --- Playwright Chromium ----------------------------------------------------

info "Playwright Chromium をインストール中..."
"$VENV_DIR/bin/python" -m playwright install chromium
ok "Playwright Chromium インストール完了"

# Playwright のシステム依存チェック
info "Chromium のシステム依存ライブラリを確認中..."
if "$VENV_DIR/bin/python" -m playwright install-deps chromium 2>/dev/null; then
    ok "システム依存ライブラリ OK"
else
    warn "システム依存ライブラリのインストールに失敗しました。
  必要に応じて以下を手動実行してください:
    sudo $VENV_DIR/bin/python -m playwright install-deps chromium"
fi

# --- 完了 -------------------------------------------------------------------

echo ""
echo "=========================================="
ok "インストール完了!"
echo "=========================================="
echo ""
echo "次のステップ:"
echo ""
echo "  1. 仮想環境を有効化:"
echo "     source $VENV_DIR/bin/activate"
echo ""
echo "  2. 初回ログイン (ブラウザが開きます):"
echo "     python -m src.scraper.login"
echo ""
echo "  3. ダッシュボード起動:"
echo "     python -m src.web.server"
echo ""
echo "  デモモードで試す場合:"
echo "     python -m src.web.server --demo"
echo ""
