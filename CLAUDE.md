# CLAUDE.md

このファイルは AI アシスタント（Claude Code、スマホ版 Claude 等）がこのプロジェクトで作業する際の手引き。

## プロジェクト概要

マネーフォワード ME の資産・家計簿データを取得・分析する個人用ツール。
詳細は `README.md` を参照。

## 開発環境

```bash
# Python 仮想環境
source .venv/bin/activate   # または .venv/bin/python3 で直接実行

# デモモードで動作確認（実データ不要）
.venv/bin/python3 -m src.web.server --demo

# テスト実行
.venv/bin/python3 -m pytest tests/ -v

# リント + フォーマット（ruff）
.venv/bin/ruff check src/ tests/ --fix
.venv/bin/ruff format src/ tests/

# パッケージ追加（uv 使用）
~/.local/bin/uv pip install <package> --python .venv/bin/python3
```

## ツールチェーン

| ツール | 用途 | 設定 |
|--------|------|------|
| **ruff** | リンター + フォーマッター | `pyproject.toml [tool.ruff]` |
| **pytest** | テストフレームワーク | `pyproject.toml [tool.pytest.ini_options]` |
| **pre-commit** | コミット前に ruff 自動実行 | `.pre-commit-config.yaml` |

### ruff ルール

- `E`, `F`, `W` — pyflakes / pycodestyle
- `I` — isort（import 整列）
- `UP` — pyupgrade（Python バージョン対応）
- `B` — bugbear（バグ予防）
- `SIM` — simplify（コード簡略化）
- line-length: 120、target: Python 3.12

### Claude Code スキル

| コマンド | 内容 |
|----------|------|
| `/test` | pytest 実行 + 失敗分析 |
| `/lint` | ruff check + format + 結果報告 |
| `/demo` | デモサーバー起動 |
| `/fix-issue <番号>` | Issue 番号を渡して実装→テスト→Issue更新 |
| `/security-check` | シークレットスキャン + 依存脆弱性チェック |

## 主要ファイル

| ファイル | 役割 |
|---------|------|
| `src/web/server.py` | Web ダッシュボード（全ページの HTML 生成・API エンドポイント） |
| `src/db/repository.py` | DB アクセス層（クエリ関数） |
| `src/db/schema.py` | SQLite スキーマ定義 |
| `src/daily.py` | 日次データ取得パイプライン |
| `tests/test_repository.py` | DB クエリ関数のテスト |
| `tests/test_server_html.py` | HTML 生成・ナビゲーション・折りたたみのテスト |

## ページ構成

| パス | 関数 | 内容 |
|------|------|------|
| `/` | `_build_html()` | 資産ダッシュボード |
| `/cf` | `_build_cf_html()` | 家計簿分析 |
| `/plan` | `_build_plan_html()` | ライフプランニング |
| `/settings` | `_build_settings_html()` | 設定 |

各ページにはデモデータ生成関数がある: `_demo_data()`, `_demo_cf_data()`, `_demo_plan_data()`

共通パーツ:
- `_nav_html(active)` — ツールバー形式ナビゲーション
- `_NAV_CSS` — ナビゲーション CSS
- `_COLLAPSE_CSS` / `_COLLAPSE_JS` — カード折りたたみ

## 開発フロー

### 1. Issue 確認
```bash
gh issue list
```

### 2. 実装
- Issue 番号を参照しながら作業
- コードを変更したら **必ずテストを実行**: `.venv/bin/python3 -m pytest tests/ -v`
- テストが通らない状態でコミットしない

### 3. テスト追加
- 新しい repository 関数 → `tests/test_repository.py` にテスト追加
- 新しい HTML カードや UI 変更 → `tests/test_server_html.py` にテスト追加
- テストは自動検証できるもの（データ構造、HTML 内の文字列存在確認）と、人間が確認すべきもの（見た目、操作感）を分ける

### 4. Issue 更新
実装完了後、Issue を以下の形式に更新する:
```markdown
## 自動テスト（pytest で検証済み）
- [x] テストで確認済みの項目

## 人間が確認すること
- [ ] 目視・操作で確認が必要な項目
```

### 5. コミット
Issue に紐づく場合は `#番号` を含める:
```
ナビゲーションをツールバー形式に変更 (#13)
```

### 6. 人間の確認
- 人間が「人間が確認すること」のチェックリストを消化
- 全項目 OK → Issue をクローズ

## GitHub Issues の運用ルール

### Issue の書き方テンプレート

```markdown
## 概要
何をしたいか / 何が問題か

## 現状
今どうなっているか

## 変更案
どう変えるか

## 対象ファイル
- `src/web/server.py`: 変更箇所の説明

## 自動テスト（pytest で検証済み）
- [x] テストで確認済みの項目

## 人間が確認すること
- [ ] 具体的な操作手順と期待結果
```

### チェックリストの書き方

**自動テスト（AI が書く）:**
- HTML 内に特定の要素が存在するか
- データ構造が正しいか
- DB クエリの結果が正しいか

**人間が確認すること:**
- デザイン・見た目が自然か
- クリックして遷移が正しいか
- モバイル表示の崩れがないか
- 実データ（ログインセッション）が必要な操作

### AI が実装する場合

1. `gh issue list` で未対応の Issue を確認
2. Issue の内容を読んで実装
3. テストを書いて `pytest` で全テストが通ることを確認
4. Issue を更新し、自動テスト済み項目と人間確認項目を分離
5. 人間に確認を依頼する（**Issue は閉じない** — 人間が確認して閉じる）

## セキュリティ（パブリックリポジトリ対応）

- **絶対にコミットしないもの**: `.env`, `.auth/`, `raw/`, `data/*.db`, `storage_state*.json`
- API キーは環境変数 or DB settings 経由で取得（ハードコード禁止）
- ruff の `S` (bandit) ルールでセキュリティリントを常時実行
- 定期的に `/security-check` スキルでシークレットスキャン + 依存脆弱性チェックを実施

## コーディング規約

- コード変更後は `ruff check` と `ruff format` を通す（pre-commit でも自動実行される）
- Web サーバーは**標準ライブラリのみ**（外部フレームワーク不使用）
- フロントエンドは vanilla HTML/CSS/JS + Canvas（ライブラリ不使用）
- DB クエリは `repository.py` に集約、`server.py` から直接 SQL を書かない
- デモモード (`--demo`) で全機能が動作確認できるようにする
- 新しいカードには `data-card-id` 属性をつけて折りたたみに対応させる
- 共通 CSS/HTML は定数・関数に抽出する（`_NAV_CSS`, `_nav_html()` 等）
- 重複コードを見つけたらリファクタリングする
