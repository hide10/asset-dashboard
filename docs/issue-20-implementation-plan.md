# Issue #20 実装計画メモ

対象Issue: `#20 ライフプラン: ライフイベント・大型支出の計画機能`

最終更新: 2026-03-08

## 合意済み仕様
- シミュレーションへのイベント反映は **年次減算**
- 教育費機能は **初期実装に含める**
- 物価上昇率は **まずグローバル1本**（イベント個別設定は後続）

## 実装スコープ（MVP）
1. DBスキーマ
- `life_events` テーブル追加
  - `id`, `event_type`, `title`, `amount`, `start_year`, `repeat_every_years`, `end_year`, `enabled`, `note`
- `children_profiles` テーブル追加
  - `id`, `name`, `birth_year`, `birth_month`, `education_plan_json`, `enabled`
- `life_plan_settings` テーブル追加
  - `inflation_rate`（年率）

2. Repository
- `life_events` CRUD
- `children_profiles` CRUD
- 子どもプロフィール + 教育費設定から年次イベント配列を生成する関数
- シミュレーション対象期間（開始年〜終了年）にイベントを展開する関数

3. シミュレーション統合
- 既存ライフサイクル計算に `year -> event_cost` を差し込む
- `eventなし` と `eventあり` の比較結果を返却
- サマリーに「累計イベント支出」を追加

4. UI（ライフプラン / シミュレーター）
- イベント管理カード
  - 単発イベント追加
  - 繰り返しイベント追加
  - 一覧表示・有効無効・削除
- 子ども登録カード
  - 生年月入力
  - 教育費プラン（公立/私立）選択
  - 自動生成プレビュー
- グラフ比較
  - イベントなし線
  - イベントあり線

5. テスト
- DB schema migration テスト
- repository CRUD テスト
- 教育費イベント生成テスト
- 年次イベント展開テスト
- シミュレーション反映テスト（イベントあり/なし比較）
- HTML/API表示テスト

## 段階実装順
- Phase 1: DB + repository + イベント展開ロジック
- Phase 2: シミュレーション統合 + 比較データ返却
- Phase 3: UI（イベント管理 + 子ども登録）
- Phase 4: テスト拡充 + 仕上げ

## 進捗ログ
- [x] 2026-03-08: 仕様合意（年次減算 / 教育費含む / 物価上昇率グローバル）
- [x] 2026-03-08: 実装計画メモ作成
- [x] 2026-03-08: DBスキーマ実装（life_events / children_profiles / life_plan_settings）
- [x] 2026-03-08: Repository実装（CRUD / 教育費生成 / 年次イベント展開）
- [x] 2026-03-08: シミュレーション関数に年次イベント支出入力口を追加
- [x] 2026-03-08: サーバー連携（`_get_simulator_data` / `/api/simulator` がイベント支出を反映）
- [x] 2026-03-08: 関連テスト通過（`tests/test_repository.py`, `tests/test_montecarlo.py`, `tests/test_server_html.py`）
- [ ] UI実装
- [ ] テスト追加
