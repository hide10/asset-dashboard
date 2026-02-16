---
name: fix-issue
description: Issue 番号を渡して一連のワークフロー実行
user_invocable: true
---

# /fix-issue スキル

GitHub Issue を受け取り、実装からテスト・Issue 更新まで一連のワークフローを実行する。

## 手順

1. **Issue 確認**: 引数の Issue 番号（例: `12`）の内容を取得:
   ```bash
   gh issue view <番号>
   ```

2. **実装**: Issue の内容に基づいてコードを修正・追加する

3. **リント**: ruff でコードを整形:
   ```bash
   .venv/bin/ruff check src/ tests/ --fix
   .venv/bin/ruff format src/ tests/
   ```

4. **テスト**: 必要なテストを追加し、全テストがパスすることを確認:
   ```bash
   .venv/bin/python3 -m pytest tests/ -v --tb=short
   ```

5. **Issue 更新**: Issue を以下の形式で更新（クローズはしない）:
   ```markdown
   ## 自動テスト（pytest で検証済み）
   - [x] 確認済みの項目

   ## 人間が確認すること
   - [ ] 目視・操作で確認が必要な項目
   ```
   ```bash
   gh issue comment <番号> --body "..."
   ```

6. **報告**: 変更内容と確認事項をユーザーに報告する
