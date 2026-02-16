---
name: security-check
description: パブリックリポジトリ向けセキュリティチェック
user_invocable: true
---

# /security-check スキル

パブリックリポジトリ公開に備えたセキュリティチェックを実行する。

## チェック項目

### 1. シークレットスキャン
Git 履歴とワーキングツリーから機密情報を検索する:
```bash
# トラッキングされているファイルに機密情報がないか
git ls-files | xargs grep -l -i -E '(api[_-]?key|secret|password|token|credential)\s*=' 2>/dev/null || echo "検出なし"

# .env ファイルがコミットされていないか
git ls-files | grep -i '\.env' || echo "検出なし"

# storage_state（セッション情報）がコミットされていないか
git ls-files | grep -i 'storage_state' || echo "検出なし"
```

### 2. .gitignore 確認
以下が .gitignore に含まれていることを確認:
- `.env` / `.env.*`
- `.auth/` / `**/storage_state*.json`
- `raw/` (個人データ)
- `data/*.db` (SQLite DB)
- `data/dividends.json` / `data/sectors.json` (ポートフォリオ情報)
- `*.pem` / `*.key` / `*.crt` / `credentials*.json`

### 3. ruff セキュリティルール (bandit)
```bash
.venv/bin/ruff check src/ tests/ --select S
```

### 4. 依存関係の脆弱性チェック
```bash
# pip-audit がインストールされていない場合はインストール
.venv/bin/pip-audit 2>/dev/null || (~/.local/bin/uv pip install pip-audit --python .venv/bin/python3 && .venv/bin/pip-audit)
```

### 5. 結果報告
各チェックの結果をまとめて報告:
- 問題なし / 要対応の分類
- 要対応項目には修正案を提示
