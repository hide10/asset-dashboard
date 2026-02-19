"""Playwright E2E tests for the budget feature on the /cf page.

Tests run against the demo server started on a random port.
"""

from __future__ import annotations

import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(PROJECT_ROOT / ".venv" / "bin" / "python3")
pytestmark = pytest.mark.e2e


def _free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def demo_server():
    """Start the demo server on a random port, yield the base URL, then stop it."""
    port = _free_port()
    tmp_db_path = tempfile.mktemp(suffix=".db")  # noqa: S306

    proc = subprocess.Popen(
        [PYTHON, "-m", "src.web.server", "--demo", "--port", str(port), "--db", tmp_db_path],
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for the server to be ready (up to 10 seconds)
    base_url = f"http://localhost:{port}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(f"Server failed to start.\nstdout: {stdout.decode()}\nstderr: {stderr.decode()}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    # Clean up temp DB
    Path(tmp_db_path).unlink(missing_ok=True)


@pytest.fixture()
def cf_page(page: Page, demo_server: str) -> Page:
    """Navigate to the /cf page and wait for it to load."""
    page.goto(f"{demo_server}/cf")
    page.wait_for_load_state("networkidle")
    return page


class TestBudgetColumnHeader:
    """The category table should contain a budget column header."""

    def test_budget_header_visible(self, cf_page: Page):
        header = cf_page.locator("th", has_text="予算")
        expect(header).to_be_visible()


class TestBudgetRemainingCard:
    """The budget remaining summary card should be visible."""

    def test_budget_remaining_card_visible(self, cf_page: Page):
        card = cf_page.locator("[data-testid='budget-remaining']")
        expect(card).to_be_visible()

    def test_budget_remaining_card_text(self, cf_page: Page):
        card = cf_page.locator("[data-testid='budget-remaining']")
        expect(card).to_contain_text("予算残り")


class TestBudgetCellEditing:
    """Clicking a budget cell should show an input field for editing."""

    def test_click_shows_input(self, cf_page: Page):
        cell = cf_page.locator(".budget-cell").first
        cell.click()
        input_el = cell.locator("input")
        expect(input_el).to_be_visible()
        expect(input_el).to_have_attribute("type", "number")

    def test_enter_saves_budget(self, cf_page: Page):
        # Find a budget cell, click it, clear existing value, type new value
        cell = cf_page.locator(".budget-cell").first
        cell.click()
        input_el = cell.locator("input")
        input_el.fill("50000")
        input_el.press("Enter")

        # After saving, the cell should display the formatted value
        expect(cell).to_contain_text("50,000円")

    def test_enter_zero_clears_budget(self, cf_page: Page):
        # First set a value, then clear it with 0
        cell = cf_page.locator(".budget-cell").first
        cell.click()
        input_el = cell.locator("input")
        input_el.fill("50000")
        input_el.press("Enter")
        # Wait for the cell to update
        expect(cell).to_contain_text("50,000円")

        # Now click again and enter 0 to clear
        cell.click()
        input_el = cell.locator("input")
        input_el.fill("0")
        input_el.press("Enter")

        # The cell should now show the dash character
        expect(cell).to_have_text("—")


class TestProgressBar:
    """Progress bars should exist and have correct color coding."""

    def test_progress_bar_exists(self, cf_page: Page):
        # Demo data has budgets set for some categories, so progress bars should exist
        bar = cf_page.locator(".budget-bar").first
        expect(bar).to_be_visible()

    def test_progress_bar_color_coding(self, cf_page: Page):
        """Progress bars should use blue (<80%), yellow (80-100%), or red (>100%)."""
        bars = cf_page.locator(".budget-bar")
        count = bars.count()
        assert count > 0, "Expected at least one progress bar"

        valid_colors = {"rgb(40, 129, 215)", "rgb(255, 213, 79)", "rgb(223, 55, 39)"}
        for i in range(count):
            bar = bars.nth(i)
            bg = bar.evaluate("el => getComputedStyle(el).backgroundColor")
            assert bg in valid_colors, f"Bar {i} has unexpected color: {bg}"


class TestBudgetRemainingUpdates:
    """The budget remaining card should update after editing a budget."""

    def test_remaining_updates_after_edit(self, cf_page: Page):
        card = cf_page.locator("[data-testid='budget-remaining']")
        amount_el = card.locator(".amount")

        # Get the initial remaining value text
        initial_text = amount_el.text_content()
        assert initial_text is not None

        # Edit a budget cell — change to a large value so remaining shifts
        cell = cf_page.locator(".budget-cell").first
        cell.click()
        input_el = cell.locator("input")
        input_el.fill("999999")
        input_el.press("Enter")

        # Wait for the card to update (the amount text should change)
        expect(amount_el).not_to_have_text(initial_text)
