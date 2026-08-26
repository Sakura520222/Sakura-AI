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
import re
import stat
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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

    def __init__(
        self, message: str, *, error_code: str = "health_check_failed"
    ) -> None:
        super().__init__(message)
        self.error_code = error_code


class HealthCheckTimeout(HealthCheckError):
    """The health endpoint did not become ready before the deadline."""

    def __init__(self, message: str = "health check timed out") -> None:
        super().__init__(message, error_code="health_check_timeout")


@dataclass(frozen=True, slots=True)
class DeploymentSnapshot:
    """Exact deployment.env snapshot used by the image transaction rollback."""

    path: str
    content: bytes | None
    mode: int | None
    uid: int | None
    gid: int | None
    values: dict[str, str]


def _trusted_start_script(
    path: str | os.PathLike[str],
    project_root: str | os.PathLike[str],
    *,
    lstat: Callable[[str], Any] = os.lstat,
) -> Path:
    """Validate the production lifecycle entrypoint before executing it.

    The updater is a root daemon, so resolving ``start.sh`` is not sufficient:
    a writable parent directory could replace the file after a superficial
    ``is_file`` check.  Keep this check aligned with the daemon backend's
    production path policy: canonical, regular, root-owned, not shared-writable
    file, and the complete directory chain protected from group/other writes.

    ``lstat`` is injectable for Windows/static tests, where POSIX ownership
    fields are unavailable and production Linux inodes must be simulated.
    Source development mode deliberately bypasses this helper and only keeps
    the existing no-symlink/canonical check.
    """

    absolute = os.path.abspath(os.fspath(path))
    root = os.path.abspath(os.fspath(project_root))
    try:
        canonical = os.path.realpath(absolute)
        canonical_root = os.path.realpath(root)
        if canonical != absolute or canonical_root != root:
            raise ImageAdapterError(
                "production start.sh/project root must not contain symlinks"
            )
        if os.path.commonpath((canonical_root, canonical)) != canonical_root:
            raise ImageAdapterError(
                "production start.sh is outside the controlled project root"
            )
    except (OSError, ValueError) as exc:
        if isinstance(exc, ImageAdapterError):
            raise
        raise ImageAdapterError(
            "cannot resolve controlled production start.sh path"
        ) from exc

    try:
        file_stat = lstat(absolute)
    except OSError as exc:
        raise ImageAdapterError(
            f"unsafe production start.sh: cannot lstat {absolute!r}"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ImageAdapterError(
            f"unsafe production start.sh: {absolute!r} must be a regular file"
        )
    if getattr(file_stat, "st_uid", None) != 0:
        raise ImageAdapterError(
            f"unsafe production start.sh: {absolute!r} must be owned by root"
        )
    if stat.S_IMODE(file_stat.st_mode) & 0o022:
        raise ImageAdapterError(
            f"unsafe production start.sh: {absolute!r} must not be group/other writable"
        )

    directory = os.path.dirname(absolute)
    while True:
        try:
            directory_stat = lstat(directory)
        except OSError as exc:
            raise ImageAdapterError(
                f"unsafe production start.sh parent: cannot lstat {directory!r}"
            ) from exc
        if not stat.S_ISDIR(directory_stat.st_mode):
            raise ImageAdapterError(
                f"unsafe production start.sh parent: {directory!r} must be a directory"
            )
        if getattr(directory_stat, "st_uid", None) != 0:
            raise ImageAdapterError(
                f"unsafe production start.sh parent: {directory!r} must be owned by root"
            )
        if stat.S_IMODE(directory_stat.st_mode) & 0o022:
            raise ImageAdapterError(
                f"unsafe production start.sh parent: {directory!r} must not be group/other writable"
            )
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent
    return Path(absolute)


class HealthCheckVersionMismatch(HealthCheckError):
    """The endpoint stayed healthy but reported a different application version."""

    def __init__(self, expected: str, actual: Any) -> None:
        super().__init__(
            f"health endpoint reported version {actual!r}; expected {expected!r}",
            error_code="health_check_version_mismatch",
        )
        self.expected = expected
        self.actual = actual


_COMPOSE_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_PRODUCTION_COMPOSE_PROJECT = "sakura-ai"
_SANDBOXD_REPOSITORY = "ghcr.io/sakura520222/sakura-ai-sandboxd"
_RUNNER_REPOSITORY = "ghcr.io/sakura520222/sakura-ai-agent-runner"
# A failed managed uninstall gets the same single retry budget used by the
# updater's bounded Docker pull recovery. Each attempt is individually capped
# by ``command_timeout``; rollback never loops until success.
_SANDBOX_UNINSTALL_RETRIES = 1


def _read_compose_project_name(path: str) -> str:
    """Read a persisted Compose project without evaluating the env file."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise ImageAdapterError(
            f"cannot read deployment state for Compose project: {path!r}"
        ) from exc
    project: str | None = None
    for line in lines:
        if line.startswith("COMPOSE_PROJECT_NAME="):
            project = line.split("=", 1)[1]
    if project is None:
        raise ImageAdapterError("missing COMPOSE_PROJECT_NAME in deployment state")
    if not _COMPOSE_PROJECT_RE.fullmatch(project):
        raise ImageAdapterError(
            f"invalid COMPOSE_PROJECT_NAME in deployment state: {project!r}"
        )
    if project != _PRODUCTION_COMPOSE_PROJECT:
        raise ImageAdapterError(
            "unsupported COMPOSE_PROJECT_NAME in deployment state: "
            f"{project!r}; expected {_PRODUCTION_COMPOSE_PROJECT!r}"
        )
    return project


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


def _replace_env_lines(lines: list[str], values: Mapping[str, str]) -> list[str]:
    """Replace or append deployment keys while preserving unrelated lines."""

    newline = "\n"
    if lines and lines[-1].endswith("\r\n"):
        newline = "\r\n"
    elif lines and lines[-1].endswith("\n"):
        newline = "\n"
    pending = dict(values)
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key in pending:
            lines[index] = f"{key}={pending.pop(key)}{newline}"
    if pending:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] = f"{lines[-1]}{newline}"
        lines.extend(f"{key}={value}{newline}" for key, value in pending.items())
    return lines


def _read_env_values(path: str) -> dict[str, str]:
    """Read simple KEY=VALUE lines without shell evaluation."""

    try:
        content = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    values: dict[str, str] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip():
            values[key.strip()] = value.strip().strip("\"'")
    return values


def _atomic_write_content(
    destination: Path,
    content: bytes,
    *,
    mode: int | None = None,
    uid: int | None = None,
    gid: int | None = None,
) -> None:
    """Atomically write exact content and preserve supplied file identity."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        if uid is not None and gid is not None and hasattr(os, "chown"):
            try:
                os.chown(temporary, uid, gid)
            except PermissionError:
                # A non-root updater cannot restore ownership it does not own;
                # never silently change a file that was owned by somebody else.
                try:
                    current = os.stat(destination)
                except FileNotFoundError:
                    current = None
                if current is None or current.st_uid != uid or current.st_gid != gid:
                    raise
        os.replace(temporary, destination)
        _fsync_parent(destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def capture_deployment_snapshot(path: str) -> DeploymentSnapshot:
    """Capture bytes, mode, owner and parsed values before a transaction."""

    destination = Path(path)
    try:
        stat = destination.stat()
        content = destination.read_bytes()
    except FileNotFoundError:
        return DeploymentSnapshot(path, None, None, None, None, {})
    return DeploymentSnapshot(
        path=str(destination),
        content=content,
        mode=stat.st_mode & 0o7777,
        uid=getattr(stat, "st_uid", None),
        gid=getattr(stat, "st_gid", None),
        values=_read_env_values(path),
    )


def restore_deployment_snapshot(snapshot: DeploymentSnapshot) -> None:
    """Restore an exact snapshot, including mode/owner when available."""

    destination = Path(snapshot.path)
    if snapshot.content is None:
        try:
            destination.unlink()
        except FileNotFoundError:
            return
        return
    _atomic_write_content(
        destination,
        snapshot.content,
        mode=snapshot.mode,
        uid=snapshot.uid,
        gid=snapshot.gid,
    )


def write_deployment_env_values(path: str, values: Mapping[str, str]) -> None:
    """Atomically update selected deployment keys preserving all other state.

    The temp file is created in the destination directory, fsynced before
    rename, and the parent directory is fsynced afterwards. Existing content
    (including comments and unrelated variables) is retained verbatim.
    """

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        content = destination.read_text(encoding="utf-8")
    except FileNotFoundError:
        content = ""
    lines = content.splitlines(keepends=True)
    updated = "".join(_replace_env_lines(lines, values)).encode("utf-8")
    try:
        stat = destination.stat()
    except FileNotFoundError:
        stat = None
    _atomic_write_content(
        destination,
        updated,
        mode=(stat.st_mode & 0o7777) if stat is not None else None,
        uid=getattr(stat, "st_uid", None) if stat is not None else None,
        gid=getattr(stat, "st_gid", None) if stat is not None else None,
    )


def write_deployment_env(path: str, image: str) -> None:
    """Compatibility wrapper updating only ``SAKURA_AI_IMAGE``."""

    write_deployment_env_values(path, {"SAKURA_AI_IMAGE": image})


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
    except ProcessLookupError, OSError:
        pass

    communicate = getattr(process, "communicate", None)
    if communication_task is None and communicate is not None:
        try:
            result = communicate()
            if inspect.isawaitable(result):
                communication_task = asyncio.create_task(result)
                await asyncio.wait_for(
                    asyncio.shield(communication_task), timeout=timeout
                )
        except BaseException:
            # A cancelled/blocked communicate still leaves ``wait`` as the
            # final process-reaping primitive.  Give both bounded chances.
            if communication_task is not None and not communication_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(communication_task), timeout=timeout
                    )
                except BaseException:
                    communication_task.cancel()
                    await asyncio.gather(communication_task, return_exceptions=True)
    wait = getattr(process, "wait", None)
    if wait is not None:
        try:
            result = wait()
            if inspect.isawaitable(result):
                await asyncio.wait_for(
                    asyncio.shield(asyncio.create_task(result)), timeout=timeout
                )
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
        command_timeout: float = 600.0,
        *,
        production: bool = False,
        trusted_lstat: Callable[[str], Any] | None = None,
    ) -> None:
        self.compose_file = compose_file
        self.deployment_env = deployment_env
        self.web_container = web_container
        self.health_url = health_url
        self.health_timeout = health_timeout
        self.health_poll_interval = health_poll_interval
        self.command_timeout = command_timeout
        # The direct adapter API remains source/dev-friendly for callers and
        # tests.  The real daemon factory passes ``production=True`` unless
        # ``SAKURA_UPDATER_DEV=1`` is explicitly selected.
        self.production = production
        self._trusted_lstat = trusted_lstat or os.lstat
        self._last_snapshot: DeploymentSnapshot | None = None
        self._new_sandbox_install_attempted = False
        self.activation_rollback_completed = False

    def _project_start_script(self) -> Path:
        """Derive and validate the repository-owned lifecycle entrypoint."""

        compose = Path(self.compose_file)
        try:
            compose = compose.resolve(strict=False)
        except OSError as exc:
            raise ImageAdapterError("cannot resolve the production compose path") from exc
        project_root = compose.parent.parent if compose.parent.name == "docker" else compose.parent
        start_script = project_root / "start.sh"
        try:
            resolved = start_script.resolve(strict=True)
        except OSError as exc:
            raise ImageAdapterError("controlled start.sh is missing") from exc
        if resolved != start_script or not resolved.is_file():
            raise ImageAdapterError("controlled start.sh must not be a symlink")
        if self.production:
            return _trusted_start_script(
                str(resolved),
                str(project_root),
                lstat=self._trusted_lstat,
            )
        return resolved

    @staticmethod
    def _sandbox_ref_parts(ref: str, label: str) -> tuple[str, str]:
        if not isinstance(ref, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}@sha256:[0-9a-f]{64}", ref
        ):
            raise ImageAdapterError(f"{label} is not a complete immutable image ref")
        repository, digest = ref.rsplit("@", 1)
        return repository, digest

    async def capture_snapshot(self) -> DeploymentSnapshot:
        """Capture deployment.env before changing any image identity."""

        snapshot = await asyncio.to_thread(capture_deployment_snapshot, self.deployment_env)
        self._last_snapshot = snapshot
        return snapshot

    async def _run_compose_up(self) -> None:
        compose_argv = ["docker", "compose", "--env-file", self.deployment_env]
        project = await asyncio.to_thread(_read_compose_project_name, self.deployment_env)
        compose_argv.extend(["--project-name", project, "-f", self.compose_file, "up", "-d"])
        await self._run_command(compose_argv)

    async def _run_sandboxd_reinstall(self) -> None:
        start_script = self._project_start_script()
        await self._run_command(["bash", str(start_script), "sandboxd", "reinstall"])

    @staticmethod
    def _snapshot_sandbox_refs(snapshot: DeploymentSnapshot) -> tuple[str | None, str | None]:
        sandboxd = snapshot.values.get("SAKURA_SANDBOXD_IMAGE_DIGEST")
        runner = snapshot.values.get("SAKURA_AGENT_RUNNER_IMAGE_DIGEST")
        return sandboxd or None, runner or None

    def _validate_snapshot_sandbox_refs(
        self, snapshot: DeploymentSnapshot
    ) -> tuple[str | None, str | None]:
        """Reject partial/unsafe legacy state before the first env write."""

        sandboxd_ref, runner_ref = self._snapshot_sandbox_refs(snapshot)
        if (sandboxd_ref is None) != (runner_ref is None):
            raise ImageAdapterError(
                "deployment snapshot has only one sandbox image digest"
            )
        if sandboxd_ref is None:
            return None, None
        sandboxd_repository, _ = self._sandbox_ref_parts(
            sandboxd_ref, "sandboxd snapshot"
        )
        runner_repository, _ = self._sandbox_ref_parts(runner_ref or "", "runner snapshot")
        if (
            sandboxd_repository != _SANDBOXD_REPOSITORY
            or runner_repository != _RUNNER_REPOSITORY
        ):
            raise ImageAdapterError("deployment snapshot sandbox repository is not trusted")
        return sandboxd_ref, runner_ref

    async def _run_sandboxd_uninstall(self) -> None:
        start_script = self._project_start_script()
        await self._run_command(["bash", str(start_script), "sandboxd", "uninstall"])

    async def _run_sandboxd_uninstall_with_retry(self) -> None:
        """Remove a newly installed legacy sidecar with one bounded retry."""

        attempts = _SANDBOX_UNINSTALL_RETRIES + 1
        for attempt in range(attempts):
            try:
                await self._run_sandboxd_uninstall()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt + 1 >= attempts:
                    raise ImageAdapterError(
                        "rollback incomplete: sandboxd uninstall failed after "
                        f"{attempts} bounded attempts: {exc}"
                    ) from exc

    async def _restore_and_reconverge(
        self,
        snapshot: DeploymentSnapshot,
        *,
        remove_new_sandbox: bool = False,
    ) -> None:
        """Restore old env and converge the old sidecar before Web Compose.

        A legacy deployment has no old sandbox pair.  If the new sidecar was
        already installed before a later activation step failed, explicitly
        uninstall that managed instance before bringing the old Web stack back.
        Legacy rollback must prove that the new sidecar is gone before allowing
        the old Web Compose stack to start. A failed uninstall stops rollback
        immediately and leaves Web stopped for a safe retry.
        """

        await asyncio.to_thread(restore_deployment_snapshot, snapshot)
        sandboxd_ref, _ = self._validate_snapshot_sandbox_refs(snapshot)
        if sandboxd_ref is not None:
            await self._run_sandboxd_reinstall()
        elif remove_new_sandbox:
            await self._run_sandboxd_uninstall_with_retry()
        await self._run_compose_up()

    async def rollback(self, snapshot: DeploymentSnapshot | None = None) -> None:
        """Restore old env and converge the old sidecar before Web Compose."""

        selected = snapshot or self._last_snapshot
        if selected is None:
            raise ImageAdapterError("no deployment snapshot is available for rollback")
        await self._restore_and_reconverge(
            selected,
            remove_new_sandbox=self._new_sandbox_install_attempted,
        )

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
            if (
                "communication_task" in locals()
                and communication_task.done()
                and not communication_task.cancelled()
            ):
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

    async def activate(
        self,
        target_image: str,
        sandboxd_image: str | None = None,
        runner_image: str | None = None,
    ) -> None:
        """Atomically activate Web plus sandboxd and runner identities.

        The deployment file is written only after pull/preflight have passed.
        For a production transaction, sandboxd is reconciled through the
        repository-owned ``start.sh sandboxd reinstall`` argv before Compose
        is allowed to recreate Web. A failure restores the exact old file and
        converges the old sidecar before retrying the old Web configuration.
        """

        if (sandboxd_image is None) != (runner_image is None):
            raise ImageAdapterError("sandboxd and runner refs must be supplied together")
        snapshot = await self.capture_snapshot()
        self._new_sandbox_install_attempted = False
        # Validate the old state before changing deployment.env.  A single
        # persisted digest cannot be safely rolled back and is never treated as
        # a legacy bootstrap state.
        self._validate_snapshot_sandbox_refs(snapshot)
        # Validate the repository-owned lifecycle entrypoint before changing
        # deployment.env as well as immediately before execution.  This keeps
        # an unsafe production path a true preflight failure with zero writes.
        if sandboxd_image is not None:
            self._project_start_script()
        values: dict[str, str] = {"SAKURA_AI_IMAGE": target_image}
        if sandboxd_image is not None and runner_image is not None:
            sandboxd_repository, _ = self._sandbox_ref_parts(sandboxd_image, "sandboxd image")
            runner_repository, _ = self._sandbox_ref_parts(runner_image, "runner image")
            if sandboxd_repository != _SANDBOXD_REPOSITORY:
                raise ImageAdapterError("sandboxd image repository is not trusted")
            if runner_repository != _RUNNER_REPOSITORY:
                raise ImageAdapterError("runner image repository is not trusted")
            values.update(
                {
                    "SAKURA_SANDBOXD_IMAGE": sandboxd_repository,
                    "SAKURA_SANDBOXD_IMAGE_DIGEST": sandboxd_image,
                    "SAKURA_AGENT_RUNNER_IMAGE": runner_repository,
                    "SAKURA_AGENT_RUNNER_IMAGE_DIGEST": runner_image,
                }
            )
            release_match = re.search(r":v(\d+\.\d+\.\d+)(?:@|$)", target_image)
            if release_match is not None:
                values["SAKURA_SANDBOX_RELEASE_VERSION"] = release_match.group(1)
        try:
            self.activation_rollback_completed = False
            self._new_sandbox_install_attempted = False
            await asyncio.to_thread(write_deployment_env_values, self.deployment_env, values)
            if sandboxd_image is not None:
                # Mark before invoking the controlled lifecycle command: a
                # command can create the managed sidecar and then fail its
                # readiness probe.  Legacy rollback must remove that sidecar.
                self._new_sandbox_install_attempted = True
                await self._run_sandboxd_reinstall()
            await self._run_compose_up()
        except Exception:
            try:
                await self._restore_and_reconverge(
                    snapshot,
                    remove_new_sandbox=self._new_sandbox_install_attempted,
                )
                self.activation_rollback_completed = True
            except Exception as rollback_exc:
                raise ImageAdapterError(
                    f"activation failed; rollback incomplete: {rollback_exc}"
                ) from rollback_exc
            raise

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
        except URLError, OSError, TimeoutError, ValueError, json.JSONDecodeError:
            return 0, {}
        if not isinstance(payload, dict):
            return status, {}
        return status, payload

    async def health_check(self, target_version: str | dict[str, Any]) -> None:
        """Poll ``/health`` until the expected version/build identity is live.

        The string form remains the stable v1 compatibility API.  Development
        updates pass ``{version, channel, revision}`` and require all three
        fields to match.
        """

        if isinstance(target_version, dict):
            expected_version = target_version.get("version")
            expected_channel = target_version.get("channel")
            expected_revision = target_version.get("revision")
        else:
            expected_version = target_version
            expected_channel = expected_revision = None

        deadline = time.monotonic() + max(0.0, self.health_timeout)
        mismatch: Any = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 0:
                if mismatch is not None:
                    raise HealthCheckVersionMismatch(str(expected_version), mismatch)
                raise HealthCheckTimeout()
            request_timeout = max(0.05, min(remaining, 5.0))
            try:
                status, payload = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._read_health_sync, self.health_url, request_timeout
                    ),
                    timeout=request_timeout + 0.05,
                )
            except TimeoutError:
                status, payload = 0, {}
            if status == 200:
                actual = payload.get("version")
                build = (
                    payload.get("build")
                    if isinstance(payload.get("build"), dict)
                    else {}
                )
                identity_ok = actual == expected_version
                if expected_channel is not None:
                    identity_ok = (
                        identity_ok and build.get("channel") == expected_channel
                    )
                if expected_revision is not None:
                    identity_ok = (
                        identity_ok and build.get("revision") == expected_revision
                    )
                if identity_ok:
                    return
                mismatch = {
                    "version": actual,
                    "channel": build.get("channel"),
                    "revision": build.get("revision"),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if mismatch is not None:
                    raise HealthCheckVersionMismatch(str(expected_version), mismatch)
                raise HealthCheckTimeout()
            await asyncio.sleep(min(max(0.0, self.health_poll_interval), remaining))


# The explicit name is useful at integration seams while preserving the
# historical ``ImageAdapter`` import used by the updater daemon.
ImageDeploymentAdapter = ImageAdapter
