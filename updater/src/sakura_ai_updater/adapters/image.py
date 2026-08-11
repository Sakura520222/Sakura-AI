"""Asynchronous Docker/Compose primitives for image-mode updates.

All external commands are executed as an argv vector through
``asyncio.create_subprocess_exec``.  In particular, this module deliberately
does not use ``shell=True`` or a synchronous ``subprocess`` helper.  A failed
pull therefore cannot mutate ``deployment.env``; the file is changed only by
``activate`` after the image has been downloaded.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ImageAdapterError(RuntimeError):
    """Base class for adapter failures."""


class ImageCommandError(ImageAdapterError):
    """A Docker/Compose process failed, timed out, or was cancelled."""

    def __init__(
        self,
        message: str,
        *,
        argv: Iterable[str] = (),
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        error_code: str = "command_failed",
    ) -> None:
        super().__init__(message)
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error_code = error_code


class ImagePreflightError(ImageCommandError):
    """The requested image does not have a reachable registry manifest."""


class HealthCheckError(ImageAdapterError):
    """The application health endpoint returned an unusable response."""

    def __init__(self, message: str, *, error_code: str = "health_check_failed") -> None:
        super().__init__(message)
        self.error_code = error_code


class HealthCheckTimeout(HealthCheckError):
    """The health endpoint did not become ready before the deadline."""

    def __init__(self, message: str = "health check timed out") -> None:
        super().__init__(message, error_code="health_check_timeout")


class HealthCheckVersionMismatch(HealthCheckError):
    """The endpoint stayed healthy but reported a different application version."""

    def __init__(self, expected: str, actual: Any) -> None:
        super().__init__(
            f"health endpoint reported version {actual!r}; expected {expected!r}",
            error_code="health_check_version_mismatch",
        )
        self.expected = expected
        self.actual = actual


def _fsync_parent(path: str | os.PathLike[str]) -> None:
    """Durably persist an atomic rename on POSIX filesystems."""

    if os.name != "posix":
        return
    directory = os.path.dirname(os.fspath(path)) or "."
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            # Some filesystems (notably a few tmpfs variants) do not support
            # directory fsync.  The rename is still atomic in that case.
            pass
    finally:
        os.close(fd)


def _replace_image_line(lines: list[str], image: str) -> list[str]:
    """Replace or append ``SAKURA_AI_IMAGE`` while preserving other lines."""

    newline = "\n"
    if lines and lines[-1].endswith("\r\n"):
        newline = "\r\n"
    elif lines and lines[-1].endswith("\n"):
        newline = "\n"
    replacement = f"SAKURA_AI_IMAGE={image}{newline}"
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key == "SAKURA_AI_IMAGE":
            lines[index] = replacement
            return lines
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] = f"{lines[-1]}{newline}"
    lines.append(replacement)
    return lines


def write_deployment_env(path: str, image: str) -> None:
    """Atomically update ``SAKURA_AI_IMAGE`` in a deployment env file.

    The temp file is created in the destination directory, fsynced before
    rename, and the parent directory is fsynced afterwards.  Existing content
    (including comments and unrelated variables) is retained verbatim.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = destination.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    lines = content.splitlines(keepends=True)
    updated = "".join(_replace_image_line(lines, image))
    fd, temporary = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        _fsync_parent(destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# More explicit alias used by callers that want to document the durability
# requirement at the call site.
atomic_update_deployment_env = write_deployment_env


async def _terminate_and_reap(
    process: Any,
    *,
    communication_task: asyncio.Task[Any] | None = None,
    timeout: float = 1.0,
) -> None:
    """Kill a child and await its pipes/process so cancellation cannot leak it."""

    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass

    communicate = getattr(process, "communicate", None)
    if communication_task is None and communicate is not None:
        try:
            result = communicate()
            if inspect.isawaitable(result):
                communication_task = asyncio.create_task(result)
                await asyncio.wait_for(asyncio.shield(communication_task), timeout=timeout)
        except BaseException:
            # A cancelled/blocked communicate still leaves ``wait`` as the
            # final process-reaping primitive.  Give both bounded chances.
            if communication_task is not None and not communication_task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(communication_task), timeout=timeout)
                except BaseException:
                    communication_task.cancel()
                    await asyncio.gather(communication_task, return_exceptions=True)
    wait = getattr(process, "wait", None)
    if wait is not None:
        try:
            result = wait()
            if inspect.isawaitable(result):
                await asyncio.wait_for(asyncio.shield(asyncio.create_task(result)), timeout=timeout)
        except BaseException:
            # The process was killed and every available reap primitive was
            # attempted.  Preserve the original command error/cancellation.
            pass


class ImageAdapter:
    """Image deployment primitives executed on the updater host."""

    def __init__(
        self,
        compose_file: str,
        deployment_env: str,
        web_container: str = "sakura-ai",
        health_url: str = "http://localhost:8000/health",
        health_timeout: float = 90.0,
        health_poll_interval: float = 2.0,
        command_timeout: float = 300.0,
    ) -> None:
        self.compose_file = compose_file
        self.deployment_env = deployment_env
        self.web_container = web_container
        self.health_url = health_url
        self.health_timeout = health_timeout
        self.health_poll_interval = health_poll_interval
        self.command_timeout = command_timeout

    async def _run_command(
        self,
        argv: list[str],
        *,
        timeout: float | None = None,
    ) -> tuple[str, str]:
        """Run one argv command without blocking the event loop."""

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            raise ImageCommandError(
                f"cannot start command {argv!r}: {exc}",
                argv=argv,
                error_code="command_unavailable",
            ) from exc

        try:
            communication_task = asyncio.create_task(process.communicate())
            raw_stdout, raw_stderr = await asyncio.wait_for(
                asyncio.shield(communication_task),
                timeout=self.command_timeout if timeout is None else timeout,
            )
        except TimeoutError as exc:
            await _terminate_and_reap(process)
            if "communication_task" in locals() and communication_task.done() and not communication_task.cancelled():
                raw_stdout, raw_stderr = communication_task.result()
            else:
                raw_stdout, raw_stderr = b"", b""
            raise ImageCommandError(
                f"command timed out: {argv!r}",
                argv=argv,
                returncode=process.returncode,
                stdout=raw_stdout.decode(errors="replace"),
                stderr=raw_stderr.decode(errors="replace"),
                error_code="command_timeout",
            ) from exc
        except asyncio.CancelledError:
            # Do not turn cancellation into a normal failed job.  Terminate
            # the child to avoid leaking Docker processes, then re-raise so
            # JobOrchestrator preserves its active gate.
            await _terminate_and_reap(process)
            raise

        stdout = raw_stdout.decode(errors="replace")
        stderr = raw_stderr.decode(errors="replace")
        if process.returncode != 0:
            raise ImageCommandError(
                f"command exited with status {process.returncode}: {argv!r}",
                argv=argv,
                returncode=process.returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return stdout, stderr

    async def preflight_image(self, target_image: str) -> None:
        """Verify that a target image manifest is available in its registry."""

        try:
            await self._run_command(["docker", "manifest", "inspect", target_image])
        except ImageCommandError as exc:
            raise ImagePreflightError(
                f"image manifest unavailable for {target_image!r}: {exc}",
                argv=exc.argv,
                returncode=exc.returncode,
                stdout=exc.stdout,
                stderr=exc.stderr,
                error_code="image_manifest_unavailable",
            ) from exc

    async def pull(self, target_image: str) -> None:
        """Download an image without changing the authoritative env file."""

        await self._run_command(["docker", "pull", target_image])

    async def activate(self, target_image: str) -> None:
        """Persist target image and ask Compose to recreate the service."""

        # This write is deliberately after ``pull`` (the orchestrator calls
        # those methods as separate state-machine steps).
        await asyncio.to_thread(write_deployment_env, self.deployment_env, target_image)
        await self._run_command(
            [
                "docker",
                "compose",
                "--env-file",
                self.deployment_env,
                "-f",
                self.compose_file,
                "up",
                "-d",
            ]
        )

    async def inspect_running_digest(self) -> str:
        """Return the immutable digest captured by the running container."""

        stdout, _ = await self._run_command(
            ["docker", "inspect", "--format={{.Image}}", self.web_container]
        )
        digest = stdout.strip()
        if not digest:
            raise ImageCommandError(
                f"docker inspect returned an empty digest for {self.web_container!r}",
                argv=["docker", "inspect", "--format={{.Image}}", self.web_container],
                error_code="digest_missing",
            )
        return digest

    @staticmethod
    def _read_health_sync(url: str, timeout: float) -> tuple[int, dict[str, Any]]:
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=timeout) as response:
                status = int(getattr(response, "status", response.getcode()))
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return int(exc.code), {}
        except (URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError):
            return 0, {}
        if not isinstance(payload, dict):
            return status, {}
        return status, payload

    async def health_check(self, target_version: str) -> None:
        """Poll ``/health`` until HTTP 200 reports the target application version."""

        deadline = time.monotonic() + max(0.0, self.health_timeout)
        mismatch: Any = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 0:
                if mismatch is not None:
                    raise HealthCheckVersionMismatch(target_version, mismatch)
                raise HealthCheckTimeout()
            request_timeout = max(0.05, min(remaining, 5.0))
            try:
                status, payload = await asyncio.wait_for(
                    asyncio.to_thread(self._read_health_sync, self.health_url, request_timeout),
                    timeout=request_timeout + 0.05,
                )
            except TimeoutError:
                status, payload = 0, {}
            if status == 200:
                actual = payload.get("version")
                if actual == target_version:
                    return
                mismatch = actual
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if mismatch is not None:
                    raise HealthCheckVersionMismatch(target_version, mismatch)
                raise HealthCheckTimeout()
            await asyncio.sleep(min(max(0.0, self.health_poll_interval), remaining))
