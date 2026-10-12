"""Credential redaction shared by operational and persisted audit logs.

Keep diagnostic keys, error types and stack frames. Never inspect object attributes
or traceback locals. Scoped credentials cover opaque provider keys without keeping
them forever in a process-global registry.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import quote, quote_plus, unquote

REDACTED = "***"
_scoped_secrets: ContextVar[tuple[str, ...]] = ContextVar("log_secrets", default=())
_KEY = re.compile(
    r"(?:authorization|proxy_authorization|cookie|set_cookie|password|passwd|"
    r"client_secret|client_assertion|private_key|api_key|apikey|access_token|"
    r"refresh_token|id_token|oauth_code|oauth_state|code_verifier|code_challenge|"
    r"webhook_secret|signing_secret|secret_key|bot_token|credential|token|secret)",
    re.IGNORECASE,
)
_PAIR = re.compile(
    r"(?<![\w.-])(?P<prefix>[\"']?(?P<key>[\w.-]*(?:token|secret|password|passwd|api[_-]?key|"
    r"credential|private[_-]?key)|authorization|proxy-authorization|cookie|"
    r"set-cookie|client_assertion|code_verifier|code_challenge|oauth[_-]?(?:code|state)|code|state)"
    r"[\"']?\s*[:=]\s*)(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"(?:Bearer|Basic)\s+[^\s,;\]}]+|[^\s&,;\]}]+)",
    re.IGNORECASE,
)
_HEADER = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:\s*)[^\r\n]+"
)
_QUERY = re.compile(r"([?&#;])([^\s?&#;=]+)=([^\s&#;\"'<>]*)")
_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s@]+:)[^@/\s]+(@)", re.IGNORECASE)
_BOT = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_TOKEN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+|"
    r"sk-(?:proj-|ant-)?[A-Za-z0-9_-]+|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_PEM = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", re.DOTALL
)
_HEADER_PAIR = re.compile(
    r"(?P<prefix>b?[\"'](?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)[\"']\s*,\s*)"
    r"b?(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*')",
    re.IGNORECASE,
)


def sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "_", str(key).lower())
    # Counters/capabilities are useful diagnostics, not credentials.
    if normalized.endswith(("_tokens", "_token_count", "_token_limit")):
        return False
    return bool(_KEY.search(normalized))


@contextmanager
def sensitive_log_scope(*values: object):
    secrets = tuple(value for value in values if isinstance(value, str) and value)
    token = _scoped_secrets.set((*_scoped_secrets.get(), *secrets))
    try:
        yield
    finally:
        _scoped_secrets.reset(token)


def sanitize_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    text = value
    for secret in sorted({*_scoped_secrets.get(), *secrets}, key=len, reverse=True):
        if not secret:
            continue
        for encoded in {secret, quote(secret, safe=""), quote_plus(secret)}:
            text = text.replace(encoded, REDACTED)
    text = _PEM.sub(REDACTED, text)
    text = _USERINFO.sub(r"\1***\2", text)
    text = _BOT.sub("/bot***", text)

    def query(match: re.Match) -> str:
        prefix, key, item = match.groups()
        decoded = unquote(unquote(key)).lower().replace("-", "_")
        if sensitive_key(decoded) or decoded in {
            "code",
            "state",
            "key",
            "sig",
            "signature",
        }:
            item = REDACTED
        return f"{prefix}{key}={item}"

    text = _QUERY.sub(query, text)
    text = _HEADER.sub(r"\1***", text)
    text = _HEADER_PAIR.sub(lambda match: match["prefix"] + "'***'", text)
    text = _PAIR.sub(
        lambda match: (
            match[0]
            if match["key"].lower() in {"code", "state"} and match["value"].isdigit()
            else match["prefix"] + REDACTED
        ),
        text,
    )
    text = _AUTH.sub(r"\1 ***", text)
    return _TOKEN.sub(REDACTED, text)


def sanitize(value: object, *, secrets: tuple[str, ...] = (), _seen=None) -> object:
    """Return a JSON-safe copy; do not mutate caller-owned payloads."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return sanitize_text(value, secrets=secrets)
    seen = set() if _seen is None else _seen
    if id(value) in seen:
        return "<recursive>"
    seen.add(id(value))
    try:
        if isinstance(value, Mapping):
            return {
                sanitize_text(str(key), secrets=secrets): REDACTED
                if sensitive_key(key)
                or (str(key).lower() in {"code", "state"} and isinstance(item, str))
                else sanitize(item, secrets=secrets, _seen=seen)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and isinstance(value[0], (str, bytes))
                and sensitive_key(value[0])
            ):
                return [sanitize(value[0], secrets=secrets), REDACTED]
            return [sanitize(item, secrets=secrets, _seen=seen) for item in value]
        try:
            return sanitize_text(str(value), secrets=secrets)
        except Exception:
            return f"<unprintable {type(value).__name__}>"
    finally:
        seen.remove(id(value))
