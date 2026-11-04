"""Billing dynamic configuration labels keep their user-facing wording."""

from backend.core.config import DYNAMIC_CONFIG_LABELS


def test_payment_config_labels_are_chinese():
    assert DYNAMIC_CONFIG_LABELS["payment_enabled"] == "启用付费配额系统"
    assert (
        DYNAMIC_CONFIG_LABELS["payment_order_expire_minutes"] == "订单过期时间（分钟）"
    )
    assert DYNAMIC_CONFIG_LABELS["payment_default_currency"] == "默认货币"
