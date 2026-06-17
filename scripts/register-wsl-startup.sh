#!/usr/bin/env bash
set -euo pipefail

# MoneyForward ME 資産トラッカー — WSL 運用時の Windows ログオン自動起動
#
# 用途:
#   WSL2 + systemd でダッシュボードを動かしている場合に、Windows ログオン時へ
#   WSL ディストロを自動起動させる。WSL 内のサービス自体は
#   `bash install.sh --autostart`（systemd ユーザーサービス + linger）で登録しておくこと。
#   このスクリプトは「Windows ログオン → WSL 起動」の一手だけを担う。
#
# 仕組み:
#   Windows のスタートアップフォルダに VBS ランチャを置く（管理者権限不要）。
#   ログオン時にコンソール非表示で `wsl.exe -d <distro> -e true` を実行し、
#   systemd(=PID1) と linger 済みユーザーサービスが立ち上がる。
#
# Usage:
#   bash scripts/register-wsl-startup.sh              # 登録
#   bash scripts/register-wsl-startup.sh --unregister # 解除

UNREGISTER=0
[ "${1:-}" = "--unregister" ] && UNREGISTER=1

info()  { printf '\033[1;34m[INFO]\033[0m  %s\n' "$*"; }
ok()    { printf '\033[1;32m[OK]\033[0m    %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*"; exit 1; }

# WSL 上でのみ意味がある
grep -qiE "(microsoft|wsl)" /proc/version 2>/dev/null || error "WSL 環境ではありません。このスクリプトは WSL 専用です。"

DISTRO="${WSL_DISTRO_NAME:-Ubuntu}"

# Windows のスタートアップフォルダを解決
appdata_win="$(cmd.exe /c 'echo %APPDATA%' 2>/dev/null | tr -d '\r')"
[ -n "$appdata_win" ] || error "%APPDATA% を取得できませんでした。"
startup="$(wslpath "$appdata_win")/Microsoft/Windows/Start Menu/Programs/Startup"
[ -d "$startup" ] || error "スタートアップフォルダが見つかりません: $startup"

vbs="$startup/start-mf-tracker-wsl.vbs"

if [ "$UNREGISTER" = "1" ]; then
    if [ -f "$vbs" ]; then
        rm -f "$vbs"
        ok "Windows ログオン自動起動を解除しました: $vbs"
    else
        info "登録されていません: $vbs"
    fi
    exit 0
fi

# VBS を CRLF で書き出す（Windows スクリプト）。ウィンドウ非表示(0)・待たない(False)。
printf 'Set sh = CreateObject("WScript.Shell")\r\nsh.Run "wsl.exe -d %s -e true", 0, False\r\n' "$DISTRO" > "$vbs"
ok "Windows ログオン時に WSL($DISTRO) を起動するランチャを登録しました:"
echo "    $vbs"
echo ""
echo "  次回 Windows ログオンから有効。解除する場合:"
echo "    bash scripts/register-wsl-startup.sh --unregister"
