# PRレビュー依頼メモ

## 目的
PR差分を AI レビュアーに渡して、バグ・回帰・セキュリティ問題を先に検出する。

## 推奨依頼文（共通）
以下を含める:
- 「バグ / 回帰 / セキュリティ問題を優先」
- 「問題なければ `No findings` と返す」
- 「ツールは使わない（差分だけで判断）」

## Claude（ローカルCLI）
```bash
gh pr diff 65 --repo hide10/asset-dashboard | \
claude -p "このdiffをレビューして。バグ、回帰、セキュリティ問題を優先。問題がなければ No findings。ツールは使わないで。"
```

## Gemini（ローカルCLI）
Gemini CLI は環境によってツール呼び出しで詰まることがあるため、diff を直接渡す。
```bash
gh pr diff 65 --repo hide10/asset-dashboard | \
gemini -p "このdiffをレビューして。バグ、セキュリティ問題、改善点を指摘して。ツールは使わないで。問題なければ No findings。"
```

## Gemini API 直叩き（確実）
```bash
DIFF="$(gh pr diff 65 --repo hide10/asset-dashboard)"
curl -s "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key=$GEMINI_API_KEY" \
  -H 'Content-Type: application/json' \
  -d "$(jq -n --arg diff "$DIFF" '{
    contents: [{parts: [{text: ("Review this PR diff. Focus on bugs, regressions, and security issues.\n\n" + $diff)}]}]
  }')" | jq -r '.candidates[0].content.parts[0].text'
```

## Codex（ローカルCLI）
```bash
codex review --base master
```
通信不安定時は失敗することがあるため、必要なら再実行する。
