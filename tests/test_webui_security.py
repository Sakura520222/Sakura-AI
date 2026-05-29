"""WebUI 安全行为测试。"""

from backend.webui import deps


def test_toast_redirect_rejects_protocol_relative_url():
    """协议相对 URL（//host/path）必须回退到站内根路径。"""
    malicious_url = "/" + "/evil.com/path"

    response = deps.toast_redirect(malicious_url, message="ok")

    assert response.headers["location"].startswith("/?")
    assert response.headers["location"] != f"{malicious_url}?_toast=ok&_toast_type=success"
