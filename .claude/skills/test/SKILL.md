---
name: test
description: ruff + pytest (unit + E2E) 実行 + 結果報告
user_invocable: true
---

# /test スキル

ruff によるリント・フォーマットチェック、pytest ユニットテスト、Playwright E2E テストを順番に実行し、結果をまとめて報告する。

## 手順

### ステップ 1: ruff リント & フォーマットチェック

```bash
.venv/bin/ruff check src/ tests/ && .venv/bin/ruff format --check src/ tests/
```

- check と format --check の両方を実行（自動修正はしない）
- 結果を記録: PASS or FAIL

### ステップ 2: ユニットテスト

```bash
.venv/bin/python3 -m pytest tests/test_repository.py tests/test_server_html.py -v --tb=short
```

- `test_repository.py` — DB クエリ関数のテスト
- `test_server_html.py` — HTML 生成・ナビゲーション・折りたたみのテスト
- テスト件数と結果を記録

### ステップ 3: E2E テスト (Playwright)

```bash
.venv/bin/python3 -m pytest tests/test_e2e_budget.py -v --tb=short
```

- `test_e2e_budget.py` — ブラウザ経由の予算管理 E2E テスト
- テスト件数と結果を記録

### ステップ 4: 結果サマリー報告

以下の形式で結果をまとめて報告する:

```
## テスト結果サマリー

| カテゴリ | 結果 | 詳細 |
|----------|------|------|
| ruff (lint + format) | PASS / FAIL | エラー数 |
| ユニットテスト | PASS / FAIL | N passed, M failed |
| E2E テスト | PASS / FAIL | N passed, M failed |
```

- **全テストパス**: 簡潔に報告
- **失敗あり**: 失敗したテスト・エラーごとに原因を分析し、修正案を提示

## 引数が渡された場合

引数が渡された場合はそのパスのみテスト（ruff チェックはスキップ）:
```bash
.venv/bin/python3 -m pytest <引数> -v --tb=short
```

## 全テストを一括実行する場合

```bash
.venv/bin/python3 -m pytest tests/ -v --tb=short
```
