"""AI provider endpoint 安全校验 / AI provider endpoint security validation.

防护目标：
- 避免内置厂商账号把保存的 API Key 发送到任意自定义 base URL。
- 避免模型发现接口被滥用为 SSRF。

规则：
- 内置远程厂商只能使用目录声明的 base_url / endpoints 对应域名，或同域区域变体。
- 自定义 provider（custom/custom-anthropic）允许 HTTPS 公网端点，以及 HTTP/HTTPS 本地或私网端点。
- 本地 provider（ollama/vllm/lmstudio）允许 loopback/private 地址，用于本机部署。
"""

from __future__ import annotations

from ipaddress import ip_address
from urllib.parse import urlparse

from backend.core.ai_protocol.models import ProtocolFamily, ProviderDeclaration
from backend.core.ai_providers import get_builtin_provider

_CUSTOM_PROVIDER_IDS = {"custom", "custom-anthropic"}
_LOCAL_PROVIDER_IDS = {"ollama", "vllm", "lmstudio"}

# Provider endpoints are deliberately allow-listed by their complete hostname.
# Do not derive a rule from a public suffix (for example ``aliyuncs.com``):
# doing so would allow an attacker-controlled sibling such as
# ``evil.aliyuncs.com`` to receive a built-in provider's API key.  Regional
# aliases are recorded here only when the provider catalog documents the
# exact hostname.
_PROVIDER_HOSTS: dict[str, frozenset[str]] = {
    "qwen": frozenset(
        {
            "dashscope.aliyuncs.com",  # CN/default
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
        }
    ),
    "qwen-coding-plan": frozenset(
        {
            "coding.dashscope.aliyuncs.com",
            "coding-intl.dashscope.aliyuncs.com",
        }
    ),
    "moonshot": frozenset({"api.moonshot.ai", "api.moonshot.cn"}),
}


def _parse_family(value: str | ProtocolFamily | None) -> ProtocolFamily | None:
    if isinstance(value, ProtocolFamily):
        return value
    if not value:
        return None
    try:
        return ProtocolFamily(str(value))
    except ValueError:
        return None


def _host(url: str) -> str:
    parsed = urlparse(url)
    return (parsed.hostname or "").lower().rstrip(".")


def _scheme(url: str) -> str:
    return urlparse(url).scheme.lower()


def _is_private_or_loopback_host(host: str) -> bool:
    if host in {"localhost"} or host.endswith(".localhost"):
        return True
    try:
        ip = ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _declared_base_urls(decl: ProviderDeclaration) -> set[str]:
    urls = {decl.base_url}
    urls.update(decl.endpoints.values())
    return {u for u in urls if u}


def _allowed_hosts_for(decl: ProviderDeclaration) -> set[str]:
    return {_host(url) for url in _declared_base_urls(decl) if _host(url)}


def _provider_allowed_hosts(provider_id: str, declared_hosts: set[str]) -> set[str]:
    """Return the exact hostnames documented for ``provider_id``.

    Most providers have no regional aliases and therefore use the hosts from
    their declaration verbatim.  Entries in ``_PROVIDER_HOSTS`` replace that
    set with an explicit, reviewed list so that no arbitrary subdomain or
    public-suffix match can pass validation.
    """

    return set(_PROVIDER_HOSTS.get(provider_id, frozenset(declared_hosts)))


def validate_provider_base_url(
    provider_id: str,
    api_base: str,
    *,
    protocol: str | ProtocolFamily | None = None,
) -> tuple[bool, str]:
    """校验 provider/api_base 组合是否安全 / Validate provider/base URL safety.

    Returns ``(ok, message)``. Empty ``api_base`` is always safe because runtime
    falls back to the catalog-declared endpoint.
    """
    api_base = (api_base or "").strip()
    if not api_base:
        return True, ""

    decl = get_builtin_provider(provider_id)
    parsed = urlparse(api_base)
    if not parsed.scheme or not parsed.hostname:
        return False, "API Base URL 格式无效"

    scheme = _scheme(api_base)
    host = _host(api_base)

    if decl.id in _LOCAL_PROVIDER_IDS:
        if scheme not in {"http", "https"}:
            return False, "本地模型 provider 仅支持 HTTP 或 HTTPS endpoint"
        if host in {"localhost"} or _is_private_or_loopback_host(host):
            return True, ""
        return False, "本地模型 provider 只能指向 localhost、loopback 或私有网络地址"

    if decl.id in _CUSTOM_PROVIDER_IDS:
        if scheme not in {"http", "https"}:
            return False, "自定义 AI endpoint 仅支持 HTTP 或 HTTPS"
        if scheme == "http" and not _is_private_or_loopback_host(host):
            return False, "自定义公网 AI endpoint 必须使用 HTTPS"
        return True, ""

    if scheme != "https":
        return False, "内置远程 AI provider 只能使用 HTTPS endpoint"
    if _is_private_or_loopback_host(host):
        return False, "内置远程 AI provider 不能覆盖到本机或私有网络地址"

    family = _parse_family(protocol)
    # 若指定协议且目录有对应 endpoint，优先限定该协议 endpoint 的 host。
    if family is not None and family in decl.endpoints:
        declared_hosts = {_host(decl.endpoints[family])}
    else:
        declared_hosts = _allowed_hosts_for(decl)
    allowed_hosts = _provider_allowed_hosts(decl.id, declared_hosts)

    if host in allowed_hosts:
        return True, ""

    allowed = ", ".join(sorted(allowed_hosts)) or decl.base_url
    return (
        False,
        f"内置 provider {decl.id} 不允许使用非官方域名 {host}，允许域名: {allowed}。"
        "如需自定义中转地址，请选择 custom 或 custom-anthropic provider。",
    )


__all__ = ["validate_provider_base_url"]
