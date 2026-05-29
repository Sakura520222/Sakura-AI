"""WebUI 安全行为测试。

覆盖 ``_safe_redirect_path`` 的所有安全分支：
  - 空字符串 / falsy 值
  - 非 ``/`` 开头的相对路径
  - 协议相对 URL（``//host/path``）
  - 含 ``://`` 的绝对 URL
"""

from backend.webui import deps


def test_toast_redirect_rejects_protocol_relative_url():
    """协议相对 URL（//host/path）必须回退到站内根路径。"""
    response = deps.toast_redirect("//evil.com/path", message="ok")

    assert response.headers["location"].startswith("/?")
    assert "//evil.com" not in response.headers["location"]


def test_toast_redirect_rejects_absolute_url():
    """绝对 URL（http://evil.com）必须回退到站内根路径。"""
    response = deps.toast_redirect("http://evil.com/path", message="ok")

    assert response.headers["location"].startswith("/?")
    assert "evil.com" not in response.headers["location"]


def test_toast_redirect_rejects_https_url():
    """HTTPS 绝对 URL 必须回退到站内根路径。"""
    response = deps.toast_redirect("https://evil.com/path", message="ok")

    assert response.headers["location"].startswith("/?")
    assert "evil.com" not in response.headers["location"]


def test_toast_redirect_rejects_empty_string():
    """空字符串应回退到站内根路径。"""
    response = deps.toast_redirect("", message="ok")

    assert response.headers["location"].startswith("/?")


def test_toast_redirect_rejects_non_slash_start():
    """非 ``/`` 开头的路径必须回退到站内根路径。"""
    response = deps.toast_redirect("evil.com/path", message="ok")

    assert response.headers["location"].startswith("/?")
    assert "evil.com" not in response.headers["location"]


def test_toast_redirect_accepts_valid_path():
    """合法站内路径应被保留，仅追加 toast 参数。"""
    response = deps.toast_redirect("/dashboard", message="ok")

    assert response.headers["location"] == "/dashboard?_toast=ok&_toast_type=success"
