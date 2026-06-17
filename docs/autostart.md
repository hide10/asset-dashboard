# 自動起動の設定 (#59)

OS 起動 / ログイン時にダッシュボード (`python -m src.web.server`, `http://localhost:8080`) を
自動起動する手順。環境ごとに使う仕組みが違う。

## Linux（systemd）

```bash
bash install.sh --autostart
```

`scripts/mf-tracker.service` をテンプレートに、絶対パスを埋めた systemd ユーザーサービスを
`~/.config/systemd/user/mf-tracker.service` に生成し、`systemctl --user enable --now` まで実行する。

- 状態: `systemctl --user status mf-tracker`
- 解除: `systemctl --user disable --now mf-tracker`
- ログインシェルを開かずに常駐させたい場合: `loginctl enable-linger $USER`

## WSL2（systemd=true）+ Windows ログオン

WSL 内とWindows側の2層で設定する。

1. **WSL 内のサービス**（上の Linux 手順と同じ）:
   ```bash
   bash install.sh --autostart
   loginctl enable-linger "$USER"   # WSL ではシェル無し常駐に必要
   ```
   `/etc/wsl.conf` に `[boot] systemd=true` が必要。

2. **Windows ログオン → WSL 起動**:
   ```bash
   bash scripts/register-wsl-startup.sh
   ```
   Windows のスタートアップフォルダに VBS ランチャ（管理者権限不要）を置き、ログオン時に
   コンソール非表示で `wsl.exe -d <distro> -e true` を実行する。WSL が起動すれば systemd と
   linger 済みサービスが立ち上がる。解除は `bash scripts/register-wsl-startup.sh --unregister`。

連鎖: Windows ログオン → VBS(非表示) → WSL 起動 → systemd → mf-tracker → `http://localhost:8080`

> ネイティブ Windows ではなく WSL でサーバーを動かす場合はこちらを使う。
> `register-task.ps1`（下記）は WSL を経由しないネイティブ Windows 向け。

## Windows（ネイティブ Python / タスクスケジューラ）

WSL を使わず Windows 上で直接サーバーを動かす場合:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -AutoStart
# または個別に:
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
```

`pythonw.exe` でコンソール非表示のログオン時タスク `MFTrackerDashboard` を登録する。
解除は `powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Unregister`。
