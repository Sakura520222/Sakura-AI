"""URL 抓取工具处理器

为 AI 审查员提供网页内容抓取能力，使 AI 在搜索后能深入阅读相关文档/网页。
包含 SSRF 深层防护、下载体积限制、调用频率限制和审计日志。
"""

import ipaddress
import re
import socket
import time
from fnmatch import fnmatch
from typing import Any, Dict, List
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from backend.core.config import get_settings

# Sensitive query parameter keys to redact in audit logs
_SENSITIVE_PARAMS = frozenset(
    {"token", "key", "api_key", "apikey", "secret", "password", "access_token", "auth"}
)

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

_MAX_REDIRECTS = 3
_ALLOWED_SCHEMES = frozenset({"http", "https"})


class FetchUrlToolHandler:
    """URL 抓取工具处理器

    优先从 AppConfig 数据库读取配置（支持 WebUI 动态修改），
    未找到时回退到环境变量配置。
    """

    _CONFIG_MAP = {
        "fetch_url_enabled": "fetch_url_enabled",
        "fetch_url_timeout": "fetch_url_timeout",
        "fetch_url_max_content_length": "fetch_url_max_content_length",
        "fetch_url_max_download_size": "fetch_url_max_download_size",
        "fetch_url_max_calls_per_session": "fetch_url_max_calls_per_session",
        "fetch_url_domain_policy": "fetch_url_domain_policy",
        "fetch_url_domain_list": "fetch_url_domain_list",
    }

    _CONFIG_CACHE_TTL = 60

    def __init__(self) -> None:
        settings = get_settings()
        self._timeout: int = settings.fetch_url_timeout
        self._max_content_length: int = settings.fetch_url_max_content_length
        self._max_download_size: int = settings.fetch_url_max_download_size
        self._max_calls_per_session: int = settings.fetch_url_max_calls_per_session
        self._domain_policy: str = settings.fetch_url_domain_policy
        self._domain_list: str = settings.fetch_url_domain_list
        self._last_config_load: float = 0.0
        self._session_call_count: int = 0

    def reset_session(self) -> None:
        self._session_call_count = 0

    async def _load_config(self) -> None:
        if time.time() - self._last_config_load < self._CONFIG_CACHE_TTL:
            return

        try:
            from backend.models.database import AppConfig, async_session
            from sqlalchemy import select

            if async_session is None:
                return

            async with async_session() as session:
                keys = list(self._CONFIG_MAP.keys())
                result = await session.execute(
                    select(AppConfig).where(AppConfig.key_name.in_(keys))
                )
                configs = result.scalars().all()
                config_values = {c.key_name: c.key_value for c in configs}

            if not config_values:
                return

            if config_values.get("fetch_url_timeout"):
                self._timeout = int(config_values["fetch_url_timeout"])
            if config_values.get("fetch_url_max_content_length"):
                self._max_content_length = int(
                    config_values["fetch_url_max_content_length"]
                )
            if config_values.get("fetch_url_max_download_size"):
                self._max_download_size = int(
                    config_values["fetch_url_max_download_size"]
                )
            if config_values.get("fetch_url_max_calls_per_session"):
                self._max_calls_per_session = int(
                    config_values["fetch_url_max_calls_per_session"]
                )
            if config_values.get("fetch_url_domain_policy"):
                self._domain_policy = config_values["fetch_url_domain_policy"]
            if config_values.get("fetch_url_domain_list") is not None:
                self._domain_list = config_values["fetch_url_domain_list"]

            self._last_config_load = time.time()

        except (ValueError, TypeError) as e:
            logger.warning(f"URL 抓取配置值格式无效，使用环境变量默认值: {e}")
        except Exception as e:
            logger.debug(f"从数据库加载 URL 抓取配置失败，使用环境变量默认值: {e}")

    def _validate_url(self, url: str) -> str:
        """校验与标准化 URL，防止解析混淆绕过"""
        parsed = urlparse(url)

        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
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
        """将非标准 IP 表示法标准化为十进制格式"""
        # Try parsing as IPv4 — handles octal, hex, decimal, abbreviated forms
        try:
            # Python's ipaddress handles many non-standard forms
            addr = ipaddress.ip_address(hostname)
            return str(addr)
        except ValueError:
            pass

        # Try interpreting as integer-based IPv4
        try:
            addr = ipaddress.IPv4Address(int(hostname))
            return str(addr)
        except (ValueError, TypeError):
            pass

        # Try interpreting as hex integer IPv4
        if hostname.startswith("0x") or hostname.startswith("0X"):
            try:
                addr = ipaddress.IPv4Address(int(hostname, 16))
                return str(addr)
            except (ValueError, TypeError):
                pass

        # Try interpreting as octal (all digits start with 0)
        if (
            len(hostname) > 1
            and hostname[0] == "0"
            and hostname[1:].isdigit()
            and "." not in hostname
        ):
            try:
                addr = ipaddress.IPv4Address(int(hostname, 8))
                return str(addr)
            except (ValueError, TypeError):
                pass

        return hostname

    def _resolve_and_check_ssrf(self, hostname: str) -> str:
        """DNS 解析并检查 IP 是否为内网地址，返回解析到的 IP"""
        try:
            addr_infos = socket.getaddrinfo(hostname, None)
        except socket.gaierror as e:
            raise ValueError(f"DNS 解析失败: {hostname} — {e}")

        resolved_ips: List[str] = []
        for family, _, _, _, sockaddr in addr_infos:
            ip_str = sockaddr[0]
            resolved_ips.append(ip_str)

        # Check all resolved IPs against private networks
        for ip_str in resolved_ips:
            try:
                addr = ipaddress.ip_address(ip_str)
                for network in _PRIVATE_NETWORKS:
                    if addr in network:
                        raise ValueError(
                            f"目标地址 {ip_str} 属于内网/保留地址段，禁止访问"
                        )
            except ValueError as e:
                if "属于内网" in str(e):
                    raise
                # If ip_address() fails, skip this entry
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
            if domain_list and not any(
                fnmatch(hostname_lower, p) for p in domain_list
            ):
                raise ValueError(
                    f"域名 {hostname} 不在白名单中，禁止访问"
                )

    def _sanitize_url_for_log(self, url: str) -> str:
        """脱敏 URL 中的敏感查询参数"""
        parsed = urlparse(url)
        if not parsed.query:
            return url

        # Simple redaction for sensitive params
        redacted = re.sub(
            r"([?&])([^&=]*(?:token|key|secret|password|auth)[^&=]*)=([^&]*)",
            r"\1\2=***REDACTED***",
            url,
            flags=re.IGNORECASE,
        )
        return redacted

    def _html_to_text(self, html: str) -> str:
        """使用 BeautifulSoup4 将 HTML 转换为纯文本"""
        soup = BeautifulSoup(html, "html.parser")

        # Remove unwanted tags
        for tag in soup.find_all(
            ["script", "style", "nav", "footer", "header", "noscript", "iframe"]
        ):
            tag.decompose()

        # Try to get <body> content, fall back to whole document
        body = soup.find("body")
        target = body if body else soup

        text = target.get_text(separator="\n", strip=True)

        # Collapse excessive blank lines
        text = re.sub(r"\n{3,}", "\n\n", text)

        return text.strip()

    def _truncate(self, text: str) -> str:
        if len(text) <= self._max_content_length:
            return text
        return text[: self._max_content_length] + "..."

    async def fetch_url(self, url: str) -> Dict[str, Any]:
        """抓取网页并转换为纯文本"""
        start_time = time.time()

        # Check session call limit
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
        security_events: List[str] = []
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
            resolved_ip = self._resolve_and_check_ssrf(hostname)

            # Step 4: Fetch with redirect control
            content, status_code, download_bytes = await self._fetch_with_redirects(
                validated_url, resolved_ip
            )

            # Step 5: Extract text
            text = self._html_to_text(content)
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

            result: Dict[str, Any] = {
                "url": validated_url,
                "content": text,
                "content_length": len(text),
                "truncated": truncated,
            }

            if resolved_ip:
                result["resolved_ip"] = resolved_ip

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

    async def _fetch_with_redirects(
        self, url: str, resolved_ip: str
    ) -> tuple[str, int, int]:
        """执行 HTTP 请求，手动处理重定向并在每步进行 SSRF 校验"""
        current_url = url
        redirect_count = 0

        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False
        ) as client:
            while True:
                response = await client.get(
                    current_url,
                    headers={
                        "User-Agent": "Sakura-AI-Reviewer/1.0",
                        "Accept": "text/html,application/xhtml+xml,text/plain",
                    },
                )

                if response.status_code in (301, 302, 303, 307, 308):
                    redirect_count += 1
                    if redirect_count > _MAX_REDIRECTS:
                        raise ValueError(
                            f"重定向次数超过限制 ({_MAX_REDIRECTS})"
                        )

                    location = response.headers.get("location", "")
                    if not location:
                        raise ValueError("重定向响应缺少 Location 头")

                    from urllib.parse import urljoin

                    redirect_url = urljoin(current_url, location)

                    # Full SSRF validation on redirect target
                    redirect_url = self._validate_url(redirect_url)
                    redirect_parsed = urlparse(redirect_url)
                    redirect_hostname = self._normalize_ip_hostname(
                        redirect_parsed.hostname or ""
                    )
                    self._check_domain_policy(redirect_hostname)
                    self._resolve_and_check_ssrf(redirect_hostname)

                    current_url = redirect_url
                    continue

                # Not a redirect — read body with size limit
                status_code = response.status_code
                download_bytes = 0
                chunks: list[bytes] = []

                # Check Content-Length header first
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > self._max_download_size:
                    raise ValueError(
                        f"响应体积 ({content_length} 字节) 超过下载限制 "
                        f"({self._max_download_size} 字节)"
                    )

                # Stream-read with byte counting
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    download_bytes += len(chunk)
                    if download_bytes > self._max_download_size:
                        break
                    chunks.append(chunk)

                body = b"".join(chunks)

                # Try to decode as text
                try:
                    content = body.decode(
                        response.encoding or "utf-8", errors="replace"
                    )
                except (UnicodeDecodeError, LookupError):
                    content = body.decode("utf-8", errors="replace")

                return content, status_code, download_bytes

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
        security_events: List[str],
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
