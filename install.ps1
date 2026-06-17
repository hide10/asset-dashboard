# MoneyForward ME 資産トラッカー — ワンコマンドインストール (Windows PowerShell)
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1 [-AutoStart]
#   -AutoStart : ログオン時にダッシュボードを自動起動するタスクを登録

param(
    [switch]$AutoStart
)

$ErrorActionPreference = "Stop"

$PYTHON_MIN = [version]"3.11"
$VENV_DIR = ".venv"

# --- ヘルパー関数 -----------------------------------------------------------

function Write-Info  { param([string]$Message) Write-Host "[INFO]  $Message" -ForegroundColor Cyan }
function Write-Ok    { param([string]$Message) Write-Host "[OK]    $Message" -ForegroundColor Green }
function Write-Warn  { param([string]$Message) Write-Host "[WARN]  $Message" -ForegroundColor Yellow }
function Write-Err   { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red; exit 1 }

# --- Python チェック ---------------------------------------------------------

Write-Info "Python $PYTHON_MIN 以上を検索中..."

$pythonCmd = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver -and [version]$ver -ge $PYTHON_MIN) {
            $pythonCmd = $cmd
            $pythonVer = $ver
            break
        }
    } catch { }
}

if (-not $pythonCmd) {
    Write-Err @"
Python $PYTHON_MIN 以上が見つかりません。
  インストール方法:
    winget : winget install Python.Python.3.12
    公式   : https://www.python.org/downloads/
"@
}
Write-Ok "Python $pythonVer ($pythonCmd)"

# --- uv チェック・インストール -----------------------------------------------

$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvPath) {
    $localUv = Join-Path $env:LOCALAPPDATA "uv\uv.exe"
    if (Test-Path $localUv) {
        $env:PATH = "$(Split-Path $localUv);$env:PATH"
        $uvPath = Get-Command uv -ErrorAction SilentlyContinue
    }
}

if ($uvPath) {
    $uvVer = (uv --version 2>$null) | Select-Object -First 1
    Write-Ok "uv $uvVer"
} else {
    Write-Info "uv をインストール中..."
    irm https://astral.sh/uv/install.ps1 | iex
    # インストーラーが PATH を設定するまで再検索
    $localUv = Join-Path $env:LOCALAPPDATA "uv\uv.exe"
    if (Test-Path $localUv) {
        $env:PATH = "$(Split-Path $localUv);$env:PATH"
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Err "uv のインストールに失敗しました。手動でインストールしてください: https://docs.astral.sh/uv/"
    }
    Write-Ok "uv インストール完了"
}

# --- 仮想環境 ---------------------------------------------------------------

if (Test-Path $VENV_DIR) {
    Write-Info "既存の仮想環境を検出 ($VENV_DIR)"
} else {
    Write-Info "仮想環境を作成中..."
    uv venv $VENV_DIR --python $pythonVer
}
Write-Ok "仮想環境: $VENV_DIR"

$venvPython = Join-Path $VENV_DIR "Scripts\python.exe"

# --- 依存インストール -------------------------------------------------------

Write-Info "依存パッケージをインストール中 (本体 + 開発ツール)..."
uv pip install -e ".[dev]" --python $venvPython
Write-Ok "依存インストール完了"

# --- Playwright Chromium ----------------------------------------------------

Write-Info "Playwright Chromium をインストール中..."
& $venvPython -m playwright install chromium
Write-Ok "Playwright Chromium インストール完了"

# --- 自動起動の登録 ---------------------------------------------------------

if ($AutoStart) {
    Write-Info "ログオン時自動起動タスクを登録中..."
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "scripts\register-task.ps1")
}

# --- 完了 -------------------------------------------------------------------

Write-Host ""
Write-Host "==========================================" -ForegroundColor Green
Write-Ok "インストール完了!"
Write-Host "==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "次のステップ:"
Write-Host ""
Write-Host "  1. 仮想環境を有効化:"
Write-Host "     $VENV_DIR\Scripts\Activate.ps1"
Write-Host ""
Write-Host "  2. 初回ログイン (ブラウザが開きます):"
Write-Host "     python -m src.scraper.login"
Write-Host ""
Write-Host "  3. ダッシュボード起動:"
Write-Host "     python -m src.web.server"
Write-Host ""
Write-Host "  デモモードで試す場合:"
Write-Host "     python -m src.web.server --demo"
Write-Host ""
if (-not $AutoStart) {
    Write-Host "  ログオン時に自動起動させる場合:"
    Write-Host "     powershell -ExecutionPolicy Bypass -File install.ps1 -AutoStart"
    Write-Host ""
}
