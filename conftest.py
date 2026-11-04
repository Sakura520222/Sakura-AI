"""Capabilities shared by the backend and independent package test suites."""

import errno

import pytest


@pytest.fixture
def symlink_support(tmp_path):
    """Skip link-dependent cases only when the current account cannot create links."""
    target = tmp_path / ".symlink-probe-target"
    link = tmp_path / ".symlink-probe-link"
    target.mkdir()
    try:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314 or exc.errno in {
                errno.EPERM, errno.ENOSYS, errno.ENOTSUP,
            }:
                pytest.skip("Symbolic links require Windows Developer Mode or an account with link privileges")
            raise
    finally:
        if link.is_symlink():
            link.unlink()
        target.rmdir()
