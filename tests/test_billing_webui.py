"""Billing WebUI route helpers tests"""

from backend.webui.routes.billing import _parse_page


def test_parse_page_defaults_invalid_value():
    assert _parse_page("abc") == 1


def test_parse_page_clamps_negative_value():
    assert _parse_page("-1") == 1


def test_parse_page_accepts_positive_value():
    assert _parse_page("3") == 3
