"""WebUI 安全行为测试。"""

from backend.webui import deps


def test_toast_redirect_rejects_protocol_relative_url():
    """协议相对 URL（//host/path）必须回退到站内根路径。"""
    response = deps.toast_redirect("//evil.com/path", message="ok")

    assert response.headers["location"].startswith("/?")
    assert "//evil.com" not in response.headers["location"]
