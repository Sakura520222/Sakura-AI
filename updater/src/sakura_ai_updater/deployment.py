"""Authoritative host deployment state for image-mode updates."""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sakura_ai_updater.adapters.image import _terminate_and_reap, write_deployment_env


class DeploymentError(RuntimeError):
    """The current deployment cannot be read or materialized safely."""


_SEMVER_RE = re.compile(r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")
_LATEST_RE = re.compile(r"^(?P<prefix>.+):latest(?:@(?P<digest>sha256:[0-9a-fA-F]+))?$")


def _parse_version(value: str) -> tuple[int, int, int] | None:
    match = _SEMVER_RE.fullmatch(value)
    if not match:
        return None
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))  # type: ignore[return-value]


def _read_env_file(path: str) -> dict[str, str]:
    """Read simple KEY=VALUE deployment env syntax without shell evaluation."""

    result: dict[str, str] = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key:
            result[key] = value.strip().strip("\"'")
    return result


class DeploymentStateProvider:
    """Read deployment.env and running-container facts on every request.

    No value is cached: the updater daemon survives Web container restarts,
    and deployment.env remains the authoritative source for the selected image.
    """

    def __init__(
        self,
        deployment_env: str,
        web_container: str = "sakura-ai",
        health_url: str = "http://localhost:8000/health",
        command_timeout: float = 30.0,
    ) -> None:
        self.deployment_env = deployment_env
        self.web_container = web_container
        self.health_url = health_url
        self.command_timeout = command_timeout

    def _env(self) -> dict[str, str]:
        return _read_env_file(self.deployment_env)

    def read_image_ref(self) -> str | None:
        """Return the authoritative ``SAKURA_AI_IMAGE`` value."""

        return self._env().get("SAKURA_AI_IMAGE") or None

    def read_deploy_mode(self) -> str | None:
        """Read deployment mode from deployment.env, then process environment."""

        return self._env().get("SAKURA_DEPLOY_MODE") or os.environ.get("SAKURA_DEPLOY_MODE")

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
        return status, payload if isinstance(payload, dict) else {}

    async def _health_payload(self, timeout: float = 5.0) -> dict[str, Any] | None:
        status, payload = await asyncio.to_thread(self._read_health_sync, self.health_url, timeout)
        if status != 200:
            return None
        return payload

    async def resolve_current_version(self) -> str | None:
        """Resolve the final application version from the host ``/health`` endpoint.

        The image tag is deployment metadata, not proof of the version serving
        requests (a stale container can continue serving after an env change).
        Therefore concrete tags and ``:latest`` both use the same host health
        authority; an unavailable/malformed endpoint fails closed with ``None``.
        """

        image_ref = self.read_image_ref()
        if not image_ref:
            return None
        payload = await self._health_payload()
        version = payload.get("version") if payload else None
        return version if isinstance(version, str) and _parse_version(version) else None

    async def capture_from_image(self) -> str | None:
        """Capture the current authoritative image ref before activation."""

        return self.read_image_ref()

    async def _run_inspect(self) -> str:
        argv = ["docker", "inspect", "--format={{.Image}}", self.web_container]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communication_task = asyncio.create_task(process.communicate())
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication_task), timeout=self.command_timeout
            )
        except asyncio.CancelledError:
            await _terminate_and_reap(
                process,
                communication_task=locals().get("communication_task"),
            )
            raise
        except TimeoutError as exc:
            await _terminate_and_reap(
                process,
                communication_task=locals().get("communication_task"),
            )
            raise DeploymentError(
                f"docker inspect timed out for {self.web_container!r}"
            ) from exc
        except OSError as exc:
            raise DeploymentError(f"cannot inspect running container {self.web_container!r}: {exc}") from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise DeploymentError(
                f"docker inspect failed for {self.web_container!r}: {detail or process.returncode}"
            )
        digest = stdout.decode(errors="replace").strip()
        if not digest:
            raise DeploymentError(f"docker inspect returned no digest for {self.web_container!r}")
        return digest

    async def capture_from_digest(self) -> str:
        """Capture immutable image identity from the running ``sakura-ai`` container."""

        return await self._run_inspect()

    async def materialize_current_anchor(self) -> str:
        """Pin a mutable ``:latest`` deployment to its running tag and digest.

        This method is intentionally not called by read-only check/preflight
        paths.  The orchestrator invokes it only after a destructive preflight
        has passed and immediately before downloading a target image.
        """

        image_ref = self.read_image_ref()
        if image_ref is None:
            raise DeploymentError("SAKURA_AI_IMAGE is missing from deployment.env")
        # Any image ref that already carries an immutable digest is authoritative,
        # including ``:latest@sha256:...``.  Do not re-query host health/docker or
        # rewrite deployment.env for an already pinned ref.
        if "@sha256:" in image_ref:
            return image_ref
        latest = _LATEST_RE.fullmatch(image_ref)
        if latest is None:
            return image_ref
        current_version = await self.resolve_current_version()
        if current_version is None:
            raise DeploymentError("cannot materialize :latest without /health.version")
        digest = await self.capture_from_digest()
        if not digest.startswith("sha256:"):
            raise DeploymentError(f"running image digest is not sha256: {digest!r}")
        concrete = f"{latest.group('prefix')}:v{current_version}@{digest}"
        await asyncio.to_thread(write_deployment_env, self.deployment_env, concrete)
        return concrete

    async def disk_space_sufficient(self, threshold: int) -> tuple[bool, int | None]:
        """Return whether the deployment filesystem has enough free bytes."""

        import shutil

        directory = str(Path(self.deployment_env).parent)
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, directory)
        except OSError:
            return False, None
        return usage.free >= threshold, usage.free

    async def current_state(self) -> dict[str, Any]:
        """Convenience projection used by status/readiness integrations."""

        image = self.read_image_ref()
        running_digest = await self.capture_from_digest()
        return {
            "current_version": await self.resolve_current_version(),
            "current_image": image,
            "from_image": image,
            "from_digest": running_digest,
            "deployment_mode": self.read_deploy_mode(),
            "running_container_digest": running_digest,
        }
