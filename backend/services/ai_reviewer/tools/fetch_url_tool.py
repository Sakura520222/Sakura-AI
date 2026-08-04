"""URL 抓取工具处理器

为 AI 审查员提供网页内容抓取能力，使 AI 在搜索后能深入阅读相关文档/网页。
包含 SSRF 深层防护、下载体积限制、调用频率限制、Content-Type 白名单和审计日志。
"""

import asyncio
import ipaddress
import re
import socket
import time
import unicodedata
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urljoin, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from backend.core.config import DEFAULT_FETCH_URL_ALLOWED_CONTENT_TYPES, get_settings

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Unicode categories used by confusable (homoglyph) characters
_CONFUSABLE_CATEGORIES = frozenset({"Mn", "Cf", "Co"})

# Common confusable character ranges
_CONFUSABLE_RANGES: list[tuple[int, int]] = [
    (0x0300, 0x036F),  # Combining diacritical marks
    (0x1AB0, 0x1AFF),  # Combining diacritical marks extended
    (0x0400, 0x04FF),  # Cyrillic (confusable with Latin)
    (0x200B, 0x200F),  # Zero-width chars, direction marks
    (0x2028, 0x202F),  # Line/word separators, directional
    (0x2060, 0x206F),  # Word joiner, invisible chars
    (0xFE00, 0xFE0F),  # Variation selectors
    (0xFEFF, 0xFEFF),  # BOM / zero-width no-break space
]


def _normalize_ip_octet(octet: str) -> int:
    """Normalize a single IP octet: handle octal (077), hex (0x7f), decimal (127)."""
    if not octet:
        return 0
    if octet.lower().startswith("0x"):
        return int(octet, 16)
    if len(octet) > 1 and octet[0] == "0" and octet[1:].isdigit():
        return int(octet, 8)
    return int(octet)


def _try_parse_mixed_radix_ipv4(hostname: str) -> str | None:
    """Try to parse mixed-radix IPv4 like 0177.0.0x1.1"""
    parts = hostname.split(".")
    if len(parts) != 4:
        return None
    try:
        octets = [_normalize_ip_octet(p) for p in parts]
        if any(o < 0 or o > 255 for o in octets):
            return None
        return str(ipaddress.IPv4Address(bytes(octets)))
    except (ValueError, TypeError):
        return None


class FetchUrlToolHandler:
    """URL 抓取工具处理器

    优先从 AppConfig 数据库读取配置（支持 WebUI 动态修改），
    未找到时回退到环境变量配置。
    """

    _CONFIG_KEYS = [
        "fetch_url_timeout",
        "fetch_url_max_content_length",
        "fetch_url_max_download_size",
        "fetch_url_max_calls_per_session",
        "fetch_url_domain_policy",
        "fetch_url_domain_list",
        "fetch_url_force_https",
        "fetch_url_allowed_content_types",
        "fetch_url_max_redirects",
    ]

    _CONFIG_CACHE_TTL = 60

    def __init__(self) -> None:
        settings = get_settings()
        self._timeout: int = settings.fetch_url_timeout
        self._max_content_length: int = settings.fetch_url_max_content_length
        self._max_download_size: int = settings.fetch_url_max_download_size
        self._max_calls_per_session: int = settings.fetch_url_max_calls_per_session
        self._domain_policy: str = settings.fetch_url_domain_policy
        self._domain_list: str = settings.fetch_url_domain_list
        self._force_https: bool = settings.fetch_url_force_https
        self._allowed_content_types: frozenset[str] = self._parse_content_types(
            settings.fetch_url_allowed_content_types
        )
        self._max_redirects: int = settings.fetch_url_max_redirects
        self._last_config_load: float = 0.0
        self._session_call_count: int = 0
        self._session_lock = asyncio.Lock()

    async def reset_session(self) -> None:
        async with self._session_lock:
            self._session_call_count = 0

    async def _load_config(self) -> None:
        if time.time() - self._last_config_load < self._CONFIG_CACHE_TTL:
            return

        try:
            from sqlalchemy import select

            from backend.models.database import AppConfig, async_session

            if async_session is None:
                return

            async with async_session() as session:
                result = await session.execute(
                    select(AppConfig).where(AppConfig.key_name.in_(self._CONFIG_KEYS))
                )
                configs = result.scalars().all()
                config_values = {c.key_name: c.key_value for c in configs}

            if not config_values:
                return

            if config_values.get("fetch_url_timeout") is not None:
                self._timeout = int(config_values["fetch_url_timeout"])
            if config_values.get("fetch_url_max_content_length") is not None:
                self._max_content_length = int(
                    config_values["fetch_url_max_content_length"]
                )
            if config_values.get("fetch_url_max_download_size") is not None:
                self._max_download_size = int(
                    config_values["fetch_url_max_download_size"]
                )
            if config_values.get("fetch_url_max_calls_per_session") is not None:
                self._max_calls_per_session = int(
                    config_values["fetch_url_max_calls_per_session"]
                )
            if config_values.get("fetch_url_domain_policy") is not None:
                self._domain_policy = config_values["fetch_url_domain_policy"]
            if config_values.get("fetch_url_domain_list") is not None:
                self._domain_list = config_values["fetch_url_domain_list"]
            if config_values.get("fetch_url_force_https") is not None:
                self._force_https = config_values["fetch_url_force_https"] == "true"
            if config_values.get("fetch_url_allowed_content_types") is not None:
                self._allowed_content_types = self._parse_content_types(
                    config_values["fetch_url_allowed_content_types"]
                )
            if config_values.get("fetch_url_max_redirects") is not None:
                self._max_redirects = int(config_values["fetch_url_max_redirects"])

            self._last_config_load = time.time()

        except (ValueError, TypeError) as e:
            logger.warning(f"URL 抓取配置值格式无效，使用环境变量默认值: {e}")
        except Exception as e:
            logger.debug(f"从数据库加载 URL 抓取配置失败，使用环境变量默认值: {e}")

    def _validate_url(self, url: str) -> str:
        """校验与标准化 URL，防止解析混淆绕过"""
        parsed = urlparse(url)

        scheme = parsed.scheme.lower()
        if self._force_https:
            if scheme != "https":
                raise ValueError("当前配置仅允许 HTTPS 协议")
        elif scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"不允许的协议: {parsed.scheme}，仅支持 {', '.join(_ALLOWED_SCHEMES)}"
            )

        # Reject URLs containing @ (e.g. http://evil@legit.com)
        if "@" in (parsed.netloc or ""):
            raise ValueError("URL 中不允许包含 '@' 字符")

        hostname = parsed.hostname
        if not hostname:
            raise ValueError("URL 缺少主机名")

        # Normalize non-standard IP representations
        hostname = self._normalize_ip_hostname(hostname)

        # Rebuild URL with normalized hostname
        normalized = parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.replace(parsed.hostname or "", hostname)
            if parsed.hostname
            else parsed.netloc,
        )
        return urlunparse(normalized)

    def _normalize_ip_hostname(self, hostname: str) -> str:
        """将非标准 IP 表示法标准化为十进制格式

        处理场景：
        - IPv4-mapped IPv6: ::ffff:127.0.0.1 → 127.0.0.1
        - 混合进制: 0177.0.0x1.1 → 127.0.0.1
        - 全数字域名: 2130706433 → 127.0.0.1
        - 十六进制整数: 0x7f000001 → 127.0.0.1
        - 八进制整数: 017700000001 → 127.0.0.1
        """
        # Unwrap IPv4-mapped or IPv4-compatible IPv6
        unwrapped = self._unwrap_ipv6_mapped(hostname)
        if unwrapped:
            return unwrapped

        # Mixed-radix dotted notation (e.g. 0177.0.0x1.1)
        if "." in hostname:
            mixed = _try_parse_mixed_radix_ipv4(hostname)
            if mixed:
                return mixed

        # Standard ipaddress parsing
        try:
            addr = ipaddress.ip_address(hostname)
            return str(addr)
        except ValueError:
            pass

        # All-numeric hostname (integer IPv4, e.g. 2130706433)
        if hostname.isdigit():
            try:
                return str(ipaddress.IPv4Address(int(hostname)))
            except (ValueError, TypeError):
                pass

        # Hex integer IPv4 (e.g. 0x7f000001)
        if hostname.lower().startswith("0x"):
            try:
                return str(ipaddress.IPv4Address(int(hostname, 16)))
            except (ValueError, TypeError):
                pass

        # Octal integer IPv4 (e.g. 017700000001)
        if len(hostname) > 1 and hostname[0] == "0" and hostname[1:].isdigit():
            try:
                return str(ipaddress.IPv4Address(int(hostname, 8)))
            except (ValueError, TypeError):
                pass

        return hostname

    def _unwrap_ipv6_mapped(self, hostname: str) -> str | None:
        """Unwrap IPv4-mapped or IPv4-compatible IPv6 to plain IPv4.

        ::ffff:127.0.0.1 → 127.0.0.1
        ::ffff:7f00:1    → 127.0.0.1
        """
        try:
            addr = ipaddress.ip_address(hostname)
        except ValueError:
            return None

        if isinstance(addr, ipaddress.IPv6Address):
            # IPv4-mapped (::ffff:x.x.x.x)
            if addr.ipv4_mapped:
                return str(addr.ipv4_mapped)

        return None

    async def _resolve_and_check_ssrf(self, hostname: str) -> str:
        """DNS 解析并检查 IP 是否为内网地址，返回解析到的 IP

        使用 run_in_executor 避免阻塞事件循环。
        NOTE: 存在 TOCTOU 风险 — DNS 解析在此完成，但 httpx 发起请求时会
        再次独立解析。攻击者理论上可通过 DNS rebinding 绕过（第一次解析返回
        合法 IP，第二次返回内网 IP）。实际利用难度高（时间窗口极短），且
        httpx 不支持自定义 DNS resolver，此处作为已知可接受风险。
        """
        loop = asyncio.get_running_loop()
        try:
            addr_infos = await loop.run_in_executor(
                None, socket.getaddrinfo, hostname, None
            )
        except socket.gaierror as e:
            raise ValueError(f"DNS 解析失败: {hostname} — {e}")

        resolved_ips: list[str] = []
        for family, _, _, _, sockaddr in addr_infos:
            ip_str = sockaddr[0]
            resolved_ips.append(ip_str)

        for ip_str in resolved_ips:
            # Unwrap IPv4-mapped IPv6 before checking
            unwrapped = self._unwrap_ipv6_mapped(ip_str)
            check_ip = unwrapped or ip_str
            try:
                addr = ipaddress.ip_address(check_ip)
                for network in _PRIVATE_NETWORKS:
                    if addr in network:
                        raise ValueError(
                            f"目标地址 {ip_str} 属于内网/保留地址段，禁止访问"
                        )
            except ValueError as e:
                if "属于内网" in str(e):
                    raise
                continue

        return resolved_ips[0]

    def _check_domain_policy(self, hostname: str) -> None:
        """根据域名过滤策略检查"""
        policy = self._domain_policy.lower()
        if policy == "off":
            return

        domain_list = [
            d.strip().lower() for d in self._domain_list.split(",") if d.strip()
        ]
        hostname_lower = hostname.lower()

        if policy == "blacklist":
            for pattern in domain_list:
                if fnmatch(hostname_lower, pattern):
                    raise ValueError(f"域名 {hostname} 在黑名单中，禁止访问")

        elif policy == "whitelist":
            if domain_list and not any(fnmatch(hostname_lower, p) for p in domain_list):
                raise ValueError(f"域名 {hostname} 不在白名单中，禁止访问")

    def _sanitize_url_for_log(self, url: str) -> str:
        """脱敏 URL 中的敏感查询参数"""
        parsed = urlparse(url)
        if not parsed.query:
            return url

        sensitive_keys = {
            "token",
            "key",
            "api_key",
            "apikey",
            "secret",
            "password",
            "access_token",
            "auth",
        }
        params = parsed.query.split("&")
        redacted = []
        for param in params:
            if "=" not in param:
                redacted.append(param)
                continue
            key, _ = param.split("=", 1)
            if key.lower() in sensitive_keys:
                redacted.append(f"{key}=***REDACTED***")
            else:
                redacted.append(param)

        return urlunparse(parsed._replace(query="&".join(redacted)))

    @staticmethod
    def _parse_content_types(value: str | None) -> frozenset[str]:
        content_types = {
            item.strip().lower() for item in (value or "").split(",") if item.strip()
        }
        if not content_types:
            content_types = {
                item.strip().lower()
                for item in DEFAULT_FETCH_URL_ALLOWED_CONTENT_TYPES.split(",")
            }
        return frozenset(content_types)

    def _check_content_type(self, content_type: str | None) -> None:
        """Validate response Content-Type against the configured whitelist.

        Defaults to text/html, application/xhtml+xml, and text/plain. Rejects
        responses whose MIME type is not present in the active whitelist.
        """
        if not content_type:
            raise ValueError("响应缺少 Content-Type 头，拒绝处理")

        # Extract MIME type (ignore parameters like charset)
        mime = content_type.split(";")[0].strip().lower()
        if mime not in self._allowed_content_types:
            raise ValueError(
                f"不允许的 Content-Type: {mime}，仅支持 "
                f"{', '.join(sorted(self._allowed_content_types))}"
            )

    @staticmethod
    def _detect_suspicious_text(text: str) -> bool:
        """Detect homoglyph characters and suspicious Unicode patterns"""
        suspicious_count = 0
        for ch in text:
            cp = ord(ch)
            # Check known confusable ranges
            for start, end in _CONFUSABLE_RANGES:
                if start <= cp <= end:
                    suspicious_count += 1
                    break
            else:
                # Check Unicode category for invisible/confusable marks
                cat = unicodedata.category(ch)
                if cat in _CONFUSABLE_CATEGORIES:
                    suspicious_count += 1

        # Flag if more than 5 suspicious characters found
        return suspicious_count > 5

    def _html_to_text(self, html: str) -> tuple[str, bool]:
        """使用 BeautifulSoup4 将 HTML 转换为纯文本

        Returns:
            (text, suspicious) tuple — extracted text and suspicious flag
        """
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.find_all(
            ["script", "style", "nav", "footer", "header", "noscript", "iframe"]
        ):
            tag.decompose()

        body = soup.find("body")
        target = body if body else soup

        text = target.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        suspicious = self._detect_suspicious_text(text)
        return text, suspicious

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_content_length:
            return text
        return text[: self._max_content_length] + "..."

    async def fetch_url(self, url: str) -> dict[str, Any]:
        """抓取网页并转换为纯文本"""
        start_time = time.time()

        # Check session call limit (protected by lock)
        async with self._session_lock:
            self._session_call_count += 1
            call_num = self._session_call_count

        if call_num > self._max_calls_per_session:
            logger.warning(
                f"URL 抓取调用次数超限: 第 {call_num} 次，"
                f"上限 {self._max_calls_per_session}"
            )
            return {
                "url": url,
                "content": "",
                "error": f"已达到单次会话最大抓取次数限制 ({self._max_calls_per_session})",
            }

        await self._load_config()

        # Re-check limit after config reload
        if call_num > self._max_calls_per_session:
            return {
                "url": url,
                "content": "",
                "error": f"已达到单次会话最大抓取次数限制 ({self._max_calls_per_session})",
            }

        resolved_ip = ""
        security_events: list[str] = []
        status_code = 0
        download_bytes = 0

        try:
            # Step 1: Validate URL
            validated_url = self._validate_url(url)
            parsed = urlparse(validated_url)
            hostname = self._normalize_ip_hostname(parsed.hostname or "")

            # Step 2: Domain policy check
            self._check_domain_policy(hostname)

            # Step 3: DNS resolve + SSRF IP check
            resolved_ip = await self._resolve_and_check_ssrf(hostname)

            # Step 4: Fetch with redirect control and Content-Type check
            content, status_code, download_bytes = await self._fetch_with_redirects(
                validated_url
            )

            # Step 5: Extract text and check for suspicious content
            text, suspicious = self._html_to_text(content)
            if suspicious:
                security_events.append("可疑 Unicode 字符（可能为同形异义攻击）")
            truncated = len(text) > self._max_content_length
            text = self._truncate(text)

            elapsed_ms = int((time.time() - start_time) * 1000)
            self._audit_log(
                url=validated_url,
                resolved_ip=resolved_ip,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                download_bytes=download_bytes,
                text_length=len(text),
                truncated=truncated,
                call_num=call_num,
                security_events=security_events,
            )

            result: dict[str, Any] = {
                "url": validated_url,
                "content": text,
                "content_length": len(text),
                "truncated": truncated,
            }

            if resolved_ip:
                result["resolved_ip"] = resolved_ip
            if suspicious:
                result["suspicious"] = True

            return result

        except ValueError as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            security_events.append(str(e))
            self._audit_log(
                url=self._sanitize_url_for_log(url),
                resolved_ip=resolved_ip,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                download_bytes=download_bytes,
                text_length=0,
                truncated=False,
                call_num=call_num,
                security_events=security_events,
            )
            return {"url": url, "content": "", "error": str(e)}

        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            logger.error(f"URL 抓取失败: {e}", exc_info=True)
            self._audit_log(
                url=self._sanitize_url_for_log(url),
                resolved_ip=resolved_ip,
                status_code=status_code,
                elapsed_ms=elapsed_ms,
                download_bytes=download_bytes,
                text_length=0,
                truncated=False,
                call_num=call_num,
                security_events=security_events + [f"异常: {e}"],
            )
            return {"url": url, "content": "", "error": f"抓取失败: {e}"}

    async def _fetch_with_redirects(self, url: str) -> tuple[str, int, int]:
        """执行 HTTP 请求，手动处理重定向并在每步进行 SSRF 校验

        跟踪重定向链的总下载量，防止反射放大攻击。
        """
        current_url = url
        redirect_count = 0
        total_bytes = 0

        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False
        ) as client:
            while True:
                response = await client.get(
                    current_url,
                    headers={
                        "User-Agent": "Sakura-AI-Reviewer/1.0",
                        "Accept": ",".join(sorted(self._allowed_content_types)),
                    },
                )

                # Check Content-Type on redirect responses
                if response.status_code in (301, 302, 303, 307, 308):
                    ct = response.headers.get("content-type")
                    self._check_content_type(ct)

                    # Account for redirect response body bytes
                    redirect_body_len = len(response.content) if response.content else 0
                    total_bytes += redirect_body_len
                    if total_bytes > self._max_download_size:
                        raise ValueError(
                            f"重定向链累计下载量 ({total_bytes} 字节) 超过限制 "
                            f"({self._max_download_size} 字节)"
                        )

                    redirect_count += 1
                    if redirect_count > self._max_redirects:
                        raise ValueError(f"重定向次数超过限制 ({self._max_redirects})")

                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError("重定向响应缺少 Location 头")

                    redirect_url = urljoin(current_url, location)

                    # Full SSRF validation on redirect target
                    redirect_url = self._validate_url(redirect_url)
                    redirect_parsed = urlparse(redirect_url)
                    redirect_hostname = self._normalize_ip_hostname(
                        redirect_parsed.hostname or ""
                    )
                    self._check_domain_policy(redirect_hostname)
                    await self._resolve_and_check_ssrf(redirect_hostname)

                    current_url = redirect_url
                    continue

                # Non-redirect response
                status_code = response.status_code

                # Reject non-2xx errors early
                if not (200 <= status_code < 300):
                    raise ValueError(f"HTTP {status_code}: 无法获取页面内容")

                # Content-Type check only for successful responses
                ct = response.headers.get("content-type")
                self._check_content_type(ct)
                download_bytes = 0
                chunks: list[bytes] = []

                # Check Content-Length header first
                content_length = response.headers.get("content-length")
                if content_length:
                    cl = int(content_length)
                    if total_bytes + cl > self._max_download_size:
                        raise ValueError(
                            f"响应体积 ({cl} 字节) + 已下载 ({total_bytes} 字节) "
                            f"超过下载限制 ({self._max_download_size} 字节)"
                        )

                # Stream-read with byte counting
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    download_bytes += len(chunk)
                    if total_bytes + download_bytes > self._max_download_size:
                        break
                    chunks.append(chunk)

                total_bytes += download_bytes
                body = b"".join(chunks)

                try:
                    content = body.decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                except (UnicodeDecodeError, LookupError):
                    content = body.decode("utf-8", errors="replace")

                return content, status_code, total_bytes

    def _audit_log(
        self,
        url: str,
        resolved_ip: str,
        status_code: int,
        elapsed_ms: int,
        download_bytes: int,
        text_length: int,
        truncated: bool,
        call_num: int,
        security_events: list[str],
    ) -> None:
        """记录结构化审计日志"""
        parts = [
            f"[fetch_url #{call_num}]",
            f"url={url}",
            f"ip={resolved_ip or 'N/A'}",
            f"status={status_code}",
            f"time={elapsed_ms}ms",
            f"bytes={download_bytes}",
            f"text_len={text_length}",
            f"truncated={truncated}",
        ]
        if security_events:
            parts.append(f"security=[{'; '.join(security_events)}]")

        log_line = " | ".join(parts)

        if security_events:
            logger.warning(log_line)
        else:
            logger.info(log_line)
