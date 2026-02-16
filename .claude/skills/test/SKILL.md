---
name: test
description: pytest 実行 + 失敗分析
user_invocable: true
---

# /test スキル

pytest を実行し、結果を分析して報告する。

## 手順

1. テスト実行:
   ```bash
   .venv/bin/python3 -m pytest tests/ -v --tb=short
   ```

2. 結果を分析:
   - **全テストパス**: 簡潔に報告
   - **失敗あり**: 失敗したテストごとに原因を分析し、修正案を提示

3. 引数が渡された場合はそのパスのみテスト:
   ```bash
   .venv/bin/python3 -m pytest <引数> -v --tb=short
   ```
