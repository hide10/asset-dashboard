---
name: demo
description: デモサーバー起動
user_invocable: true
---

# /demo スキル

デモモードで Web サーバーを起動する。

## 手順

1. サーバーをバックグラウンドで起動:
   ```bash
   .venv/bin/python3 -m src.web.server --demo
   ```

2. 起動後、アクセス URL を報告:
   - http://localhost:8000/ — 資産ダッシュボード
   - http://localhost:8000/cf — 家計簿分析
   - http://localhost:8000/plan — ライフプランニング
   - http://localhost:8000/settings — 設定
