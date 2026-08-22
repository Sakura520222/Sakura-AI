"""Billing WebUI route helpers tests"""

from pathlib import Path

from backend.core.config import DYNAMIC_CONFIG_LABELS
from backend.webui.routes.billing import _parse_page


class SettingsStub:
    def __init__(self, payment_enabled: bool):
        self.payment_enabled = payment_enabled


def test_parse_page_defaults_invalid_value():
    assert _parse_page("abc") == 1


def test_parse_page_clamps_negative_value():
    assert _parse_page("-1") == 1


def test_parse_page_accepts_positive_value():
    assert _parse_page("3") == 3


def test_payment_config_labels_are_chinese():
    assert DYNAMIC_CONFIG_LABELS["payment_enabled"] == "启用付费配额系统"
    assert (
        DYNAMIC_CONFIG_LABELS["payment_order_expire_minutes"] == "订单过期时间（分钟）"
    )
    assert DYNAMIC_CONFIG_LABELS["payment_default_currency"] == "默认货币"


def test_sidebar_hides_billing_links_when_payment_disabled():
    from backend.webui.deps import get_templates

    template = get_templates().get_template("components/sidebar.html")
    rendered = template.render(
        active_page="dashboard",
        current_user={"role": "super_admin"},
        # Override cached template globals so this test is isolated from settings state.
        settings=SettingsStub(payment_enabled=False),
    )

    assert "套餐中心" not in rendered
    assert "套餐管理" not in rendered
    assert "兑换码管理" not in rendered
    assert "退款审核" not in rendered


def test_sidebar_shows_billing_links_when_payment_enabled():
    from backend.webui.deps import get_templates

    template = get_templates().get_template("components/sidebar.html")
    rendered = template.render(
        active_page="dashboard",
        current_user={"role": "super_admin"},
        # Override cached template globals so this test is isolated from settings state.
        settings=SettingsStub(payment_enabled=True),
    )

    assert "套餐中心" in rendered
    assert "套餐管理" in rendered
    assert "兑换码管理" in rendered
    assert "退款审核" in rendered


def test_sidebar_hides_refund_reviews_for_non_super_admin():
    from backend.webui.deps import get_templates

    template = get_templates().get_template("components/sidebar.html")
    rendered = template.render(
        active_page="dashboard",
        current_user={"role": "admin"},
        settings=SettingsStub(payment_enabled=True),
    )

    assert "套餐中心" in rendered
    assert "退款审核" not in rendered


def test_admin_code_edit_payload_preserves_expiration_fold():
    source = (
        Path(__file__).parents[1] / "backend/webui/templates/billing/admin_codes.html"
    ).read_text(encoding="utf-8")

    assert '"expires_at_fold": code.expires_at|datetime_local_fold' in source
    assert "code.expires_at_fold === 0 || code.expires_at_fold === 1" in source
    assert "String(code.expires_at_fold)" in source
