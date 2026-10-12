"""Signed Webhook provider with pinned public DNS, bounded I/O and no redirects."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from backend.core.config import get_settings
from backend.core.time_service import now_utc
from backend.services.secret_crypto_service import SecretCryptoError, decrypt_secret


def validate_webhook_url(value):
    from backend.services.notification_service import NotificationProviderPermanentError

    try:
        url = urlsplit(str(value).strip())
        hostname = url.hostname
        port = url.port
    except ValueError:
        raise NotificationProviderPermanentError("Invalid Webhook URL", "security") from None
    if (url.scheme != "https" or not hostname or url.username or url.password or
        url.query or url.fragment or port not in {None, 443} or
        any(ord(char) < 33 for char in value) or "\\" in value or "%" in hostname):
        raise NotificationProviderPermanentError("Webhook requires an HTTPS URL without credentials or query parameters", "security")
    host = hostname.rstrip(".").encode("idna").decode("ascii").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise NotificationProviderPermanentError("Webhook host is not public", "security")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not _public_address(address):
            raise NotificationProviderPermanentError("Webhook host is not public", "security")
    allowed = {entry.strip().lower() for entry in get_settings().notification_webhook_allowed_hosts.split(",") if entry.strip()}
    if allowed and host not in allowed:
        raise NotificationProviderPermanentError("Webhook host is not allowed by deployment policy", "security")
    authority = f"[{host}]" if ":" in host else host
    normalized = urlunsplit(("https", authority, url.path or "/", "", ""))
    if len(normalized) > 320:
        raise NotificationProviderPermanentError("Webhook URL is too long", "security")
    return normalized


def _public_address(address):
    # Reject IPv4-mapped and transition/tunnel addresses as well as private,
    # loopback, unspecified, reserved, multicast and cloud metadata ranges.
    return address.is_global and not address.is_multicast and not (
        isinstance(address, ipaddress.IPv6Address) and
        (address.ipv4_mapped or address.sixtofour or address.teredo)
    )


async def resolve_public_addresses(host):
    from backend.services.notification_service import NotificationProviderPermanentError

    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    addresses = {(record[0], record[4][0]) for record in records}
    if not addresses or any(not _public_address(ipaddress.ip_address(ip)) for _, ip in addresses):
        raise NotificationProviderPermanentError("Webhook DNS must resolve only to public addresses", "security")
    return sorted(addresses)


class _PinnedResolver(AbstractResolver):
    def __init__(self, hostname, addresses):
        self.hostname, self.addresses = hostname, addresses

    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        if host != self.hostname:
            raise OSError("Unexpected Webhook host")
        return [{"hostname": host, "host": ip, "port": port, "family": kind,
                     "proto": socket.IPPROTO_TCP, "flags": socket.AI_NUMERICHOST}
                for kind, ip in self.addresses]

    async def close(self):
        return


def webhook_signature(secret, timestamp, body):
    return "sha256=" + hmac.new(secret.encode(), str(timestamp).encode() + b"." + body, hashlib.sha256).hexdigest()


def webhook_retry_after(value):
    """HTTP Retry-After allows delta seconds or an aware HTTP-date."""
    from backend.services.notification_service import normalize_retry_after

    numeric = normalize_retry_after(value)
    if numeric:
        return numeric
    try:
        deadline = parsedate_to_datetime(str(value))
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            return 0.0
        return max(0.0, (deadline - now_utc()).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def verify_webhook_signature(secret, timestamp, body, signature, *, tolerance_seconds=300):
    """Receiver helper: authenticate raw bytes and enforce a replay time window.

    Receivers also deduplicate X-Sakura-Event-ID. This function deliberately
    does not imply that an in-memory replay cache can ensure exactly-once.
    """
    try:
        timestamp = int(timestamp)
    except (ValueError, TypeError):
        return False
    return bool(secret) and abs(now_utc().timestamp() - timestamp) <= tolerance_seconds and hmac.compare_digest(
        webhook_signature(secret, timestamp, body), str(signature)
    )


class WebhookNotificationProvider:
    channel = "webhook"
    descriptor = {
        "channel": "webhook", "configuration_version": 1, "verified": True,
        "fields": ["url", "signing_secret"], "secret_fields": ["signing_secret"],
        "capabilities": ["json", "hmac-sha256", "ownership_challenge"],
        "rate_limit_scope": "database/provider/deployment",
        "retry_after_scope": "endpoint",
    }

    def deployment_status(self):
        return {"enabled": get_settings().notification_webhook_enabled, "configured": True}

    async def _request(self, endpoint, payload, event_id):
        from backend.services.notification_service import (
            NotificationProviderDisabled,
            NotificationProviderError,
            NotificationProviderPermanentError,
            NotificationProviderRetryAfter,
        )
        settings = get_settings()
        if not settings.notification_webhook_enabled:
            raise NotificationProviderDisabled("Webhook disabled")
        url = validate_webhook_url(endpoint.address)
        try:
            secret = decrypt_secret(endpoint.secret_encrypted)
        except SecretCryptoError as exc:
            raise NotificationProviderPermanentError("Webhook credential unavailable", "credential") from exc
        if not secret:
            raise NotificationProviderPermanentError("Webhook credential missing", "credential")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        timestamp = str(int(now_utc().timestamp()))
        host = urlsplit(url).hostname
        try:
            # Bound DNS as well as connect/read/response time. Pin the validated
            # addresses to the connector; the hostname remains TLS SNI/Host.
            async with asyncio.timeout(settings.notification_webhook_timeout_seconds):
                addresses = await resolve_public_addresses(host)
                connector = aiohttp.TCPConnector(resolver=_PinnedResolver(host, addresses), use_dns_cache=False)
                async with aiohttp.ClientSession(connector=connector, trust_env=False,
                    cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
                    timeout=aiohttp.ClientTimeout(total=settings.notification_webhook_timeout_seconds)) as client:
                    async with client.post(url, data=body, allow_redirects=False, headers={
                        "Content-Type": "application/json", "X-Sakura-Event-ID": event_id,
                        "X-Sakura-Timestamp": timestamp,
                        "X-Sakura-Signature": webhook_signature(secret, timestamp, body),
                    }) as response:
                        if 300 <= response.status < 400:
                            raise NotificationProviderPermanentError("Webhook redirects are forbidden", "security")
                        if response.status == 429:
                            raise NotificationProviderRetryAfter("Webhook rate limited", webhook_retry_after(response.headers.get("Retry-After", 1)), scope="endpoint")
                        if response.status in {401, 403}:
                            raise NotificationProviderPermanentError("Webhook credential rejected", "credential")
                        if response.status >= 500 or response.status in {408, 425}:
                            raise NotificationProviderError("Webhook temporarily unavailable")
                        if response.status >= 400:
                            raise NotificationProviderPermanentError("Webhook target rejected request")
                        # read(n) may return short chunks; bound the entire response.
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(4096):
                            data.extend(chunk)
                            if len(data) > settings.notification_webhook_response_max_bytes:
                                raise NotificationProviderPermanentError("Webhook response too large", "security")
                        return bytes(data)
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            raise NotificationProviderError("Webhook transport unavailable") from exc

    async def send_message(self, *, endpoint, message):
        await self._request(endpoint, message.as_dict(), message.event_id)

    async def verify_endpoint(self, endpoint):
        from backend.services.notification_service import (
            NotificationProviderPermanentError,
        )

        challenge = secrets.token_urlsafe(32)
        result = await self._request(endpoint, {"type": "endpoint.verify", "challenge": challenge}, "verify:" + challenge)
        try:
            echoed = json.loads(result).get("challenge", "")
        except (ValueError, AttributeError):
            echoed = ""
        if not hmac.compare_digest(str(echoed), challenge):
            raise NotificationProviderPermanentError("Webhook ownership challenge failed", "verification")

