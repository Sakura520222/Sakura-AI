"""Setup Token 管理函数单元测试。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.core import bootstrap
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


def test_private_handoff_rotates_removes_and_never_logs_token(tmp_path):
    import os

    from loguru import logger

    messages = []
    sink = logger.add(lambda message: messages.append(str(message)))
    try:
        generate_setup_token()
        first = get_setup_token()
        path = tmp_path / ".setup-token"
        assert path.read_text(encoding="utf-8").strip() == first
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600
        generate_setup_token()
        second = get_setup_token()
        assert first != second
        assert path.read_text(encoding="utf-8").strip() == second
        assert first not in "".join(messages)
        assert second not in "".join(messages)
        assert not validate_setup_token(first)
        clear_setup_token()
        assert not path.exists()
    finally:
        logger.remove(sink)


def test_cleanup_failure_revokes_token_without_failing_completed_setup(
    monkeypatch, tmp_path
):
    from loguru import logger

    generate_setup_token()
    token = get_setup_token()
    path = tmp_path / ".setup-token"
    original_unlink = Path.unlink

    def unlink(file, *args, **kwargs):
        if file == path:
            raise PermissionError("synthetic scanner holds the token file")
        return original_unlink(file, *args, **kwargs)

    messages = []
    sink = logger.add(lambda message: messages.append(message.record))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "unlink", unlink)
            bootstrap.mark_setup_completed("sqlite:///setup-test.db")
            clear_setup_token()
        assert bootstrap.check_setup_state() == "completed"
        assert not validate_setup_token(token) and get_setup_token() is None
        assert path.exists() and bootstrap._setup_token_file is None
        assert any(record["level"].name == "WARNING" for record in messages)
        assert token not in str(messages)
    finally:
        logger.remove(sink)
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_setup_completion_with_locked_token_still_schedules_restart(
    monkeypatch, tmp_path
):
    import asyncio

    from backend.core.setup_service import SetupService
    from backend.webui.routes import setup

    generate_setup_token()
    token = get_setup_token()
    path = tmp_path / ".setup-token"
    service = SetupService()
    service.init_database = AsyncMock()
    service.save_configs_to_db = AsyncMock()
    service.create_admin_user = AsyncMock()
    service.trigger_restart = Mock()
    monkeypatch.setattr(setup, "setup_service", service)
    values = {
        "DATABASE_URL": "mysql+aiomysql://test:test@localhost/setup_test",
        "ADMIN_GITHUB_USERNAME": "setup-test-admin",
        "GITHUB_OAUTH_CLIENT_ID": "synthetic-client",
        "GITHUB_OAUTH_CLIENT_SECRET": "synthetic-secret",
        "GITHUB_OAUTH_REDIRECT_URI": "http://localhost/auth/callback",
    }
    request = SimpleNamespace(json=AsyncMock(return_value=values))
    tasks = []
    original_unlink = Path.unlink

    def unlink(file, *args, **kwargs):
        if file == path:
            raise PermissionError("synthetic file lock")
        return original_unlink(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        patch.setattr(asyncio, "create_task", lambda task: tasks.append(task))
        response = await setup.complete_setup(request)
    try:
        assert response.status_code == 200
        assert json.loads(response.body)["success"] is True
        service.save_configs_to_db.assert_awaited_once()
        service.create_admin_user.assert_awaited_once()
        assert bootstrap.check_setup_state() == "completed"
        assert not validate_setup_token(token)
        assert len(tasks) == 1
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "sleep", AsyncMock())
            await tasks[0]
        service.trigger_restart.assert_called_once()
    finally:
        for task in tasks:
            task.close()
        path.unlink(missing_ok=True)


@pytest.mark.skipif(os.name != "nt", reason="native Windows file security")
def test_windows_token_is_private_before_write_and_after_rotation(
    monkeypatch, tmp_path
):
    import ctypes
    import msvcrt
    import subprocess
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.GetSecurityInfo.restype = wintypes.DWORD
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p

    def descriptor(fd):
        security = ctypes.c_void_p()
        result = advapi.GetSecurityInfo(
            msvcrt.get_osfhandle(fd),
            1,
            5,
            None,
            None,
            None,
            None,
            ctypes.byref(security),
        )
        assert result == 0
        sddl = wintypes.LPWSTR()
        try:
            assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                security, 1, 5, ctypes.byref(sddl), None
            )
            return sddl.value
        finally:
            kernel.LocalFree(sddl)
            kernel.LocalFree(security)

    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    import csv

    sid = next(csv.reader([identity.strip()]))[1]
    expected = f"O:{sid}D:P(A;;FA;;;{sid})"
    observed = []
    original_fdopen = os.fdopen

    def fdopen(fd, *args, **kwargs):
        # Observe the actual kernel DACL before any credential bytes are written.
        assert os.fstat(fd).st_size == 0
        assert not os.get_inheritable(fd)
        observed.append(descriptor(fd))
        return original_fdopen(fd, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fdopen", fdopen)
        for _ in range(2):
            generate_setup_token()
            with (tmp_path / ".setup-token").open("rb") as stream:
                assert descriptor(stream.fileno()) == expected
            assert (tmp_path / ".setup-token").read_text().strip() == get_setup_token()
    assert observed == [expected, expected]


@pytest.mark.parametrize("stage", ["create", "fdopen", "fsync", "replace"])
def test_failed_token_rotation_preserves_old_credential_and_cleans_files(
    monkeypatch, tmp_path, stage
):
    generate_setup_token()
    old_token = get_setup_token()
    failure = OSError(f"synthetic {stage} failure")
    created = []
    original_create = bootstrap._create_setup_token_tempfile

    def create(directory):
        if stage == "create":
            raise failure
        fd, name = original_create(directory)
        created.append((fd, name))
        return fd, name

    def fail(*args, **kwargs):
        raise failure

    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "_create_setup_token_tempfile", create)
        patch.setattr(
            bootstrap.secrets, "token_urlsafe", lambda size: "synthetic-next-token"
        )
        if stage != "create":
            patch.setattr(bootstrap.os, stage, fail)
        with pytest.raises(OSError) as caught:
            generate_setup_token()
    assert caught.value is failure
    assert get_setup_token() == old_token and validate_setup_token(old_token)
    assert not validate_setup_token("synthetic-next-token")
    assert (tmp_path / ".setup-token").read_text().strip() == old_token
    assert not list(tmp_path.glob(".setup-token-*"))
    for fd, _ in created:
        with pytest.raises(OSError):
            os.fstat(fd)


def test_token_handoff_respects_custom_connection_directory(monkeypatch, tmp_path):
    config = tmp_path / "custom config" / "connection.dev.json"
    monkeypatch.setenv("SAKURA_CONNECTION_CONFIG_PATH", str(config))
    generate_setup_token()
    token_path = config.parent / ".setup-token"
    assert token_path.read_text().strip() == get_setup_token()
    assert not (tmp_path / ".setup-token").exists()
    clear_setup_token()
    assert not token_path.exists()


def test_token_rotation_replaces_destination_symlink_without_following(tmp_path):
    target = tmp_path / "unrelated-file.txt"
    target.write_text("preserve this content", encoding="utf-8")
    token_path = tmp_path / ".setup-token"
    try:
        token_path.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    generate_setup_token()
    assert not token_path.is_symlink()
    assert token_path.read_text().strip() == get_setup_token()
    assert target.read_text(encoding="utf-8") == "preserve this content"


@pytest.mark.skipif(os.name != "nt", reason="native Windows file security")
@pytest.mark.parametrize(
    "stage", ["sid", "descriptor", "volume", "acl_less", "transfer"]
)
def test_windows_security_failure_never_publishes_token(monkeypatch, tmp_path, stage):
    import ctypes

    from backend.core import windows_private_file as native

    failure = OSError("synthetic native security failure")

    def fail(*args, **kwargs):
        raise failure

    def unavailable(*args):
        ctypes.set_last_error(50)  # ERROR_NOT_SUPPORTED
        return False

    def acl_less(handle, name, length, serial, maximum, flags, fs_name, fs_length):
        flags._obj.value = 0
        return True

    with monkeypatch.context() as patch:
        if stage == "sid":
            patch.setattr(native, "_process_user_sid", fail)
        elif stage == "descriptor":
            patch.setattr(
                native._advapi32,
                "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                unavailable,
            )
        elif stage in {"volume", "acl_less"}:
            patch.setattr(
                native._kernel32,
                "GetVolumeInformationByHandleW",
                unavailable if stage == "volume" else acl_less,
            )
        else:
            patch.setattr(native.msvcrt, "open_osfhandle", fail)
        with pytest.raises(OSError):
            generate_setup_token()
    assert get_setup_token() is None
    assert not (tmp_path / ".setup-token").exists()
    assert not list(tmp_path.glob(".setup-token-*"))


@pytest.mark.skipif(os.name != "nt", reason="native Windows sharing violation")
def test_native_windows_lock_does_not_undo_setup_completion(tmp_path):
    import ctypes

    from backend.core import windows_private_file as native

    generate_setup_token()
    token = get_setup_token()
    path = tmp_path / ".setup-token"
    handle = native._kernel32.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        bootstrap.mark_setup_completed(
            "mysql+aiomysql://test:test@localhost/setup_test"
        )
        assert bootstrap.check_setup_state() == "completed"
        assert not validate_setup_token(token)
        assert path.exists()
    finally:
        native._kernel32.CloseHandle(handle)
        path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def private_setup_directory(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "SAKURA_CONNECTION_CONFIG_PATH", str(tmp_path / "connection.json")
    )
    monkeypatch.setattr("backend.core.bootstrap._setup_token_file", None)
    yield
    clear_setup_token()
