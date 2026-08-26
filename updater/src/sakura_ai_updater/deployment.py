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


_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$"
)
_LATEST_RE = re.compile(r"^(?P<prefix>.+):latest$")
_IMAGE_DIGEST_RE = re.compile(r"^.+@(?P<digest>sha256:[0-9a-fA-F]{64})$")
_REGISTRY_DIGEST_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")


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


def _registry_image_ref(image_ref: str) -> tuple[str, str]:
    """Return ``(repository, repository:tag)`` for an explicit registry ref.

    Docker's ``.Image`` field is a local content-addressed image ID.  It is
    not a registry repository and must never be interpolated into an
    ``image@sha256:...`` reference.  Requiring an explicit registry host also
    makes local-only names (``sakura-ai:latest``) fail closed.
    """

    if "@" in image_ref:
        image_ref, digest = image_ref.rsplit("@", 1)
        if not _REGISTRY_DIGEST_RE.fullmatch(digest):
            raise DeploymentError(
                f"invalid registry digest in image reference: {image_ref!r}"
            )
        # A digest-only ref has no tag and cannot prove the RepoTags identity
        # required for safe capture.  Keep the updater's repository/tag model
        # explicit and fail closed rather than guessing a tag.
        if ":" not in image_ref.rsplit("/", 1)[-1]:
            raise DeploymentError(
                f"digest-pinned image must include an explicit tag: {image_ref!r}"
            )
    repository_and_tag = image_ref.rsplit(":", 1)
    if len(repository_and_tag) != 2 or not all(repository_and_tag):
        raise DeploymentError(
            f"image reference is missing an explicit registry tag: {image_ref!r}"
        )
    repository, tag = repository_and_tag
    normalized_repository = _registry_repository(repository)
    return normalized_repository, f"{normalized_repository}:{tag.lower()}"


def _registry_repository(repository: str) -> str:
    """Validate and normalize a registry repository without requiring a tag."""

    first_component = repository.split("/", 1)[0]
    if "/" not in repository or not (
        "." in first_component
        or ":" in first_component
        or first_component == "localhost"
    ):
        raise DeploymentError(
            f"local or unqualified image cannot be materialized: {repository!r}"
        )
    return repository.lower()


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

    def sandbox_image_refs(self) -> dict[str, str | None]:
        """Return the persisted immutable sandbox pair, if configured.

        Development updates deliberately use this pair as-is because the
        development channel has no stable sandbox manifest to borrow.
        """

        values = self._env()
        return {
            "sandboxd_image": values.get("SAKURA_SANDBOXD_IMAGE_DIGEST") or None,
            "runner_image": values.get("SAKURA_AGENT_RUNNER_IMAGE_DIGEST") or None,
        }

    def read_deploy_mode(self) -> str | None:
        """Read deployment mode from deployment.env, then process environment."""

        return self._env().get("SAKURA_DEPLOY_MODE") or os.environ.get(
            "SAKURA_DEPLOY_MODE"
        )

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
        return status, payload if isinstance(payload, dict) else {}

    async def _health_payload(self, timeout: float = 5.0) -> dict[str, Any] | None:
        status, payload = await asyncio.to_thread(
            self._read_health_sync, self.health_url, timeout
        )
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

    async def resolve_current_build(self) -> dict[str, Any]:
        """Return health build identity when the running app exposes it."""

        payload = await self._health_payload()
        build = payload.get("build") if isinstance(payload, dict) else None
        return dict(build) if isinstance(build, dict) else {}

    async def capture_from_image(self) -> str | None:
        """Capture the current authoritative image ref before activation."""

        return self.read_image_ref()

    async def _run_docker_command(self, argv: list[str]) -> tuple[str, str]:
        """Run a Docker inspection command with bounded cancellation cleanup."""

        process = None
        communication_task: asyncio.Task[tuple[bytes, bytes]] | None = None
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
            if process is not None:
                await _terminate_and_reap(
                    process, communication_task=communication_task
                )
            raise
        except TimeoutError as exc:
            if process is not None:
                await _terminate_and_reap(
                    process, communication_task=communication_task
                )
            raise DeploymentError(f"docker command timed out for {argv!r}") from exc
        except OSError as exc:
            raise DeploymentError(
                f"cannot run Docker inspection {argv!r}: {exc}"
            ) from exc
        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()
            raise DeploymentError(
                f"docker inspection failed for {argv!r}: {detail or process.returncode}"
            )
        return stdout.decode(errors="replace"), stderr.decode(errors="replace")

    async def _run_inspect(self) -> str:
        stdout, _ = await self._run_docker_command(
            ["docker", "inspect", "--format={{.Image}}", self.web_container]
        )
        image_id = stdout.strip()
        if not image_id:
            raise DeploymentError(
                f"docker inspect returned no image ID for {self.web_container!r}"
            )
        return image_id

    async def _inspect_image_metadata(self, image_id: str) -> dict[str, Any]:
        """Read registry tags/digests for a local image ID."""

        stdout, _ = await self._run_docker_command(
            ["docker", "image", "inspect", "--format={{json .}}", image_id]
        )
        try:
            metadata = json.loads(stdout)
        except (TypeError, ValueError) as exc:
            raise DeploymentError("docker image inspect returned invalid JSON") from exc
        if isinstance(metadata, list):
            if len(metadata) != 1:
                raise DeploymentError(
                    "docker image inspect returned ambiguous metadata"
                )
            metadata = metadata[0]
        if not isinstance(metadata, dict):
            raise DeploymentError("docker image inspect returned no image metadata")
        return metadata

    @staticmethod
    def _select_registry_digest(
        metadata: dict[str, Any],
        *,
        expected_repository: str,
        expected_tag: str,
        expected_digest: str | None = None,
    ) -> str:
        """Select the registry manifest digest for the deployment image.

        A digest-pinned deployment is already named by immutable repository and
        digest identity. Docker is allowed to omit the source tag from
        ``RepoTags`` after pulling ``tag@digest``, so pinned refs are proven by
        an exact matching ``RepoDigests`` entry. Mutable tag refs still require
        both the expected ``RepoTags`` entry and one unambiguous repository
        digest; they fail closed if either proof is missing.
        """
        repo_digests = metadata.get("RepoDigests")
        if not isinstance(repo_digests, list):
            raise DeploymentError("docker image metadata has no RepoDigests")
        candidates: set[str] = set()
        for entry in repo_digests:
            if not isinstance(entry, str) or "@" not in entry:
                continue
            repository, digest = entry.rsplit("@", 1)
            if repository.strip().lower() != expected_repository.lower():
                continue
            if not _REGISTRY_DIGEST_RE.fullmatch(digest):
                raise DeploymentError(
                    f"invalid registry digest for {expected_repository!r}: {digest!r}"
                )
            candidates.add(digest.lower())

        if expected_digest is not None:
            normalized_expected = expected_digest.lower()
            if not _REGISTRY_DIGEST_RE.fullmatch(normalized_expected):
                raise DeploymentError(
                    f"invalid pinned deployment digest: {expected_digest!r}"
                )
            if normalized_expected not in candidates:
                raise DeploymentError(
                    f"running repository digests do not match pinned deployment "
                    f"digest {normalized_expected!r}"
                )
            return normalized_expected

        tags = metadata.get("RepoTags")
        if not isinstance(tags, list):
            raise DeploymentError("docker image metadata has no RepoTags")
        normalized_tags = {
            str(tag).strip().lower() for tag in tags if isinstance(tag, str)
        }
        if expected_tag.lower() not in normalized_tags:
            raise DeploymentError(
                f"running image is not tagged as the deployment image {expected_tag!r}"
            )

        if len(candidates) != 1:
            if not candidates:
                raise DeploymentError(
                    f"no RepoDigests entry matches repository {expected_repository!r}"
                )
            raise DeploymentError(
                f"multiple RepoDigests entries match repository {expected_repository!r}"
            )
        return candidates.pop()

    async def capture_from_digest(self) -> str:
        """Capture the registry manifest digest of the running image.

        The container ``.Image`` value is only a local image ID.  Resolve it
        through ``docker image inspect`` and select the unique RepoDigests entry
        matching the repository/tag in deployment.env instead of treating the
        local ID as a registry digest.
        """

        image_ref = self.read_image_ref()
        if not image_ref:
            raise DeploymentError("SAKURA_AI_IMAGE is missing from deployment.env")
        expected_repository, expected_tag = _registry_image_ref(image_ref)
        expected_digest = (
            image_ref.rsplit("@", 1)[1].lower() if "@" in image_ref else None
        )
        image_id = await self._run_inspect()
        if not _REGISTRY_DIGEST_RE.fullmatch(image_id):
            raise DeploymentError(
                f"running container returned an invalid local image ID: {image_id!r}"
            )
        metadata = await self._inspect_image_metadata(image_id)
        running_digest = self._select_registry_digest(
            metadata,
            expected_repository=expected_repository,
            expected_tag=expected_tag,
            expected_digest=expected_digest,
        )
        return running_digest

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
        if "@" in image_ref:
            if _IMAGE_DIGEST_RE.fullmatch(image_ref):
                _registry_image_ref(image_ref)
                return image_ref
            raise DeploymentError(
                f"invalid digest-pinned image reference: {image_ref!r}"
            )
        latest = _LATEST_RE.fullmatch(image_ref)
        if latest is None:
            return image_ref
        repository, _ = _registry_image_ref(image_ref)
        current_version = await self.resolve_current_version()
        if current_version is None:
            raise DeploymentError("cannot materialize :latest without /health.version")
        digest = await self.capture_from_digest()
        concrete = f"{repository}:v{current_version}@{digest}"
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
        build = await self.resolve_current_build()
        return {
            "current_version": await self.resolve_current_version(),
            "current_channel": build.get("channel"),
            "current_revision": build.get("revision"),
            "current_image": image,
            "from_image": image,
            "from_digest": running_digest,
            "deployment_mode": self.read_deploy_mode(),
            "running_container_digest": running_digest,
            **self.sandbox_image_refs(),
        }
