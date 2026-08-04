"""fetch_url_tool 安全测试

覆盖：SSRF 绕过、URL 标准化、Content-Type、并发计数、域名策略等。
"""

import asyncio
import socket
from unittest.mock import patch

import pytest

from backend.core.config import DEFAULT_FETCH_URL_ALLOWED_CONTENT_TYPES
from backend.services.ai_reviewer.tools.fetch_url_tool import (
    FetchUrlToolHandler,
    _normalize_ip_octet,
    _try_parse_mixed_radix_ipv4,
)

# ── URL 标准化 ──────────────────────────────────────────────────


class TestNormalizeIPHostname:
    """Non-standard IP representation normalization"""

    def setup_method(self):
        self.handler = FetchUrlToolHandler()

    def test_ipv4_mapped_ipv6(self):
        assert self.handler._normalize_ip_hostname("::ffff:127.0.0.1") == "127.0.0.1"

    def test_ipv4_mapped_ipv6_hex(self):
        assert self.handler._normalize_ip_hostname("::ffff:7f00:1") == "127.0.0.1"

    def test_mixed_radix_octal_hex(self):
        assert self.handler._normalize_ip_hostname("0177.0.0x1.1") == "127.0.1.1"

    def test_all_numeric_domain(self):
        assert self.handler._normalize_ip_hostname("2130706433") == "127.0.0.1"

    def test_hex_integer(self):
        assert self.handler._normalize_ip_hostname("0x7f000001") == "127.0.0.1"

    def test_octal_dotted(self):
        result = self.handler._normalize_ip_hostname("0177.0.0.01")
        assert result == "127.0.0.1"

    def test_regular_hostname_unchanged(self):
        assert self.handler._normalize_ip_hostname("example.com") == "example.com"

    @pytest.mark.asyncio
    async def test_ipv4_mapped_private_blocked_in_ssrf(self):
        """After normalization, ::ffff:10.0.0.1 should be caught by SSRF check"""
        normalized = self.handler._normalize_ip_hostname("::ffff:10.0.0.1")
        assert normalized == "10.0.0.1"
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf(normalized)


class TestNormalizeIPOctet:
    def test_decimal(self):
        assert _normalize_ip_octet("127") == 127

    def test_octal(self):
        assert _normalize_ip_octet("0177") == 127

    def test_hex(self):
        assert _normalize_ip_octet("0x7f") == 127

    def test_zero(self):
        assert _normalize_ip_octet("0") == 0

    def test_empty(self):
        assert _normalize_ip_octet("") == 0


class TestMixedRadixIPv4:
    def test_standard_dotted(self):
        assert _try_parse_mixed_radix_ipv4("192.168.1.1") == "192.168.1.1"

    def test_mixed_octal_hex(self):
        assert _try_parse_mixed_radix_ipv4("0177.0.0x1.1") == "127.0.1.1"

    def test_not_four_parts(self):
        assert _try_parse_mixed_radix_ipv4("192.168.1") is None

    def test_out_of_range(self):
        assert _try_parse_mixed_radix_ipv4("999.999.999.999") is None


# ── URL 校验 ────────────────────────────────────────────────────


class TestValidateURL:
    def setup_method(self):
        self.handler = FetchUrlToolHandler()

    def test_reject_ftp(self):
        with pytest.raises(ValueError, match="不允许的协议"):
            self.handler._validate_url("ftp://example.com")

    def test_reject_file(self):
        with pytest.raises(ValueError, match="不允许的协议"):
            self.handler._validate_url("file:///etc/passwd")

    def test_reject_at_sign(self):
        with pytest.raises(ValueError, match="@"):
            self.handler._validate_url("http://evil@legit.com")

    def test_reject_missing_hostname(self):
        with pytest.raises(ValueError, match="主机名"):
            self.handler._validate_url("http://")

    def test_accept_https(self):
        url = self.handler._validate_url("https://example.com/path")
        assert url.startswith("https://")

    def test_accept_http(self):
        url = self.handler._validate_url("http://example.com/path")
        assert url.startswith("http://")

    def test_force_https_rejects_http(self):
        self.handler._force_https = True
        with pytest.raises(ValueError, match="仅允许 HTTPS"):
            self.handler._validate_url("http://example.com")

    def test_force_https_allows_https(self):
        self.handler._force_https = True
        url = self.handler._validate_url("https://example.com")
        assert url.startswith("https://")


# ── SSRF IP 检查 ────────────────────────────────────────────────


class TestSSRFCheck:
    def setup_method(self):
        self.handler = FetchUrlToolHandler()

    @pytest.mark.asyncio
    async def test_loopback_blocked(self):
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf("127.0.0.1")

    @pytest.mark.asyncio
    async def test_private_10_blocked(self):
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf("10.0.0.1")

    @pytest.mark.asyncio
    async def test_private_192_168_blocked(self):
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf("192.168.1.1")

    @pytest.mark.asyncio
    async def test_private_172_16_blocked(self):
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf("172.16.0.1")

    @pytest.mark.asyncio
    async def test_link_local_blocked(self):
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf("169.254.1.1")

    @pytest.mark.asyncio
    async def test_ipv6_loopback_blocked(self):
        with pytest.raises(ValueError, match="内网"):
            await self.handler._resolve_and_check_ssrf("::1")

    @pytest.mark.asyncio
    async def test_unresolvable_host(self):
        with (
            patch.object(
                socket, "getaddrinfo", side_effect=socket.gaierror("DNS lookup failed")
            ),
            pytest.raises(ValueError, match="DNS 解析失败"),
        ):
            await self.handler._resolve_and_check_ssrf("nonexistent.invalid")


# ── Content-Type 白名单 ─────────────────────────────────────────


class TestContentType:
    def setup_method(self):
        self.handler = FetchUrlToolHandler()

    def test_html_allowed(self):
        self.handler._check_content_type("text/html; charset=utf-8")

    def test_xhtml_allowed(self):
        self.handler._check_content_type("application/xhtml+xml")

    def test_plain_text_allowed_by_default(self):
        self.handler._check_content_type("text/plain")

    def test_missing_rejected(self):
        with pytest.raises(ValueError, match="缺少 Content-Type"):
            self.handler._check_content_type(None)

    def test_octet_stream_rejected(self):
        with pytest.raises(ValueError, match="不允许的 Content-Type"):
            self.handler._check_content_type("application/octet-stream")

    def test_json_rejected(self):
        with pytest.raises(ValueError, match="不允许的 Content-Type"):
            self.handler._check_content_type("application/json")

    def test_plain_text_can_be_rejected_by_custom_config(self):
        self.handler._allowed_content_types = frozenset({"text/html"})
        with pytest.raises(ValueError, match="不允许的 Content-Type"):
            self.handler._check_content_type("text/plain")

    def test_empty_content_type_config_falls_back_to_defaults(self):
        content_types = FetchUrlToolHandler._parse_content_types("")
        expected_content_types = {
            item.strip().lower()
            for item in DEFAULT_FETCH_URL_ALLOWED_CONTENT_TYPES.split(",")
        }

        assert content_types == expected_content_types


# ── 域名策略 ────────────────────────────────────────────────────


class TestDomainPolicy:
    def setup_method(self):
        self.handler = FetchUrlToolHandler()

    def test_off_allows_all(self):
        self.handler._domain_policy = "off"
        self.handler._check_domain_policy("internal.corp")

    def test_blacklist_blocks_match(self):
        self.handler._domain_policy = "blacklist"
        self.handler._domain_list = "*.internal.com,evil.com"
        with pytest.raises(ValueError, match="黑名单"):
            self.handler._check_domain_policy("corp.internal.com")

    def test_blacklist_allows_non_match(self):
        self.handler._domain_policy = "blacklist"
        self.handler._domain_list = "*.internal.com,evil.com"
        self.handler._check_domain_policy("example.com")

    def test_whitelist_blocks_non_match(self):
        self.handler._domain_policy = "whitelist"
        self.handler._domain_list = "docs.python.org,*.github.com"
        with pytest.raises(ValueError, match="白名单"):
            self.handler._check_domain_policy("evil.com")

    def test_whitelist_allows_match(self):
        self.handler._domain_policy = "whitelist"
        self.handler._domain_list = "docs.python.org,*.github.com"
        self.handler._check_domain_policy("docs.github.com")

    def test_wildcard_star(self):
        self.handler._domain_policy = "blacklist"
        self.handler._domain_list = "*.local"
        with pytest.raises(ValueError, match="黑名单"):
            self.handler._check_domain_policy("api.local")


# ── 并发会话计数 ────────────────────────────────────────────────


class TestConcurrentSessionCounter:
    @pytest.mark.asyncio
    async def test_concurrent_calls_counted(self):
        handler = FetchUrlToolHandler()
        handler._max_calls_per_session = 5

        async def increment():
            async with handler._session_lock:
                handler._session_call_count += 1

        await asyncio.gather(*[increment() for _ in range(5)])
        assert handler._session_call_count == 5

    @pytest.mark.asyncio
    async def test_reset_session(self):
        handler = FetchUrlToolHandler()
        handler._session_call_count = 10
        await handler.reset_session()
        assert handler._session_call_count == 0


# ── 可疑文本检测 ────────────────────────────────────────────────


class TestSuspiciousTextDetection:
    def test_clean_text(self):
        assert not FetchUrlToolHandler._detect_suspicious_text(
            "Hello, this is a normal text."
        )

    def test_homoglyph_cyrillic(self):
        # Mix Cyrillic 'а' (U+0430) with Latin
        text = "pаypal" + "а" * 10  # Cyrillic а mixed in
        assert FetchUrlToolHandler._detect_suspicious_text(text)

    def test_zero_width_chars(self):
        text = "hello\u200bworld\u200btest\u200b" * 3
        assert FetchUrlToolHandler._detect_suspicious_text(text)

    def test_normal_ascii_not_flagged(self):
        long_text = "a" * 10000
        assert not FetchUrlToolHandler._detect_suspicious_text(long_text)


# ── 脱敏 URL ────────────────────────────────────────────────────


class TestSanitizeURL:
    def setup_method(self):
        self.handler = FetchUrlToolHandler()

    def test_redacts_token(self):
        result = self.handler._sanitize_url_for_log(
            "https://example.com/path?token=secret123&foo=bar"
        )
        assert "secret123" not in result
        assert "***REDACTED***" in result
        assert "foo=bar" in result

    def test_no_query_unchanged(self):
        url = "https://example.com/path"
        assert self.handler._sanitize_url_for_log(url) == url

    def test_redacts_api_key(self):
        result = self.handler._sanitize_url_for_log(
            "https://api.example.com?key=abc123"
        )
        assert "abc123" not in result
