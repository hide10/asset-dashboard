# MoneyForward ME 資産トラッカー — ログオン時自動起動タスクの登録 (Windows)
#
# Usage:
#   登録:   powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
#   解除:   powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Unregister
#
# ログオン時にダッシュボードをバックグラウンド（コンソールウィンドウ非表示）で起動する。
# 非表示実行のため pythonw.exe を使う。

param(
    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$TaskName = "MFTrackerDashboard"

function Write-Info { param([string]$Message) Write-Host "[INFO]  $Message" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Message) Write-Host "[OK]    $Message" -ForegroundColor Green }
function Write-Warn { param([string]$Message) Write-Host "[WARN]  $Message" -ForegroundColor Yellow }
function Write-Err  { param([string]$Message) Write-Host "[ERROR] $Message" -ForegroundColor Red; exit 1 }

# --- 解除 -------------------------------------------------------------------

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Ok "自動起動タスクを解除しました: $TaskName"
    } else {
        Write-Info "自動起動タスクは登録されていません: $TaskName"
    }
    return
}

# --- パス解決 ---------------------------------------------------------------
# このスクリプトは scripts\ にあるので、リポジトリルートは1つ上。

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    Write-Err @"
仮想環境の pythonw.exe が見つかりません: $pythonw
  先に install.ps1 を実行して仮想環境を作成してください。
"@
}

# --- タスク登録 -------------------------------------------------------------

Write-Info "ログオン時自動起動タスクを登録中: $TaskName"

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m src.web.server" -WorkingDirectory $repoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description "MoneyForward ME 資産トラッカー ダッシュボードをログオン時に起動" `
    -Force | Out-Null

Write-Ok "登録完了: $TaskName"
Write-Host ""
Write-Host "  次回ログオンから自動起動します。すぐ試す場合は手動実行:"
Write-Host "    Start-ScheduledTask -TaskName $TaskName"
Write-Host "  解除する場合:"
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Unregister"
Write-Host ""
