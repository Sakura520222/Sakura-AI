"""Pricing setup stays discoverable before payment or charging is enabled."""

from html.parser import HTMLParser
from types import SimpleNamespace

import pytest

from backend.webui.deps import get_templates
from backend.webui.i18n import i18n


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details_depth = 0
        self.links = []
        self.active_link = None

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.details_depth += 1
        if tag == "a":
            self.active_link = {
                **dict(attrs),
                "details_depth": self.details_depth,
                "label": "",
            }

    def handle_data(self, value):
        if self.active_link is not None:
            self.active_link["label"] += value

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1
        if tag == "a" and self.active_link is not None:
            self.links.append(self.active_link)
            self.active_link = None


@pytest.mark.parametrize("payment_enabled", [False, True])
@pytest.mark.parametrize("role", ["user", "admin", "super_admin"])
@pytest.mark.parametrize(
    "lang, labels",
    [
        ("zh-CN", ["模型定价", "套餐与价格"]),
        ("en", ["Model pricing", "Plans and prices"]),
    ],
)
def test_admin_pricing_setup_is_visible_outside_collapsed_billing(
    role, payment_enabled, lang, labels
):
    parser = LinkParser()
    parser.feed(
        get_templates()
        .get_template("components/sidebar.html")
        .render(
            active_page="config_unified",
            current_user={"role": role},
            settings=SimpleNamespace(
                payment_enabled=payment_enabled, billing_enabled=False
            ),
            _=lambda key: i18n.t(key, lang=lang),
        )
    )
    for href, label in zip(
        ["/billing/admin/pricing", "/billing/admin/plans"], labels, strict=True
    ):
        links = [link for link in parser.links if link.get("href") == href]
        if role != "super_admin":
            assert links == []
            continue
        assert len(links) == 1
        assert links[0]["label"].strip() == label
        assert links[0]["details_depth"] == 0


@pytest.mark.parametrize("lang", ["zh-CN", "en"])
def test_payment_config_card_exposes_both_setup_pages_without_enabling_payment(lang):
    parser = LinkParser()
    html = (
        get_templates()
        .get_template("components/config_dynamic_card.html")
        .render(
            group={"id": "payment", "label": "Billing", "fields": []},
            card_id="section-group-payment",
            current_user={"role": "super_admin"},
            settings=SimpleNamespace(payment_enabled=False, billing_enabled=False),
            _=lambda key: i18n.t(key, lang=lang),
        )
    )
    parser.feed(html)
    assert {link["href"] for link in parser.links} == {
        "/billing/admin/pricing",
        "/billing/admin/plans",
    }
    assert "模型定价" in html if lang == "zh-CN" else "Model pricing" in html


@pytest.mark.parametrize(
    "role, group", [("user", "payment"), ("admin", "payment"), ("super_admin", "rag")]
)
def test_pricing_setup_card_links_do_not_leak_to_other_roles_or_groups(role, group):
    html = (
        get_templates()
        .get_template("components/config_dynamic_card.html")
        .render(
            group={"id": group, "label": "Test", "fields": []},
            card_id="section-group-test",
            current_user={"role": role},
        )
    )
    assert 'href="/billing/admin/pricing"' not in html
    assert 'href="/billing/admin/plans"' not in html
