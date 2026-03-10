"""日次パイプライン: 取得 → 比較 → (必要なら一括更新 → 再取得) → DB保存。

使い方:
    python -m src.daily                    # デフォルト（更新待ち60秒）
    python -m src.daily --wait 30          # 更新待ち30秒
    python -m src.daily --no-refresh       # 一括更新を行わない（従来動作）
    python -m src.daily --build-static     # dist/ に静的HTMLを生成
    python -m src.daily --deploy --deploy-project-name <name>  # Cloudflare Pagesへデプロイ
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
from pathlib import Path

from src.db.repository import (
    get_snapshot,
    save_cashflows,
    save_cf_csv_month,
    save_cf_transactions,
    save_setting,
    save_snapshot,
)
from src.db.schema import init_db
from src.parser.cashflow import parse_monthly
from src.parser.cf_csv import parse_cf_csv
from src.parser.normalize import AssetSnapshot, parse_raw
from src.scraper.fetch import create_context, fetch_cf_csv, fetch_monthly, fetch_page, request_aggregation


def _is_unchanged(current: AssetSnapshot, previous: dict) -> bool:
    """前回スナップショットとデータが変わっていないか判定する。

    total_asset と by_class の全額が一致する場合に「変化なし」とみなす。
    """
    if current.total_asset != previous["total_asset"]:
        return False
    prev_by_class = previous.get("by_class", {})
    return current.by_class == prev_by_class


async def run(
    storage_state: str | None = None,
    wait_seconds: int = 60,
    enable_refresh: bool = True,
    headless: bool = False,
    build_static: bool = False,
    deploy: bool = False,
    deploy_project_name: str | None = None,
) -> None:
    pw, browser, context = await create_context(storage_state, headless=headless, accept_downloads=True)
    try:
        page = await context.new_page()

        # 1. 初回取得
        print("[1/3] データ取得中...")
        raw_path = await fetch_page(page)

        # 2. パース
        print("[2/3] パース中...")
        snapshot = parse_raw(raw_path)
        print(f"  日付: {snapshot.date}")
        print(f"  総資産: {snapshot.total_asset:,.0f}円")
        print(f"  口座: {len(snapshot.accounts)}件 / 銘柄: {len(snapshot.holdings)}件")

        # 3. 前回データとの比較 → 必要なら一括更新
        if enable_refresh:
            conn = init_db()
            previous = get_snapshot(conn, snapshot.date)
            conn.close()

            needs_refresh = False
            if snapshot.total_asset == 0:
                # データ未ロード状態（金融機関連携がまだ反映されていない）
                print("\n総資産が0円です（金融機関データ未反映の可能性）。")
                needs_refresh = True
            elif previous and _is_unchanged(snapshot, previous):
                print(f"\n前回取得時からデータ変化なし（総資産: {snapshot.total_asset:,.0f}円）")
                needs_refresh = True
            elif previous:
                print(f"\n前回からデータ更新あり（前回: {previous['total_asset']:,.0f}円）")
            else:
                print("\n本日の初回取得です。")

            if needs_refresh:
                print("一括更新をリクエストします...")
                await request_aggregation(page)

                print(f"{wait_seconds}秒待機中...")
                await asyncio.sleep(wait_seconds)

                # 再取得
                print("\n再取得中...")
                raw_path = await fetch_page(page)
                snapshot = parse_raw(raw_path)
                print(f"  総資産: {snapshot.total_asset:,.0f}円")

                if snapshot.total_asset == 0:
                    print("\n※ 再取得後も総資産0円です。")
                    print("  金融機関側の更新に時間がかかっている可能性があります。")
                elif previous and _is_unchanged(snapshot, previous):
                    print("\n※ 再取得後もデータに変化がありませんでした。")
                    print("  金融機関側の更新に時間がかかっている可能性があります。")
                else:
                    print("\nデータが更新されました。")

        # 4. 月次収支取得
        try:
            print("\n月次収支データ取得中...")
            await fetch_monthly(page, raw_path)
            cashflows = parse_monthly(raw_path)
            if cashflows:
                print(f"  月次収支: {len(cashflows)}ヶ月分")
            else:
                print("  月次収支: パース結果なし（HTML構造確認後に調整）")
        except Exception as e:
            print(f"  月次収支取得失敗（続行）: {e}")
            cashflows = []

        # 5. 当月家計簿CSV取得
        try:
            from datetime import date as _d

            today = _d.today()
            csv_path = await fetch_cf_csv(page, today.year, today.month, raw_path)
            if csv_path:
                cf_transactions = parse_cf_csv(csv_path)
                if cf_transactions:
                    print(f"  家計簿CSV: {len(cf_transactions)}件")
                else:
                    print("  家計簿CSV: パース結果なし")
            else:
                cf_transactions = []
                print("  家計簿CSVダウンロード失敗（続行）")
        except Exception as e:
            print(f"  家計簿CSV取得失敗（続行）: {e}")
            cf_transactions = []

        # 6. DB保存
        print("\n[3/3] DB保存中...")
        conn = init_db()
        if snapshot.total_asset > 0:
            save_snapshot(conn, snapshot, str(raw_path))
            from datetime import datetime

            save_setting(conn, "last_fetch_at", datetime.now().isoformat())
            print(f"\n完了: {snapshot.date} のスナップショットを保存しました。")
        else:
            print("\n※ 総資産0円のため、スナップショットの保存をスキップしました。")
        if cashflows:
            from datetime import date as _date

            save_cashflows(conn, cashflows, _date.today().isoformat())
        if cf_transactions:
            from datetime import date as _date2

            today_str = _date2.today().isoformat()
            ym = f"{_date2.today().year}-{_date2.today().month:02d}"
            save_cf_transactions(conn, cf_transactions, today_str)
            save_cf_csv_month(conn, ym, today_str, len(cf_transactions))
            print(f"  家計簿CSV: {len(cf_transactions)}件をDB保存")
        conn.close()

        # 7. 静的HTMLビルド / Cloudflare Pages デプロイ（任意）
        if build_static or deploy:
            print("\n静的HTMLを生成します（dist/）...")
            from scripts.build_static import build as build_static_pages

            out_dir = build_static_pages(output_dir=Path("dist"), mode="live", db_path="data/assets.db")
            print(f"  静的HTML生成完了: {out_dir}")

            if deploy:
                cmd = ["wrangler", "pages", "deploy", str(out_dir)]
                if deploy_project_name:
                    cmd += ["--project-name", deploy_project_name]
                print("Cloudflare Pages へデプロイします...")
                print(" ", " ".join(cmd))
                try:
                    subprocess.run(cmd, check=True)
                    print("  Cloudflare Pages デプロイ完了")
                except FileNotFoundError:
                    print("  wrangler コマンドが見つかりません。npm で wrangler をインストールしてください。")
                except subprocess.CalledProcessError as e:
                    print(f"  デプロイ失敗（exit={e.returncode}）")

    finally:
        await browser.close()
        await pw.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="日次データ取得パイプライン")
    parser.add_argument("--storage-state", type=str, default=None)
    parser.add_argument("--wait", type=int, default=60, help="一括更新後の待機秒数（デフォルト: 60）")
    parser.add_argument("--no-refresh", action="store_true", help="データ未更新時の一括更新を行わない")
    parser.add_argument("--build-static", action="store_true", help="処理後に dist/ へ静的HTMLを生成する")
    parser.add_argument(
        "--deploy", action="store_true", help="処理後に Cloudflare Pages へデプロイする（wrangler 必須）"
    )
    parser.add_argument("--deploy-project-name", type=str, default=None, help="Cloudflare Pages の project name")
    args = parser.parse_args()
    asyncio.run(
        run(
            args.storage_state,
            args.wait,
            not args.no_refresh,
            False,
            args.build_static,
            args.deploy,
            args.deploy_project_name,
        )
    )


if __name__ == "__main__":
    main()
