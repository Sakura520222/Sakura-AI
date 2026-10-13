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
from urllib.parse import parse_qsl, quote_plus, unquote, urlsplit

REDACTED = "***"
_scoped_secrets: ContextVar[tuple[str, ...]] = ContextVar("log_secrets", default=())
_HEX_VALUES = {ord(char): int(char, 16) for char in "0123456789abcdefABCDEF"}
_KEY = re.compile(
    r"(?:authorization|proxy_authorization|cookie|set_cookie|password|passwd|"
    r"client_secret|client_assertion|private_key|api_key|apikey|access_token|"
    r"refresh_token|id_token|oauth_code|oauth_state|code_verifier|code_challenge|"
    r"webhook_secret|signing_secret|secret_key|bot_token|credential|token|secret)",
    re.IGNORECASE,
)
_PAIR = re.compile(
    r"(?<![\w.-])(?P<prefix>(?:\\*[\"'])?(?P<key>[\w.-]*(?:token|secret|password|passwd|api[_-]?key|"
    r"credential|private[_-]?key)|authorization|proxy-authorization|cookie|"
    r"set-cookie|client_assertion|code_verifier|code_challenge|oauth[_-]?(?:code|state)|code|state)"
    r"(?:\\*[\"'])?\s*[:=]\s*)(?P<value>b?(?P<quote>\\*[\"']).*?(?<!\\)(?P=quote)|"
    r"(?:Bearer|Basic)\s+[^\s,;\]}]+|[^\s&,;\]}]+)",
    re.IGNORECASE | re.DOTALL,
)
_HEADER = re.compile(
    r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key)\s*:\s*)[^\r\n]+"
)
_QUERY = re.compile(r"([?&#;])([^\s?&#;=]+)=([^\s&#;\"'<>]*)")
# Empty usernames are valid (notably Redis). The last @ within the authority
# separates the password from the host; query/fragment @ characters do not.
_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s?#]*:)[^/\s?#]+(@)", re.IGNORECASE)
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


def url_password_secrets(value: str) -> tuple[str, ...]:
    """Collect raw/decoded connection passwords while retaining endpoint diagnostics."""
    authority = re.split(r"[/?#]", value.partition("://")[2], maxsplit=1)[0]
    userinfo, separator, _ = authority.rpartition("@")
    raw_password = userinfo.partition(":")[2] if separator else ""
    # Keep the raw form too: urllib removes literal tabs/newlines from URLs.
    passwords = [raw_password, unquote(raw_password)]
    raw_query = value.partition("#")[0].partition("?")[2]
    passwords.extend(item for key, item in parse_qsl(raw_query) if key == "password")
    try:
        parsed = urlsplit(value)
    except ValueError:
        # A malformed configured URI must not prevent logging its original error.
        pass
    else:
        if parsed.password:
            passwords.extend((parsed.password, unquote(parsed.password)))
        passwords.extend(
            item for key, item in parse_qsl(parsed.query) if key == "password"
        )
    return tuple(dict.fromkeys(password for password in passwords if password))


@contextmanager
def sensitive_log_scope(*values: object):
    secrets = tuple(value for value in values if isinstance(value, str) and value)
    token = _scoped_secrets.set((*_scoped_secrets.get(), *secrets))
    try:
        yield
    finally:
        _scoped_secrets.reset(token)


def _percent_projection(text: str) -> tuple[bytes, list[tuple[int, int]]]:
    """Decode nested percent escapes in one pass, retaining original spans.

    Each reduction consumes two bytes, so deeply encoded input cannot require
    repeated whole-message decoding. Raw Unicode and encoded UTF-8 share the
    same byte representation; unmatched diagnostic text stays untouched.
    """
    nodes = []
    for position, char in enumerate(text):
        for byte in char.encode("utf-8", errors="surrogatepass"):
            nodes.append((byte, position, position + 1))
            while (
                len(nodes) >= 3
                and nodes[-3][0] == ord("%")
                and nodes[-2][0] in _HEX_VALUES
                and nodes[-1][0] in _HEX_VALUES
            ):
                byte = (_HEX_VALUES[nodes[-2][0]] << 4) | _HEX_VALUES[nodes[-1][0]]
                start, end = nodes[-3][1], nodes[-1][2]
                del nodes[-3:]
                nodes.append((byte, start, end))
    return bytes(node[0] for node in nodes), [(node[1], node[2]) for node in nodes]


def _replace_encoded_secret(text: str, secret: str) -> str:
    if "%" not in text and (" " not in secret or "+" not in text):
        return text
    projection, spans = _percent_projection(text)
    aliases = {
        _percent_projection(secret)[0],
        _percent_projection(quote_plus(secret))[0],
    }
    matches = []
    for alias in aliases:
        start = 0
        while (position := projection.find(alias, start)) != -1:
            end = position + len(alias)
            matches.append((spans[position][0], spans[end - 1][1]))
            start = end
    pieces = []
    start = 0
    for position, end in sorted(matches):
        if position >= start:
            pieces.extend((text[start:position], REDACTED))
        elif end <= start:
            continue
        start = end
    pieces.append(text[start:])
    return "".join(pieces)


def sanitize_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    text = value
    for secret in sorted({*_scoped_secrets.get(), *secrets}, key=len, reverse=True):
        if not secret:
            continue
        text = text.replace(secret, REDACTED)
        text = _replace_encoded_secret(text, secret)
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


def sanitize_exception_text(
    error: BaseException, *, secrets: tuple[str, ...] = ()
) -> str:
    """Redact before creating truncated logs or persisted diagnostic copies.

    Provider errors can outlive their credential scope. Preserve the original
    exception and use its attached redaction knowledge, including nested causes.
    """
    values = set(secrets)
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        values.update(getattr(current, "_log_redaction_secrets", ()))
        pending.extend((current.__cause__, current.__context__))
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return sanitize_text(str(error), secrets=tuple(values))


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
