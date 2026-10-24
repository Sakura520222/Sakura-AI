"""Exercise the real Setup template/JS with isolated API fixtures.

Run: uv run --with playwright python scripts/validate_setup_ui_682.py
Uses an installed Chrome by default; PLAYWRIGHT_CHANNEL can select Chromium.
No deployment configuration, database, or administrator credential is used.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playwright.sync_api import expect, sync_playwright

from backend.webui.i18n import i18n
from backend.webui.routes.setup import _js_i18n_dict, templates

OUTPUT = Path(__file__).resolve().parents[1] / "test-results" / "setup-682"
OUTPUT.mkdir(parents=True, exist_ok=True)


def validate(browser, language):
    strings = i18n.get_all_translations(language)

    def translate(key):
        return strings.get(key, key)

    html = templates.env.get_template("setup_wizard.html").render(
        lang=language,
        _=translate,
        js_i18n=_js_i18n_dict(language),
        prefill_values={
            "database_url": "mysql+asyncmy://runtime:fixture@mysql.example/sakura",
            "redis_url": "redis://127.0.0.1:6379/0",
        },
    )
    report = {
        "success": False,
        "connected": True,
        "schema_ready": False,
        "initialized": False,
        "stage": "preflight",
        "retryable": True,
        "dialect": "mysql",
        "version": "8.4.13",
        "identity": "runtime@%",
        "log_bin": 1,
        "log_bin_trust_function_creators": 0,
        "privileges": ["CREATE", "ALTER", "TRIGGER"],
        "code": "billing_schema_privilege_required",
        "database_error_code": 1419,
        "failed_object": "billing_transactions_no_update",
        "warnings": [],
        "repair_sql": "-- Isolated browser fixture\nSELECT 1;\n",
        "guards": [{"name": "billing_transactions_no_update", "status": "missing"}],
    }
    ready = report | {
        "success": True,
        "schema_ready": True,
        "code": "database_preflight_ready",
        "database_error_code": None,
        "failed_object": None,
    }
    page = browser.new_page(viewport={"width": 1280, "height": 1000})
    errors = []
    repairs = []
    saves = []
    repaired = False
    page.on("pageerror", lambda error: errors.append(str(error)))

    def route_request(route):
        nonlocal repaired
        path = route.request.url.split("setup682.test", 1)[1]
        payload = {}
        status = 200
        if path == "/setup":
            route.fulfill(status=200, content_type="text/html", body=html)
            return
        if path == "/setup/api/state":
            payload = {"state": "in_progress", "current_step": 1}
        elif path == "/setup/api/ai-providers":
            payload = {"success": False}
        elif path == "/setup/api/test-connection":
            payload = {"success": True, "message": "Fixture connection"}
        elif path == "/setup/api/database-preflight":
            payload = ready if repaired else report
            status = 200 if repaired else 409
        elif path == "/setup/api/database-repair":
            repairs.append(route.request.post_data_json)
            if repairs[-1]["password"] == "wrong-password":
                payload = report | {
                    "stage": "repair",
                    "code": "database_admin_authentication_failed",
                    "database_error_code": 1045,
                }
                status = 500
            else:
                repaired = True
                payload = ready | {"stage": "repair", "repaired": True}
        elif path == "/setup/api/save-step":
            saves.append(route.request.post_data_json)
            # Even after a successful preflight, saving must honor a fresh
            # server-side failure and keep the user on the current step.
            payload = report if len(saves) == 1 else {"success": True}
            status = 409 if len(saves) == 1 else 200
            if len(saves) == 1:
                repaired = False
        else:
            raise AssertionError(f"Unexpected fixture request: {path}")
        route.fulfill(
            status=status, content_type="application/json", body=json.dumps(payload)
        )

    page.route("http://setup682.test/**", route_request)
    page.goto("http://setup682.test/setup", wait_until="networkidle")
    expect(page.get_by_text(translate("setup.db_title"), exact=True)).to_be_visible()
    expect(page.get_by_test_id("database-report")).not_to_be_visible()
    page.get_by_role("button", name=translate("setup.db_recheck"), exact=True).click()
    expect(page.get_by_role("status")).to_have_text(
        translate("setup.db_status_billing_schema_privilege_required")
    )
    assert "log_bin" not in page.get_by_test_id("database-report").inner_text()
    assert "DEFINER" not in page.get_by_test_id("database-report").inner_text()
    assert "TRIGGER" not in page.get_by_test_id("database-report").inner_text()
    assert len(page.get_by_test_id("database-report").inner_text()) < 300
    page.screenshot(path=str(OUTPUT / f"preflight-{language}.png"), full_page=True)
    expect(page.locator('[x-model="databaseAdminUsername"]')).to_be_visible()
    page.locator('[x-model="databaseAdminUsername"]').fill("persistent_dba")
    page.locator('[x-model="databaseAdminPassword"]').fill("one-time-browser-secret")
    page.get_by_role(
        "button", name=translate("setup.db_run_repair"), exact=True
    ).click()
    expect(page.get_by_test_id("database-report")).to_contain_text(
        translate("setup.db_status_database_preflight_ready")
    )
    expect(page.locator('[x-model="databaseAdminPassword"]')).to_have_value("")
    expect(page.locator('[x-model="databaseAdminUsername"]')).to_have_value("")
    assert repairs[0]["password"] == "one-time-browser-secret"
    assert repairs[0]["confirmed"] is True
    page.screenshot(path=str(OUTPUT / f"repaired-{language}.png"), full_page=True)
    page.get_by_role("button", name=translate("setup.test_redis"), exact=True).click()
    page.get_by_role("button", name=translate("setup.next_step"), exact=True).click()
    expect(page.get_by_role("status")).to_have_text(
        translate("setup.db_status_billing_schema_privilege_required")
    )
    expect(page.get_by_text(translate("setup.db_title"), exact=True)).to_be_visible()
    expect(page.locator('[x-model="databaseAdminUsername"]')).to_be_visible()
    page.locator('[x-model="databaseAdminUsername"]').fill("persistent_dba")
    page.locator('[x-model="databaseAdminPassword"]').fill("wrong-password")
    page.get_by_role(
        "button", name=translate("setup.db_run_repair"), exact=True
    ).click()
    expect(page.get_by_test_id("database-report")).to_contain_text(
        translate("setup.db_status_database_admin_authentication_failed")
    )
    assert len(saves) == 1
    expect(page.locator('[x-model="databaseAdminPassword"]')).to_have_value("")
    page.locator('[x-model="databaseAdminUsername"]').fill("persistent_dba")
    page.locator('[x-model="databaseAdminPassword"]').fill("second-one-time-secret")
    page.get_by_role(
        "button", name=translate("setup.db_run_repair"), exact=True
    ).click()
    expect(page.get_by_text(translate("setup.gh_title"), exact=True)).to_be_visible()
    assert len(saves) == 2
    assert all("one-time-browser-secret" not in json.dumps(body) for body in saves)
    assert all("second-one-time-secret" not in json.dumps(body) for body in saves)
    assert not errors, errors
    page.close()


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            channel=os.environ.get("PLAYWRIGHT_CHANNEL", "chrome")
        )
        try:
            for language in ("en", "zh-CN"):
                validate(browser, language)
        finally:
            browser.close()
    print(f"Setup browser checks passed (en / zh-CN): {OUTPUT}")


if __name__ == "__main__":
    main()
