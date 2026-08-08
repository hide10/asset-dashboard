#!/usr/bin/env bash
set -euo pipefail

# MoneyForward ME 資産トラッカー — ワンコマンドインストール
# Usage: bash install.sh [--autostart]
#   --autostart : ログイン時にダッシュボードを自動起動する systemd ユーザーサービスを登録

PYTHON_MIN="3.11"
VENV_DIR=".venv"
AUTOSTART=0

# --- 引数パース -------------------------------------------------------------

for arg in "$@"; do
    case "$arg" in
        --autostart) AUTOSTART=1 ;;
        -h|--help)
            echo "Usage: bash install.sh [--autostart]"
            echo "  --autostart : ログイン時自動起動の systemd ユーザーサービスを登録"
            exit 0
            ;;
        *) printf '不明な引数: %s\n' "$arg" >&2; exit 1 ;;
    esac
done

# --- ヘルパー関数 -----------------------------------------------------------

info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
warn()  { printf '\033[1;33m[WARN]\033[0m  %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*"; exit 1; }

# --- 自動起動（systemd ユーザーサービス）------------------------------------

setup_autostart() {
    local template="scripts/mf-tracker.service"
    [ -f "$template" ] || { warn "テンプレートが見つかりません: $template — 自動起動の登録をスキップ"; return; }

    if ! command -v systemctl &>/dev/null; then
        warn "systemctl が見つかりません。自動起動の登録をスキップします（systemd 環境でのみ対応）。"
        return
    fi

    local workdir python_bin unit_dir
    workdir="$(pwd)"
    python_bin="$workdir/$VENV_DIR/bin/python"
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

    info "systemd ユーザーサービスを登録中..."
    mkdir -p "$unit_dir"
    sed -e "s|__WORKDIR__|$workdir|g" -e "s|__PYTHON__|$python_bin|g" \
        "$template" > "$unit_dir/mf-tracker.service"

    systemctl --user daemon-reload
    if systemctl --user enable --now mf-tracker.service 2>/dev/null; then
        ok "自動起動を有効化しました (systemctl --user enable mf-tracker)"
    else
        warn "サービスの有効化に失敗しました。ユニットは生成済みです:
  $unit_dir/mf-tracker.service
  手動で有効化: systemctl --user enable --now mf-tracker.service
  （リモート/SSH では 'loginctl enable-linger \$USER' が必要な場合があります）"
    fi
}

# バージョン文字列を比較 (a >= b なら 0)
# macOS の BSD sort は -V 未対応のため数値比較で実装
ver_gte() {
    local IFS=.
    local -a a=($1) b=($2)
    (( ${a[0]:-0} > ${b[0]:-0} )) && return 0
    (( ${a[0]:-0} < ${b[0]:-0} )) && return 1
    (( ${a[1]:-0} >= ${b[1]:-0} ))
}

# --- Python チェック ---------------------------------------------------------

find_python() {
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || continue
            if ver_gte "$ver" "$PYTHON_MIN"; then
                PYTHON_CMD="$(command -v "$cmd")"
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

info "依存パッケージをインストール中 (本体 + 開発ツール)..."
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

# --- 自動起動の登録 ---------------------------------------------------------

if [ "$AUTOSTART" = "1" ]; then
    setup_autostart
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
if [ "$AUTOSTART" != "1" ]; then
    echo "  ログイン時に自動起動させる場合:"
    echo "     bash install.sh --autostart"
    echo ""
fi
