"""Host UDS entrypoint for sandboxd; no updater lifecycle is imported."""

from __future__ import annotations

import os
import socket
import stat
from contextlib import suppress
from pathlib import Path

from .app import create_app
from .config import SandboxdConfig, canonicalize_socket_path
from .runtime_factory import create_runtime

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _assert_no_link_components(path: Path) -> None:
    """Reject symlink/reparse parents before creating the UDS directory."""

    current = Path(path.anchor or path.root or ".")
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        if _is_link_or_reparse(current):
            raise RuntimeError("sandbox socket root contains a symlink or reparse point")


def validate_socket_filesystem(config: SandboxdConfig) -> tuple[Path, Path]:
    """Validate lexical and filesystem ownership boundaries before bind.

    UID/GID ownership and mode ``0660`` are deployment responsibilities for the
    next lifecycle slice.  This function intentionally only prevents path
    aliasing and unsafe replacement; it never follows or removes an existing
    socket.
    """

    socket_path = Path(canonicalize_socket_path(config.socket_path))
    root_path = Path(canonicalize_socket_path(config.socket_root or socket_path.parent))
    try:
        socket_path.relative_to(root_path)
    except ValueError as exc:
        raise RuntimeError("sandbox socket is outside its configured root") from exc
    _assert_no_link_components(root_path)
    _assert_no_link_components(socket_path.parent)
    if socket_path.exists() or socket_path.is_symlink():
        if _is_link_or_reparse(socket_path) or not stat.S_ISSOCK(os.lstat(socket_path).st_mode):
            raise RuntimeError("sandbox socket path already exists and is not a socket")
        raise RuntimeError("sandbox socket path is already occupied")
    return root_path, socket_path


def _bind_secure_socket(config: SandboxdConfig, socket_path: Path) -> socket.socket:
    """Bind the UDS before uvicorn starts accepting requests.

    Binding first avoids a window in which a newly-created socket has the
    process umask's permissions or the wrong group.  Ownership failures are
    fatal rather than silently widening access.
    """

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chown(str(socket_path), config.socket_owner, config.socket_group)
        os.chmod(str(socket_path), config.socket_mode)
        listener.listen(128)
        return listener
    except BaseException:
        listener.close()
        with suppress(FileNotFoundError):
            socket_path.unlink()
        raise


def serve(config: SandboxdConfig) -> None:
    """Run the daemon on its dedicated Unix socket through uvicorn."""

    import uvicorn

    # Construct the real runtime before creating the listener.  Missing
    # Docker configuration is a fatal startup error; it must never produce a
    # healthy socket backed by ``UnavailableRuntimeAdapter``.
    runtime = create_runtime(config)
    app = create_app(config, runtime=runtime)
    root_path, socket_path = validate_socket_filesystem(config)
    root_path.mkdir(parents=True, exist_ok=True)
    _assert_no_link_components(root_path)
    listener = _bind_secure_socket(config, socket_path)
    try:
        # Pass the already-bound descriptor so uvicorn never recreates the
        # socket with an uncontrolled umask/ownership.
        uvicorn.run(app, fd=listener.fileno(), log_level="info")
    finally:
        listener.close()
        with suppress(FileNotFoundError):
            socket_path.unlink()


__all__ = ["serve", "validate_socket_filesystem"]
