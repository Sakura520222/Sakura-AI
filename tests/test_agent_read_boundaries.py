"""Ordinary workspace reads preserve semantics without following replaced paths."""

import os

import pytest

from backend.services.agent_team.tools.base import ToolContext
from backend.services.agent_team.tools.file_state import ReadFileState
from backend.services.agent_team.tools.registry import create_executor
from backend.services.agent_team.workspace_service import AgentTeamWorkspaceService


def context(workspace):
    return ToolContext(
        str(workspace),
        AgentTeamWorkspaceService(workspace),
        extra={"file_state": ReadFileState()},
    )


@pytest.mark.asyncio
async def test_read_file_does_not_disclose_host_hardlink(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = tmp_path / "host-file"
    secret.write_text("HOST PRIVATE CONTENT")
    os.link(secret, workspace / "file.txt")
    result = await create_executor(read_only=True).execute_raw(
        "read_file", {"file_path": "file.txt"}, context(workspace)
    )
    assert not result.success and "HOST PRIVATE CONTENT" not in str(result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_directory", [False, True])
async def test_read_file_rejects_link_replacement_after_path_resolution(
    tmp_path, monkeypatch, replace_directory
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    directory = workspace / "nested"
    directory.mkdir()
    target = directory / "file.txt"
    target.write_text("workspace content")
    outside = tmp_path / "host-directory"
    outside.mkdir()
    secret = outside / "file.txt"
    secret.write_text("HOST PRIVATE CONTENT")
    ctx = context(workspace)
    resolve = ctx.workspace_service.resolve_inside_workspace

    def replace_after_resolution(root, relative="."):
        resolved = resolve(root, relative)
        if relative == "nested/file.txt":
            if replace_directory:
                directory.rename(workspace / "old-nested")
                directory.symlink_to(outside, target_is_directory=True)
            else:
                target.unlink()
                target.symlink_to(secret)
        return resolved

    monkeypatch.setattr(
        ctx.workspace_service, "resolve_inside_workspace", replace_after_resolution
    )
    result = await create_executor(read_only=True).execute_raw(
        "read_file", {"file_path": "nested/file.txt"}, ctx
    )
    assert not result.success and "HOST PRIVATE CONTENT" not in str(result.output)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_path", [".env.example", ".sakura/notes.md", "config/connection.json"]
)
async def test_ordinary_read_does_not_adopt_repository_instruction_file_filters(
    tmp_path, file_path
):
    target = tmp_path / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ordinary repository content")
    result = await create_executor(read_only=True).execute_raw(
        "read_file", {"file_path": file_path}, context(tmp_path)
    )
    assert result.success and "ordinary repository content" in result.output["content"]


@pytest.mark.asyncio
async def test_safe_internal_symlink_and_line_range_keep_read_state_and_edit_semantics(
    tmp_path,
):
    target = tmp_path / "real.txt"
    target.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
    (tmp_path / "alias.txt").symlink_to(target)
    ctx = context(tmp_path)
    executor = create_executor()
    result = await executor.execute_raw(
        "read_file", {"file_path": "alias.txt", "start_line": 2, "end_line": 2}, ctx
    )
    assert result.success and result.output["content"] == "     2\tbeta"
    entry = ctx.extra["file_state"].get(target)
    assert not entry.is_full_read and entry.start_line == entry.end_line == 2
    assert (
        entry.content == "alpha\nbeta\ngamma\n"
        and entry.mtime == target.stat().st_mtime
    )
    edit = await executor.execute_raw(
        "edit_file",
        {"file_path": "alias.txt", "old_text": "beta", "new_text": "updated"},
        ctx,
    )
    assert edit.success and target.read_bytes() == b"alpha\r\nupdated\r\ngamma\r\n"


@pytest.mark.asyncio
async def test_read_keeps_utf16_and_binary_image_behavior(tmp_path):
    text = tmp_path / "utf16.txt"
    text.write_bytes(b"\xff\xfe" + "first\r\nsecond".encode("utf-16-le"))
    png = tmp_path / "image.png"
    pixels = b"\x89PNG\r\n\x1a\n\x00\xff"
    png.write_bytes(pixels)
    ctx = context(tmp_path)
    executor = create_executor(read_only=True)
    result = await executor.execute_raw(
        "read_file", {"file_path": "utf16.txt", "start_line": 2}, ctx
    )
    assert result.success and result.output["content"] == "     2\tsecond"
    image = await executor.execute_raw("read_file", {"file_path": "image.png"}, ctx)
    assert not image.success and png.read_bytes() == pixels
    assert ctx.extra["file_state"].get(png) is None


def test_secure_workspace_read_rejects_fifo_without_blocking(tmp_path):
    from backend.services.agent_team.tools.file_utils import (
        read_workspace_text_with_metadata,
    )

    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="regular"):
        read_workspace_text_with_metadata(tmp_path, fifo)
