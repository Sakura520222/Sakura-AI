"""build_version_info pure-function coverage (Slice 1).

build_version_info 接收明确的 deploy_mode 参数，不读取任何环境变量——
真正的纯函数，route 层负责从 Settings 读后传参。
"""

from backend import __version__
from backend.webui.routes.version import build_version_info


def test_image_mode_marks_updater_not_connected():
    info = build_version_info("image")
    assert info["current_version"] == __version__
    assert info["deployment_type"] == "image"
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "updater_not_connected"
    assert info["update_available"] is None
    assert info["latest_version"] is None


def test_source_mode_marks_updater_not_available():
    info = build_version_info("source")
    assert info["deployment_type"] == "source"
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "source_updater_not_available"


def test_explicit_unknown_mode():
    info = build_version_info("unknown")
    assert info["deployment_type"] == "unknown"
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "unknown_deployment"


def test_invalid_mode_normalized_to_unknown():
    info = build_version_info("garbage")
    assert info["deployment_type"] == "unknown"
    assert info["update_unsupported_reason"] == "unknown_deployment"


def test_empty_string_normalized_to_unknown():
    info = build_version_info("")
    assert info["deployment_type"] == "unknown"
