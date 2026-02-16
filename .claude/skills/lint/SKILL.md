---
name: lint
description: ruff check + format + 結果報告
user_invocable: true
---

# /lint スキル

ruff によるリントとフォーマットを実行する。

## 手順

1. リントチェック（自動修正あり）:
   ```bash
   .venv/bin/ruff check src/ tests/ --fix
   ```

2. フォーマット:
   ```bash
   .venv/bin/ruff format src/ tests/
   ```

3. 結果報告:
   - 修正されたファイルの一覧
   - 自動修正できなかったエラーがあれば内容と修正案を提示

4. 引数が渡された場合はそのパスのみ対象にする
