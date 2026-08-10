"""AI endpoint 安全校验测试。"""

import inspect

from backend.core.ai_protocol.account_probe import probe_account
from backend.core.ai_protocol.endpoint_security import validate_provider_base_url
from backend.services.ai_reviewer.unified_client import UnifiedAIClient


def test_builtin_provider_rejects_untrusted_api_base_to_prevent_key_exfiltration():
    ok, message = validate_provider_base_url(
        "openai",
        "https://evil.example/v1",
        protocol="openai_responses",
    )

    assert ok is False
    assert "不允许使用非官方域名" in message
    assert "custom" in message


def test_builtin_provider_rejects_private_network_api_base_to_prevent_ssrf():
    ok, message = validate_provider_base_url(
        "deepseek",
        "http://127.0.0.1:8080/v1",
        protocol="openai-compatible",
    )

    assert ok is False
    assert "HTTPS" in message or "本机" in message


def test_builtin_provider_allows_declared_endpoint_and_region_variants():
    ok, message = validate_provider_base_url(
        "qwen",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        protocol="openai-compatible",
    )

    assert ok is True
    assert message == ""

    ok, message = validate_provider_base_url(
        "qwen",
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        protocol="openai-compatible",
    )
    assert ok is True
    assert message == ""


def test_builtin_provider_rejects_untrusted_siblings_of_documented_region_host():
    """A public suffix must never act as an implicit provider allow-list."""
    for host in (
        "evil.aliyuncs.com",
        "attacker.dashscope.aliyuncs.com",
        "evil.coding.dashscope.aliyuncs.com",
        "attacker.api.openai.com",
    ):
        provider = (
            "qwen-coding-plan"
            if "coding" in host
            else "openai"
            if "openai" in host
            else "qwen"
        )
        ok, message = validate_provider_base_url(
            provider,
            f"https://{host}/v1",
            protocol="openai-compatible",
        )
        assert ok is False, (provider, host, message)


def test_builtin_provider_allows_only_documented_regional_aliases():
    ok, message = validate_provider_base_url(
        "qwen-coding-plan",
        "https://coding-intl.dashscope.aliyuncs.com/v1",
        protocol="openai-compatible",
    )
    assert ok is True
    assert message == ""

    ok, message = validate_provider_base_url(
        "moonshot",
        "https://api.moonshot.cn/v1",
        protocol="openai-compatible",
    )
    assert ok is True
    assert message == ""


def test_custom_provider_allows_https_and_local_http_endpoints():
    ok, message = validate_provider_base_url("custom", "https://proxy.example/v1")
    assert ok is True
    assert message == ""

    ok, message = validate_provider_base_url("custom", "http://localhost:8000/v1")
    assert ok is True
    assert message == ""

    ok, message = validate_provider_base_url(
        "custom-anthropic", "http://192.168.1.10:8080"
    )
    assert ok is True
    assert message == ""

    ok, message = validate_provider_base_url("custom", "http://proxy.example/v1")
    assert ok is False
    assert "HTTPS" in message


def test_ai_endpoint_clients_disable_redirects():
    """账号探测与实际调用均不得自动跟随跨域重定向。"""
    assert "follow_redirects=False" in inspect.getsource(probe_account)
    assert "follow_redirects=False" in inspect.getsource(
        UnifiedAIClient.http_client.fget
    )


def test_local_provider_allows_loopback():
    ok, message = validate_provider_base_url("ollama", "http://127.0.0.1:11434/v1")
    assert ok is True
    assert message == ""


def test_local_provider_rejects_non_http_scheme():
    ok, message = validate_provider_base_url("ollama", "ftp://127.0.0.1:11434/v1")

    assert ok is False
    assert "HTTP" in message
