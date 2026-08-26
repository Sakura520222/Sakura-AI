from __future__ import annotations

import math
from pathlib import Path

import pytest
from sakura_ai_sandboxer.config import (
    MIN_VERSIONED_ENVELOPE_BYTES,
    SandboxdConfig,
    canonicalize_socket_path,
)
from sakura_ai_sandboxer.server import validate_socket_filesystem


@pytest.mark.parametrize(
    "value",
    [
        "relative/sandbox.sock",
        "/run/sakura-ai-sandbox/../sakura-ai-sandboxd.sock",
        "/run/./sakura-ai-sandbox/sandboxd.sock",
        "/run/sakura-ai/updater.sock",
        "/run/sakura-ai/alias/anything.sock",
    ],
)
def test_socket_path_rejects_relative_alias_and_updater_namespace(value):
    with pytest.raises(ValueError):
        canonicalize_socket_path(value)


def test_socket_root_is_an_independent_lexical_boundary(tmp_path: Path):
    root = tmp_path / "sandbox-root"
    socket_path = root / "sandboxd.sock"
    config = SandboxdConfig(socket_path=socket_path, socket_root=root)
    assert config.socket_path.endswith("/sandboxd.sock")
    assert config.socket_root.endswith("/sandbox-root")

    with pytest.raises(ValueError):
        SandboxdConfig(
            socket_path=tmp_path / "other-root" / "sandboxd.sock",
            socket_root=root,
        )


def test_server_rejects_existing_parent_symlink_or_reparse(monkeypatch, tmp_path: Path):
    root = tmp_path / "sandbox-root"
    root.mkdir()
    config = SandboxdConfig(socket_path=root / "sandboxd.sock", socket_root=root)

    from sakura_ai_sandboxer import server

    monkeypatch.setattr(server, "_is_link_or_reparse", lambda path: path == root)
    with pytest.raises(RuntimeError, match="symlink or reparse"):
        validate_socket_filesystem(config)


def test_daemon_config_rejects_non_finite_deadline_settings():
    with pytest.raises(ValueError, match="finite"):
        SandboxdConfig(timeout_seconds=math.inf)
    with pytest.raises(ValueError, match="finite"):
        SandboxdConfig(request_ledger_ttl_seconds=math.nan)


def test_daemon_config_rejects_response_budget_below_versioned_envelope():
    with pytest.raises(ValueError, match="minimum versioned envelope"):
        SandboxdConfig(max_output_bytes=1, max_response_bytes=1)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1.0, True, False])
def test_daemon_config_rejects_non_integer_byte_limits(value):
    with pytest.raises(ValueError):
        SandboxdConfig(max_output_bytes=value)
    with pytest.raises(ValueError):
        SandboxdConfig(max_output_bytes=1, max_response_bytes=value)


def test_daemon_config_accepts_integer_byte_limit_boundaries():
    config = SandboxdConfig(
        max_output_bytes=64 * 1024 * 1024,
        max_response_bytes=128 * 1024 * 1024,
    )
    assert config.max_output_bytes == 64 * 1024 * 1024
    assert config.max_response_bytes == 128 * 1024 * 1024
    minimum = SandboxdConfig(
        max_output_bytes=1,
        max_response_bytes=MIN_VERSIONED_ENVELOPE_BYTES,
    )
    assert minimum.max_response_bytes == MIN_VERSIONED_ENVELOPE_BYTES


def test_socket_permissions_are_independent_and_exact():
    config = SandboxdConfig()
    assert config.socket_owner == 0
    assert config.socket_group == 9473
    assert config.socket_group != 9472
    assert config.socket_mode == 0o660


def test_socket_permissions_cannot_reuse_updater_group_or_widen_mode():
    with pytest.raises(ValueError, match="independent"):
        SandboxdConfig(socket_group=9472)
    with pytest.raises(ValueError, match="exactly 0660"):
        SandboxdConfig(socket_mode=0o666)
