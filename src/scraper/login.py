"""手動ログインしてstorageStateを保存するスクリプト。

使い方:
    python -m src.scraper.login

ブラウザが開くので、マネーフォワードに手動でログインする。
ログイン完了後、ターミナルでEnterを押すとセッション情報が保存される。
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import sync_playwright

AUTH_DIR = Path(__file__).resolve().parents[2] / ".auth"
STORAGE_STATE_PATH = AUTH_DIR / "storage_state.json"

LOGIN_URL = "https://moneyforward.com/sign_in"


def save_login_state() -> Path:
    """ブラウザを開き、手動ログイン後にstorageStateを保存する。"""
    AUTH_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        page.goto(LOGIN_URL)
        print("\nブラウザでマネーフォワードにログインしてください。")
        print("ログイン完了後、ここでEnterを押してください...")
        input()

        # ログイン状態を確認
        current_url = page.url
        print(f"現在のURL: {current_url}")

        context.storage_state(path=str(STORAGE_STATE_PATH))
        print(f"\nセッション情報を保存しました: {STORAGE_STATE_PATH}")

        browser.close()

    return STORAGE_STATE_PATH


if __name__ == "__main__":
    save_login_state()
