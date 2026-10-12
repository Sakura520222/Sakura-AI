"""Browser E2E for real templates, JavaScript and API against an isolated fixture.

Run with uv run --with playwright python scripts/validate_notification_ui_646.py
after playwright install chromium. Provider/platform delivery is not simulated
as a successful external send by this browser test.
"""
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parents[1]
output = root / "test-results" / "notifications"
output.mkdir(parents=True, exist_ok=True)
server = subprocess.Popen([sys.executable, "-m", "tests.notification_ui_app_646"], cwd=root,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
try:
    for _ in range(60):
        try:
            if httpx.get("http://127.0.0.1:8646/notifications/settings").status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    else:
        raise RuntimeError("Notification browser fixture did not start")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1080})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:8646/settings/?lang=en")
        expect(page.get_by_label("Endpoint name").first).to_have_value("Personal email")
        page.get_by_label("Endpoint name").first.fill("My verified email")
        page.get_by_role("button", name="Save", exact=True).nth(1).click()
        expect(page.get_by_label("Endpoint name").first).to_have_value("My verified email")
        page.get_by_role("button", name="Send test", exact=True).click()
        expect(page.get_by_role("status")).to_contain_text("Test queued")
        page.get_by_label("Event type").select_option("review.completed")
        page.get_by_label("Target endpoint").select_option("1")
        page.get_by_label("Repository scope (owner/repo, optional)").fill("alice/Sakura-AI")
        page.get_by_label("Minimum severity").select_option("info")
        page.get_by_role("button", name="Add", exact=True).last.click()
        expect(page.get_by_text("review.completed · alice/Sakura-AI", exact=False)).to_be_visible()
        page.screenshot(path=str(output / "settings-en.png"), full_page=True)
        page.goto("http://127.0.0.1:8646/announcements?lang=en")
        expect(page.get_by_text("Sakura-AI PR #646 review completed", exact=True)).to_be_visible()
        page.get_by_role("button", name="Mark read", exact=True).click()
        expect(page.get_by_role("button", name="Mark read", exact=True)).to_have_count(0)
        page.get_by_text("Notification delivery diagnostics", exact=True).click()
        expect(page.get_by_text("database/provider/deployment", exact=True).first).to_be_visible()
        page.screenshot(path=str(output / "history-en.png"), full_page=True)
        page.goto("http://127.0.0.1:8646/settings/")
        expect(page.get_by_label("端点名称").first).to_have_value("My verified email")
        page.screenshot(path=str(output / "settings-zh.png"), full_page=True)
        assert not errors, errors
        browser.close()
    print("Notification browser E2E passed: rename, test queue, scoped preference, read state, diagnostics, bilingual render")
finally:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)
