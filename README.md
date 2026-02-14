# MoneyForward ME 資産トラッカー

マネーフォワード ME の「資産」画面から1日1回データを取得し、資産の推移・分析・将来予測を行う個人用ツール。

## 目的

- 毎日の資産状況をスナップショットとして記録する
- 口座別・銘柄別・資産クラス別の増減を把握する（前日/前月/前年比較）
- 保有株式の業種分散と配当予測を確認する
- モンテカルロ・シミュレーションで将来の資産レンジを推定する

## 全体の流れ

```
[ Playwright (ブラウザ自動操作) ]
    ↓  HTML / Screenshot 保存
[ raw/ ディレクトリ ]
    ↓  パース
[ SQLite DB ]
    ↓  分析・予測
[ Web ダッシュボード / CLI ]
```

サイトへのアクセスは**1日1回のみ**。取得した raw データを元にすべての分析を行い、再アクセスはしない。

## セットアップ

```bash
# 仮想環境の作成と依存インストール
uv venv .venv
source .venv/bin/activate
uv pip install -e .

# Playwright のブラウザをインストール
playwright install chromium
```

> システムに Chromium の依存ライブラリが足りない場合:
> `sudo playwright install-deps chromium`

## 使い方

### 1. 初回ログイン（手動）

ブラウザが開くので、マネーフォワードに手動でログインする。
ログイン完了後、ターミナルで Enter を押すとセッション情報が `.auth/storage_state.json` に保存される。

```bash
python -m src.scraper.login
```

### 2. 日次データ取得（毎日1回）

取得 → パース → DB保存 を1コマンドで実行する。

```bash
python -m src.daily
```

raw データは `raw/YYYY-MM-DD_HHMMSS/` に保存され、解析結果が `data/assets.db` に格納される。

### 3. ダッシュボード表示

```bash
python -m src.web.server
# → http://localhost:8080
```

ダッシュボードの機能:
- 資産総額と資産クラス別内訳（円グラフ）
- 前日比 / 前月比 / 前年比の変動表示
- 口座一覧（残高順）
- 保有銘柄一覧（クラス別グループ）
- 株式の業種別内訳（円グラフ）
- 年間配当予測（銘柄別内訳つき）
- 成長予測：追加投資なし / 積立込み（月額変更可能）
- 日付セレクタで過去データの閲覧

### 4. CLI レポート

```bash
python -m src.cli.report
```

### 5. デモモード（ダミーデータ）

実データを使わず架空のポートフォリオで表示する。SNS共有などに。

```bash
python -m src.web.server --demo
```

## プロジェクト構成

```
money_forward/
├── pyproject.toml
├── .gitignore
├── raw/                            # raw データ（git管理外）
├── data/                           # SQLite DB（git管理外）
├── .auth/                          # セッション情報（git管理外）
├── src/
│   ├── scraper/
│   │   ├── login.py                # 手動ログイン → storageState 保存
│   │   └── fetch.py                # Playwright でページ取得・raw 保存
│   ├── parser/
│   │   └── normalize.py            # HTML → 構造化データ
│   ├── db/
│   │   ├── schema.py               # SQLite スキーマ定義・初期化
│   │   └── repository.py           # スナップショット保存・取得
│   ├── analysis/
│   │   ├── compare.py              # 前日/前月/前年比較
│   │   └── metrics.py              # ボラティリティ・ドローダウン等
│   ├── prediction/
│   │   └── montecarlo.py           # モンテカルロ成長予測
│   ├── data/
│   │   └── stock_master.py         # 銘柄マスタ（業種・配当）
│   ├── cli/
│   │   └── report.py               # CLI レポート
│   ├── web/
│   │   └── server.py               # Web ダッシュボード
│   └── daily.py                    # 日次パイプライン
└── tests/
```

## 技術スタック

| 項目 | 技術 |
|------|------|
| ブラウザ自動操作 | Playwright (Chromium, headless=False) |
| HTML パース | BeautifulSoup4 + lxml |
| データベース | SQLite (WAL モード) |
| Web サーバー | Python 標準ライブラリ (http.server) |
| フロントエンド | vanilla HTML/CSS/JS, Canvas 円グラフ |
| 成長予測 | モンテカルロ・シミュレーション (stdlib random) |
| パッケージ管理 | uv |

外部フレームワーク不使用。Web ダッシュボードは stdlib の HTTPServer で動作する。

## 成長予測について

モンテカルロ・シミュレーションで 1年/3年/5年 後の資産額を P10（悲観）/ P50（中央）/ P90（楽観）のレンジで推定する。

- **リスク資産**（株式・投信）のみ市場変動の対象としてシミュレーション
- **安全資産**（預金・不動産・年金）は変動なしで固定加算
- 幾何ブラウン運動（対数正規モデル）で月次リターンを生成、10,000回実行
- パラメータは過去の日次リターンから推定（データ5日未満の場合はデフォルト: リターン5%/年、ボラ15%/年）
- 「積立込み」モードでは月額積立額を指定可能

ダッシュボードの `?` ボタンから詳細な説明を確認できる。

## 注意事項

- 本ツールは個人利用・自己分析用途に限る
- サイトへのアクセスは1日1回を厳守し、サーバーへの負荷を最小化する
- `.auth/`（セッション情報）、`raw/`（生データ）、`data/`（DB）は git 管理外
