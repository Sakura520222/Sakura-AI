"""Setup Token 管理函数单元测试。"""

from backend.core.bootstrap import (
    _COOKIE_NAME,
    _has_valid_setup_cookie,
    clear_setup_token,
    generate_setup_token,
    get_setup_token,
    validate_setup_token,
)


def _make_scope(cookie_header: str = "") -> dict:
    """构造最小 ASGI scope，注入指定 Cookie 头。"""
    headers = []
    if cookie_header:
        headers.append((b"cookie", cookie_header.encode("latin-1")))
    return {"headers": headers}


def test_generate_and_get_token():
    """generate_setup_token 后 get_setup_token 返回非空字符串。"""
    clear_setup_token()
    assert get_setup_token() is None

    generate_setup_token()
    token = get_setup_token()
    assert token is not None
    assert len(token) >= 30  # token_urlsafe(32) ~43 chars

    clear_setup_token()


def test_validate_setup_token_correct():
    """正确 Token 通过验证。"""
    clear_setup_token()
    generate_setup_token()
    token = get_setup_token()

    assert validate_setup_token(token) is True

    clear_setup_token()


def test_validate_setup_token_wrong():
    """错误 Token 不通过验证。"""
    clear_setup_token()
    generate_setup_token()

    assert validate_setup_token("wrong-token") is False
    assert validate_setup_token("") is False

    clear_setup_token()


def test_validate_setup_token_not_generated():
    """Token 未生成时验证始终返回 False。"""
    clear_setup_token()
    assert validate_setup_token("anything") is False


def test_has_valid_setup_cookie_valid():
    """有效 Cookie 返回 True。"""
    clear_setup_token()
    generate_setup_token()
    token = get_setup_token()
    cookie_str = f"{_COOKIE_NAME}={token}"
    scope = _make_scope(cookie_str)

    assert _has_valid_setup_cookie(scope) is True

    clear_setup_token()


def test_has_valid_setup_cookie_invalid():
    """无效 Cookie 返回 False。"""
    clear_setup_token()
    generate_setup_token()
    scope = _make_scope(f"{_COOKIE_NAME}=invalid-value")

    assert _has_valid_setup_cookie(scope) is False

    clear_setup_token()


def test_has_valid_setup_cookie_missing():
    """无 Cookie 头返回 False。"""
    clear_setup_token()
    generate_setup_token()
    scope = _make_scope("")

    assert _has_valid_setup_cookie(scope) is False

    clear_setup_token()


def test_has_valid_setup_cookie_no_token():
    """Token 未生成时始终返回 False。"""
    clear_setup_token()
    scope = _make_scope(f"{_COOKIE_NAME}=whatever")

    assert _has_valid_setup_cookie(scope) is False
