"""build_version_info pure-function coverage (Slice 1).

build_version_info 接收明确的 deploy_mode 参数，不读取任何环境变量——
真正的纯函数，route 层负责从 Settings 读后传参。
"""

from backend import __version__
from backend.services.update_checker import is_newer_version
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


def test_with_update_info_when_update_available():
    # 测试环境 __version__=3.0.0 < latest 3.1.0 → derived True
    info = build_version_info(
        "image",
        update_info={
            "latest_version": "3.1.0",
            "update_available": True,
            "last_checked": "2026-08-07T10:00:00Z",
            "check_error": None,
        },
    )
    assert info["latest_version"] == "3.1.0"
    assert info["update_available"] is True
    assert info["last_checked"] == "2026-08-07T10:00:00Z"


def test_update_available_derived_not_cached_bool():
    # 陈旧缓存说 update_available=true，但 latest == 当前 __version__ → derive False
    info = build_version_info(
        "image",
        update_info={
            "latest_version": __version__,  # == 当前进程版本
            "update_available": True,  # 陈旧缓存布尔值
            "last_checked": "2026-08-07T10:00:00Z",
            "check_error": None,
        },
    )
    assert info["update_available"] is False


def test_with_update_info_none_keeps_nulls():
    info = build_version_info("source")
    assert info["update_available"] is None
    assert info["latest_version"] is None
    assert info["last_checked"] is None


def test_with_check_error_no_latest():
    # 有缓存数据但 latest=None（失败且无 last-known-good）→ False 而非 None
    info = build_version_info(
        "image",
        update_info={
            "latest_version": None,
            "update_available": False,
            "last_checked": "2026-08-07T10:00:00Z",
            "check_error": "timeout",
        },
    )
    assert info["update_available"] is False
    assert info["check_error"] == "timeout"


def test_is_newer_version_public_helper():
    # 公开比较函数可被 Web 层复用（derived state 的单一真相源）
    assert is_newer_version("3.0.0", "3.1.0") is True
    assert is_newer_version("3.1.0", "3.1.0") is False
