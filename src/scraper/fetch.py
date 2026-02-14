"""マネーフォワード資産画面のスクレイパー。

Playwrightを使用して資産画面にアクセスし、
HTML / JSON / スクリーンショットをrawディレクトリに保存する。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page, Response

RAW_DIR = Path(__file__).resolve().parents[2] / "raw"
PORTFOLIO_URL = "https://moneyforward.com/bs/portfolio"
MONTHLY_URL = "https://moneyforward.com/cf/monthly"
AGGREGATION_URL = "https://moneyforward.com/aggregation_queue"
DEFAULT_STORAGE_STATE = Path(__file__).resolve().parents[2] / ".auth" / "storage_state.json"


def _build_raw_path() -> Path:
    """日時単位のrawディレクトリパスを生成する。"""
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    path = RAW_DIR / ts
    path.mkdir(parents=True, exist_ok=True)
    return path


async def create_context(
    storage_state: str | None = None,
) -> tuple:
    """Playwrightのブラウザコンテキストを作成する。

    Returns:
        (playwright, browser, context) のタプル。
        呼び出し側で browser.close() と playwright の終了を行うこと。
    """
    if storage_state is None:
        storage_state = str(DEFAULT_STORAGE_STATE)

    if not Path(storage_state).exists():
        raise FileNotFoundError(
            f"storageStateが見つかりません: {storage_state}\n"
            "先に python -m src.scraper.login でログインしてください。"
        )

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=False)
    context = await browser.new_context(
        storage_state=storage_state,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    )
    return pw, browser, context


async def fetch_page(page: Page) -> Path:
    """既存のページで資産画面を取得しrawデータを保存する。"""
    raw_path = _build_raw_path()
    api_responses: list[dict] = []

    async def on_response(response: Response) -> None:
        url = response.url
        if "moneyforward.com" in url and response.status == 200:
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    body = await response.json()
                    api_responses.append({
                        "url": url,
                        "status": response.status,
                        "body": body,
                    })
                except Exception:
                    pass

    page.on("response", on_response)

    print(f"アクセス中: {PORTFOLIO_URL}")
    await page.goto(PORTFOLIO_URL, wait_until="networkidle", timeout=60000)

    # ログインページにリダイレクトされた場合
    if "sign_in" in page.url:
        raise RuntimeError(
            "ログインページにリダイレクトされました。セッションが期限切れです。\n"
            "python -m src.scraper.login で再ログインしてください。"
        )

    await page.wait_for_timeout(3000)

    # HTML保存
    html_content = await page.content()
    html_path = raw_path / "asset.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"HTML保存: {html_path}")

    # スクリーンショット保存（フルページ）
    png_path = raw_path / "asset.png"
    await page.screenshot(path=str(png_path), full_page=True)
    print(f"スクリーンショット保存: {png_path}")

    # APIレスポンスJSON保存
    if api_responses:
        json_path = raw_path / "api_responses.json"
        json_path.write_text(
            json.dumps(api_responses, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"APIレスポンス保存: {json_path} ({len(api_responses)}件)")

    # リスナー解除
    page.remove_listener("response", on_response)

    print(f"raw保存完了: {raw_path}")
    return raw_path


async def fetch_monthly(page: Page, raw_path: Path) -> Path:
    """月次収支ページを取得しrawデータを保存する。"""
    api_responses: list[dict] = []

    async def on_response(response: Response) -> None:
        url = response.url
        if "moneyforward.com" in url and response.status == 200:
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    body = await response.json()
                    api_responses.append({
                        "url": url,
                        "status": response.status,
                        "body": body,
                    })
                except Exception:
                    pass

    page.on("response", on_response)

    print(f"アクセス中: {MONTHLY_URL}")
    await page.goto(MONTHLY_URL, wait_until="networkidle", timeout=60000)

    # ログインページにリダイレクトされた場合
    if "sign_in" in page.url:
        page.remove_listener("response", on_response)
        raise RuntimeError(
            "ログインページにリダイレクトされました。セッションが期限切れです。\n"
            "python -m src.scraper.login で再ログインしてください。"
        )

    await page.wait_for_timeout(3000)

    # HTML保存
    html_content = await page.content()
    html_path = raw_path / "monthly.html"
    html_path.write_text(html_content, encoding="utf-8")
    print(f"HTML保存: {html_path}")

    # スクリーンショット保存
    png_path = raw_path / "monthly.png"
    await page.screenshot(path=str(png_path), full_page=True)
    print(f"スクリーンショット保存: {png_path}")

    # APIレスポンスJSON保存
    if api_responses:
        json_path = raw_path / "monthly_api.json"
        json_path.write_text(
            json.dumps(api_responses, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"APIレスポンス保存: {json_path} ({len(api_responses)}件)")

    page.remove_listener("response", on_response)

    print(f"月次収支raw保存完了: {raw_path}")
    return raw_path


async def request_aggregation(page: Page) -> None:
    """一括更新をリクエストする。

    資産ページ上の「一括更新」ボタン（a[data-method="post"][href="/aggregation_queue"]）を
    クリックして POST リクエストを発行する。ボタンが見つからない場合は直接 POST する。
    """
    # 資産ページ上のボタンをクリック
    btn = page.locator('a[href="/aggregation_queue"][data-method="post"]')
    if await btn.count() > 0:
        print("一括更新ボタンをクリック...")
        await btn.first.click()
        await page.wait_for_timeout(3000)
        print("一括更新リクエスト完了")
    else:
        # ボタンが見つからない場合、API で直接 POST
        print("一括更新ボタンが見つかりません。API で直接リクエスト...")
        resp = await page.request.post(AGGREGATION_URL)
        print(f"一括更新リクエスト完了（status: {resp.status}）")


async def fetch_assets(storage_state: str | None = None) -> Path:
    """資産画面を取得しrawデータを保存する（単体実行用）。"""
    pw, browser, context = await create_context(storage_state)
    try:
        page = await context.new_page()
        raw_path = await fetch_page(page)
        return raw_path
    finally:
        await browser.close()
        await pw.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="マネーフォワード資産データ取得")
    parser.add_argument("--storage-state", type=str, default=None,
                        help="Playwright storageState JSONファイルのパス")
    args = parser.parse_args()
    asyncio.run(fetch_assets(args.storage_state))


if __name__ == "__main__":
    main()
