"""日次パイプライン: 取得 → パース → DB保存 を1コマンドで実行する。

使い方:
    python -m src.daily
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from src.scraper.fetch import fetch_assets
from src.parser.normalize import parse_raw
from src.db.schema import init_db
from src.db.repository import save_snapshot


async def run(storage_state: str | None = None) -> None:
    # 1. 取得
    print("[1/3] データ取得中...")
    raw_path = await fetch_assets(storage_state)

    # 2. パース
    print("[2/3] パース中...")
    snapshot = parse_raw(raw_path)
    print(f"  日付: {snapshot.date}")
    print(f"  総資産: {snapshot.total_asset:,.0f}円")
    print(f"  口座: {len(snapshot.accounts)}件 / 銘柄: {len(snapshot.holdings)}件")

    # 3. DB保存
    print("[3/3] DB保存中...")
    conn = init_db()
    save_snapshot(conn, snapshot, str(raw_path))
    conn.close()

    print(f"\n完了: {snapshot.date} のスナップショットを保存しました。")


def main() -> None:
    parser = argparse.ArgumentParser(description="日次データ取得パイプライン")
    parser.add_argument("--storage-state", type=str, default=None)
    args = parser.parse_args()
    asyncio.run(run(args.storage_state))


if __name__ == "__main__":
    main()
