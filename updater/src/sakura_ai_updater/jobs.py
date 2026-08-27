"""P0 update job orchestration and durable state transitions."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import uuid
from collections.abc import Mapping
from typing import Any

from sakura_ai_updater import PROTOCOL_VERSION
from sakura_ai_updater.deployment import DeploymentError, DeploymentStateProvider
from sakura_ai_updater.job_logs import JobLogStore
from sakura_ai_updater.registry import (
    DevelopmentTarget,
    RegistryClient,
    RegistryTargetError,
    StableTarget,
    parse_development_target,
    parse_stable_target,
)
from sakura_ai_updater.release_client import (
    ManifestNotFoundError,
    ReleaseClient,
    ReleaseClientError,
    SandboxManifest,
    parse_sandbox_manifest,
)
from sakura_ai_updater.state import (
    JobState,
    UpdateStateStore,
    load_state,
    save_state,
)
from sakura_ai_updater.time import now_rfc3339

DISK_SPACE_THRESHOLD = 2 * 1024 * 1024 * 1024
P0_STATES = frozenset(
    {
        "idle",
        "checking",
        "update_available",
        "preflight",
        "downloading",
        "activating",
        "restarting",
        "health_checking",
        "success",
        "failed",
    }
)


def _utcnow() -> str:
    return now_rfc3339()


class UpdateOrchestrationError(RuntimeError):
    """Base class for action/preflight failures."""


class UpdateInProgressError(UpdateOrchestrationError):
    """Another destructive update owns the active gate."""

    def __init__(self, job_id: str) -> None:
        super().__init__(f"update job {job_id!r} is already in progress")
        self.job_id = job_id


class UpdaterMaintenanceError(UpdateOrchestrationError):
    """The daemon is quiesced for a verified lifecycle operation."""


class TargetNotFoundError(UpdateOrchestrationError):
    """The requested release/manifest does not exist."""


class PreflightFailedError(UpdateOrchestrationError):
    """A valid manifest failed one or more readiness gates."""

    def __init__(
        self, checks: list[dict[str, Any]], result: dict[str, Any] | None = None
    ) -> None:
        self.checks = checks
        self.result = result or {"can_update": False, "checks": checks}
        super().__init__("update preflight failed")


class ManifestInvalidError(UpdateOrchestrationError):
    """Manifest was missing or did not satisfy schema/protocol constraints."""


def _value(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _updater_value(manifest: Any, name: str, default: Any = None) -> Any:
    updater = _value(manifest, "updater", {})
    return _value(updater, name, default)


def _version_tuple(version: str | None) -> tuple[int, int, int] | None:
    if not isinstance(version, str):
        return None
    try:
        from sakura_ai_updater.semver import parse_semver

        parsed = parse_semver(version)
        if isinstance(parsed, tuple):
            return parsed
        if hasattr(parsed, "major"):
            return int(parsed.major), int(parsed.minor), int(parsed.patch)
    except ImportError, ValueError, TypeError:
        pass
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _is_newer(target: str | None, current: str | None) -> bool:
    target_tuple = _version_tuple(target)
    current_tuple = _version_tuple(current)
    return (
        target_tuple is not None
        and current_tuple is not None
        and target_tuple > current_tuple
    )


def _development(value: Any) -> DevelopmentTarget | None:
    if isinstance(value, DevelopmentTarget):
        return value
    if isinstance(value, dict) and value.get("channel") == "development":
        return parse_development_target(value)
    return None


def _stable(value: Any) -> StableTarget | None:
    if isinstance(value, StableTarget):
        return value
    if isinstance(value, dict) and value.get("channel") == "stable":
        return parse_stable_target(value)
    return None


def _sandbox_ref(value: Any, name: str) -> str | None:
    """Read one full immutable sandbox ref from a manifest/state object."""

    candidate = _value(value, name)
    if candidate is None:
        return None
    return candidate if isinstance(candidate, str) and candidate else None


_SANDBOX_REF_RE = re.compile(
    r"^(?:ghcr\.io/sakura520222/sakura-ai-sandboxd|"
    r"ghcr\.io/sakura520222/sakura-ai-agent-runner)@sha256:[0-9a-f]{64}$"
)


class JobOrchestrator:
    """Coordinate check, preflight, and one asynchronous destructive update."""

    def __init__(
        self,
        state_path: str,
        adapter: Any,
        release_client: ReleaseClient,
        deployment: DeploymentStateProvider,
        disk_space_threshold: int = DISK_SPACE_THRESHOLD,
        *,
        log_capacity: int = 200,
    ) -> None:
        self.state_path = state_path
        self.adapter = adapter
        self.release_client = release_client
        self.deployment = deployment
        self.disk_space_threshold = disk_space_threshold
        self._lock = asyncio.Lock()
        self._accepting_updates = True
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._logs = JobLogStore(log_capacity)
        # ``/v1/status`` is intentionally a cheap, synchronous projection.  Keep
        # the most recent read-only check/preflight result here so status callers
        # can display host readiness without triggering another health, Docker,
        # or release-network request.  A daemon restart starts with no snapshot.
        self.readiness_snapshot: dict[str, Any] | None = None

    def _load(self) -> UpdateStateStore:
        return load_state(self.state_path)

    def _persist(self, store: UpdateStateStore) -> None:
        save_state(self.state_path, store)

    @staticmethod
    def _check(name: str, passed: bool, detail: str | None = None) -> dict[str, Any]:
        item: dict[str, Any] = {"name": name, "passed": bool(passed)}
        if detail is not None:
            item["detail"] = detail
        return item

    @staticmethod
    def _readiness_from_checks(checks: list[dict[str, Any]]) -> dict[str, bool]:
        """Project preflight checks into the stable readiness fields."""

        values = {item["name"]: bool(item["passed"]) for item in checks}
        return {
            "manifest_found": values.get("manifest_found", False),
            "manifest_valid": values.get("manifest_valid", False),
            "image_pullable": values.get("image_manifest_exists", False),
            "protocol_compatible": values.get("protocol_compatible", False),
            "min_upgrade_from_satisfied": values.get("min_upgrade_from", False),
            "updater_asset_present": values.get("updater_asset_present", False),
            # Older release clients expose the asset and checksum gate as one
            # boolean.  Prefer a dedicated check when available, while retaining
            # that compatibility for existing P0 clients/tests.
            "sha256sums_present": values.get(
                "sha256sums_present", values.get("updater_asset_present", False)
            ),
            "target_newer": values.get("target_newer", False),
            "deployment_mode_image": values.get("deployment_mode_image", False),
        }

    def _remember_readiness(
        self,
        *,
        can_update: bool,
        checks: list[dict[str, Any]],
        target_version: str,
        target_image: str,
        channel: Any = None,
        target_extra: dict[str, Any] | None = None,
    ) -> None:
        """Store a copy-only projection used by the synchronous status route."""

        target: dict[str, Any] = {
            "version": target_version,
            "image": target_image,
        }
        if channel is not None:
            target["channel"] = channel
        if target_extra:
            target.update(target_extra)
        self.readiness_snapshot = {
            "update_ready": bool(can_update),
            "readiness": self._readiness_from_checks(checks),
            "target": target,
        }

    async def _manifest(self, target_version: str | None) -> Any:
        method = getattr(self.release_client, "fetch_manifest", None)
        if method is None:
            method = getattr(self.release_client, "get_manifest", None)
        if method is None:
            raise ManifestInvalidError(
                "release client does not provide a manifest method"
            )
        try:
            manifest = await method(target_version)
        except ManifestNotFoundError:
            raise
        except ReleaseClientError:
            raise
        except Exception as exc:
            raise ManifestInvalidError(str(exc)) from exc
        if manifest is None:
            raise TargetNotFoundError(target_version or "latest")
        return manifest

    async def _sandbox_manifest(self, target_version: str) -> SandboxManifest:
        """Fetch the independent same-release sandbox manifest fail-closed."""

        method = getattr(self.release_client, "fetch_sandbox_manifest", None)
        if method is None:
            method = getattr(self.release_client, "get_sandbox_manifest", None)
        if method is None:
            raise ManifestInvalidError(
                "release client does not provide the stable sandbox manifest"
            )
        try:
            value = method(target_version)
            value = await value if inspect.isawaitable(value) else value
            if isinstance(value, SandboxManifest):
                value = {
                    "schema_version": value.schema_version,
                    "manifest": value.manifest,
                    "version": value.version,
                    "channel": value.channel,
                    "sandboxd_image": value.sandboxd_image,
                    "runner_image": value.runner_image,
                }
            return parse_sandbox_manifest(value, expected_version=target_version)
        except ReleaseClientError as exc:
            raise ManifestInvalidError(str(exc)) from exc
        except Exception as exc:
            raise ManifestInvalidError(str(exc)) from exc

    async def _resolve_stable_target(
        self,
        version: str,
        *,
        supplied: StableTarget | None = None,
    ) -> StableTarget:
        """Resolve every stable request to the current digest-pinned target.

        A version string or a caller-created dataclass is not an image
        identity.  The release client must prove that the requested tag is a
        published stable release and the registry must independently prove
        the tag/latest digest relationship.  There is intentionally no
        manifest-image or tag-only fallback here.
        """

        method = getattr(self.release_client, "resolve_stable_target", None)
        if method is None:
            raise ManifestInvalidError(
                "release client cannot resolve a stable digest-pinned target"
            )
        try:
            value = method(
                version,
                expected_digest=supplied.digest if supplied is not None else None,
            )
            value = await value if inspect.isawaitable(value) else value
            resolved = (
                value
                if isinstance(value, StableTarget)
                else _stable(value)
            )
        except RegistryTargetError:
            raise
        except ReleaseClientError as exc:
            raise ManifestInvalidError(str(exc)) from exc
        except Exception as exc:
            raise ManifestInvalidError(str(exc)) from exc
        if resolved is None:
            raise ManifestInvalidError(
                "stable resolver returned no digest-pinned target"
            )
        if resolved.version != version or resolved.tag != f"v{version}":
            raise RegistryTargetError(
                "stable resolver returned a target for another version"
            )
        if supplied is not None and resolved != supplied:
            raise RegistryTargetError(
                "stable target is not the current verified release identity"
            )
        return resolved

    async def _current_sandbox_refs(self) -> tuple[str | None, str | None]:
        method = getattr(self.deployment, "sandbox_image_refs", None)
        if method is not None:
            value = method()
            value = await value if inspect.isawaitable(value) else value
            if isinstance(value, Mapping):
                return (
                    _sandbox_ref(value, "sandboxd_image"),
                    _sandbox_ref(value, "runner_image"),
                )
            if isinstance(value, tuple) and len(value) == 2:
                return (
                    value[0] if isinstance(value[0], str) else None,
                    value[1] if isinstance(value[1], str) else None,
                )
        return (
            _sandbox_ref(getattr(self.deployment, "sandboxd_image", None), "sandboxd_image"),
            _sandbox_ref(getattr(self.deployment, "runner_image", None), "runner_image"),
        )

    async def _current_sandbox_pair(self) -> tuple[tuple[str | None, str | None], dict[str, Any]]:
        """Read the old sandbox identity and reject a partial persisted pair."""

        refs = await self._current_sandbox_refs()
        complete = (refs[0] is None) == (refs[1] is None)
        detail = "both immutable refs present or both absent"
        if not complete:
            detail = "deployment.env contains only one sandbox image digest"
        return refs, self._check("current_sandbox_pair_complete", complete, detail)

    async def _development_sandbox_refs(
        self, target: DevelopmentTarget
    ) -> tuple[str | None, str | None]:
        """Resolve a sandbox pair for the requested development revision.

        Production ``ReleaseClient`` instances always expose the strict GHCR
        resolver.  Small compatibility doubles used by source-mode tests may
        not implement it; those doubles retain the historical no-registry
        behaviour, while the real client can never fall back to the currently
        persisted pair.
        """

        method = getattr(self.release_client, "resolve_development_sandbox_pair", None)
        if method is None:
            method = getattr(self.release_client, "development_sandbox_refs", None)
        if method is None:
            method = getattr(self.release_client, "fetch_development_sandbox_refs", None)
        if method is None:
            # The historical unit contract used a literal ``object()`` as a
            # release placeholder.  Keep that one inert test double from
            # making a network call; every real/custom release client must
            # implement the pair contract and is rejected when it does not.
            if type(self.release_client) is object:
                return await self._current_sandbox_refs()
            raise RegistryTargetError(
                "release client cannot resolve development sandbox images"
            )

        value = method(target)
        value = await value if inspect.isawaitable(value) else value
        revision = _value(value, "revision")
        if revision is not None and revision != target.revision:
            raise RegistryTargetError(
                "development sandbox pair revision does not match target"
            )
        if isinstance(value, Mapping):
            sandboxd = _sandbox_ref(value, "sandboxd_image") or _sandbox_ref(
                value, "sandboxd_ref"
            )
            runner = _sandbox_ref(value, "runner_image") or _sandbox_ref(
                value, "runner_ref"
            )
        elif isinstance(value, tuple) and len(value) == 2:
            sandboxd = value[0] if isinstance(value[0], str) else None
            runner = value[1] if isinstance(value[1], str) else None
        else:
            sandboxd = _sandbox_ref(value, "sandboxd_image") or _sandbox_ref(
                value, "sandboxd_ref"
            )
            runner = _sandbox_ref(value, "runner_image") or _sandbox_ref(
                value, "runner_ref"
            )
        if (
            sandboxd is None
            or runner is None
            or not _SANDBOX_REF_RE.fullmatch(sandboxd)
            or not _SANDBOX_REF_RE.fullmatch(runner)
            or not sandboxd.startswith(
                "ghcr.io/sakura520222/sakura-ai-sandboxd@"
            )
            or not runner.startswith(
                "ghcr.io/sakura520222/sakura-ai-agent-runner@"
            )
        ):
            raise RegistryTargetError(
                "development sandbox pair must contain two trusted immutable refs"
            )
        return sandboxd, runner

    async def check(self) -> dict[str, Any]:
        """Read latest stable release and readiness without mutating deployment.env."""

        current_version = await self.deployment.resolve_current_version()
        manifest = await self._manifest(None)
        latest_version = _value(manifest, "version")
        manifest_image = _value(manifest, "image")
        if not isinstance(latest_version, str) or not isinstance(manifest_image, str):
            raise ManifestInvalidError("manifest is missing version or image")
        stable = await self._resolve_stable_target(latest_version)
        expected_manifest_image = f"{stable.repository}:{stable.tag}"
        if manifest_image != expected_manifest_image:
            raise ManifestInvalidError("stable target does not match release manifest")
        ready = await self.preflight(stable)
        readiness = self._readiness_from_checks(ready["checks"])
        target = {
            **stable.to_dict(),
            "image": stable.image,
        }
        for key in ("sandboxd_image", "runner_image"):
            if ready.get(f"target_{key}") is not None:
                target[key] = ready[f"target_{key}"]
        # ``preflight`` already records the same snapshot, but record the
        # complete check projection as well so status mirrors this response.
        self.readiness_snapshot = {
            "update_ready": bool(ready["can_update"]),
            "readiness": readiness,
            "target": target,
        }
        return {
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": _is_newer(latest_version, current_version),
            "update_ready": bool(ready["can_update"]),
            "readiness": readiness,
            "target": target,
        }

    async def _asset_check(self, manifest: Any, target_version: str) -> bool:
        method = getattr(self.release_client, "has_required_assets", None)
        if method is None:
            # A custom test/development ReleaseClient may not expose release
            # assets.  The manifest parser remains the authority in that case.
            return True
        try:
            result = method(manifest, target_version)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except ReleaseClientError:
            return False

    async def _disk_check(self) -> tuple[bool, int | None]:
        method = getattr(self.deployment, "disk_space_sufficient", None)
        if method is not None:
            result = method(self.disk_space_threshold)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, tuple) and len(result) == 2:
                return bool(result[0]), result[1]
            return bool(result), None
        import shutil

        directory = (
            os.path.dirname(getattr(self.deployment, "deployment_env", ".")) or "."
        )
        usage = await asyncio.to_thread(shutil.disk_usage, directory)
        return usage.free >= self.disk_space_threshold, usage.free

    async def _current_state(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read running image identity as a structured preflight gate.

        Deployment identity failures are expected fail-closed readiness results,
        not updater protocol crashes. This keeps the IPC response typed and
        actionable instead of turning a local Docker metadata mismatch into an
        opaque HTTP 500/Backend 502.
        """

        state_method = getattr(self.deployment, "current_state", None)
        if state_method is None:
            return {}, self._check("current_image_identity_valid", True)
        try:
            value = state_method()
            state = await value if inspect.isawaitable(value) else value
        except DeploymentError as exc:
            return {}, self._check(
                "current_image_identity_valid",
                False,
                str(exc),
            )
        if not isinstance(state, dict):
            return {}, self._check(
                "current_image_identity_valid",
                False,
                "current deployment state is malformed",
            )
        return state, self._check("current_image_identity_valid", True)

    async def preflight(
        self,
        target_version: str | dict[str, Any],
        *,
        confirm_channel_switch: bool = False,
    ) -> dict[str, Any]:
        """Evaluate all readiness gates.  This method is read-only."""

        development = _development(target_version)
        stable = _stable(target_version)
        if development is None and stable is None and isinstance(target_version, dict):
            if target_version.get("channel") != "stable":
                raise RegistryTargetError("unsupported target channel")
            # A stable object without a tag and digest is not a shorthand for
            # a version.  Requiring the full parser here prevents IPC callers
            # from reintroducing a mutable tag through a partial object.
            stable = parse_stable_target(target_version)
        elif development is None and stable is None and isinstance(target_version, str):
            stable = await self._resolve_stable_target(target_version)
        if (
            development is None
            and stable is None
            and not isinstance(target_version, str)
        ):
            raise TargetNotFoundError("target")
        if development is not None:
            current_version = await self.deployment.resolve_current_version()
            _, sandbox_pair_check = (
                await self._current_sandbox_pair()
            )
            try:
                target_sandboxd_image, target_runner_image = (
                    await self._development_sandbox_refs(development)
                )
                target_sandbox_check = self._check(
                    "target_sandbox_pair_revision",
                    (
                        target_sandboxd_image is None
                        and target_runner_image is None
                    )
                    or (
                        target_sandboxd_image is not None
                        and target_runner_image is not None
                    ),
                    "both target sandbox images are immutable refs for the target revision",
                )
            except Exception as exc:
                target_sandboxd_image = target_runner_image = None
                target_sandbox_check = self._check(
                    "target_sandbox_pair_revision",
                    False,
                    str(exc),
                )
            mode = self.deployment.read_deploy_mode()
            checks: list[dict[str, Any]] = [
                self._check("deployment_mode_image", mode == "image", f"mode={mode!r}"),
                self._check("target_identity_valid", True),
                self._check("target_channel_head", True),
                sandbox_pair_check,
                target_sandbox_check,
            ]
            current_state, identity_check = await self._current_state()
            checks.append(identity_check)
            current_channel = (
                current_state.get("current_channel")
                if isinstance(current_state, dict)
                else None
            )
            current_digest = (
                current_state.get("running_container_digest")
                if isinstance(current_state, dict)
                else None
            )
            same_channel = current_channel == "development"
            digest_changed = current_digest != development.digest
            # A missing/legacy health identity is not evidence that the host is
            # already on the requested channel.  Require the explicit channel
            # switch confirmation for every current channel except a positively
            # identified development deployment.
            requires_confirmation = current_channel != "development"
            if same_channel or current_digest is not None:
                checks.append(
                    self._check(
                        "target_newer", digest_changed, "development digest differs"
                    )
                )
            else:
                checks.append(self._check("target_newer", True, "channel switch"))
            checks.append(
                self._check(
                    "channel_switch_confirmed",
                    not requires_confirmation or confirm_channel_switch,
                )
            )
            registry_ok = True
            try:
                await RegistryClient().verify_target(development)
            except Exception:
                registry_ok = False
            checks.append(self._check("registry_digest_matches", registry_ok))
            try:
                result = self.adapter.preflight_image(development.image)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                checks.append(self._check("image_manifest_exists", False))
            else:
                checks.append(self._check("image_manifest_exists", True))
            if target_sandboxd_image is not None or target_runner_image is not None:
                for check_name, sandbox_image in (
                    ("target_sandboxd_image_manifest_exists", target_sandboxd_image),
                    ("target_runner_image_manifest_exists", target_runner_image),
                ):
                    if not isinstance(sandbox_image, str):
                        checks.append(
                            self._check(check_name, False, "target ref unavailable")
                        )
                        continue
                    try:
                        result = self.adapter.preflight_image(sandbox_image)
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        checks.append(self._check(check_name, False))
                    else:
                        checks.append(self._check(check_name, True))
            disk_ok, free = await self._disk_check()
            checks.append(
                self._check(
                    "disk_space_sufficient", disk_ok, f"free={free}" if free else None
                )
            )
            result = {
                "can_update": all(item["passed"] for item in checks),
                "from_version": current_version,
                "target_version": development.version,
                "target_image": development.image,
                "target_channel": "development",
                "target_revision": development.revision,
                "target_digest": development.digest,
                "target_tag": development.tag,
                "target_sandboxd_image": target_sandboxd_image,
                "target_runner_image": target_runner_image,
                "requires_channel_switch_confirmation": requires_confirmation,
                "risk_code": "channel_switch" if requires_confirmation else None,
                "checks": checks,
            }
            self._remember_readiness(
                can_update=result["can_update"],
                checks=checks,
                target_version=development.version,
                target_image=development.image,
                channel="development",
                target_extra={
                    "revision": development.revision,
                    "tag": development.tag,
                    "digest": development.digest,
                    "sandboxd_image": target_sandboxd_image,
                    "runner_image": target_runner_image,
                },
            )
            return result

        if stable is not None:
            stable = await self._resolve_stable_target(stable.version, supplied=stable)
            manifest = await self._manifest(stable.version)
            sandbox_manifest = await self._sandbox_manifest(stable.version)
            manifest_version = _value(manifest, "version")
            manifest_image = _value(manifest, "image")
            expected_tag_image = f"{stable.repository}:{stable.tag}"
            if (
                manifest_version != stable.version
                or manifest_image != expected_tag_image
            ):
                raise ManifestInvalidError(
                    "stable target does not match release manifest"
                )
            current_version = await self.deployment.resolve_current_version()
            _, sandbox_pair_check = await self._current_sandbox_pair()
            min_version = _value(manifest, "min_upgrade_from", "0.0.0")
            mode = self.deployment.read_deploy_mode()
            current_state, identity_check = await self._current_state()
            current_channel = (
                current_state.get("current_channel")
                if isinstance(current_state, dict)
                else None
            )
            current_digest = (
                current_state.get("running_container_digest")
                if isinstance(current_state, dict)
                else None
            )
            requires_confirmation = current_channel != "stable"
            digest_changed = current_digest != stable.digest
            target_newer = (
                _is_newer(stable.version, current_version)
                if current_channel == "stable"
                else digest_changed
                if current_channel == "development"
                else _is_newer(stable.version, current_version)
            )
            checks: list[dict[str, Any]] = [
                self._check("manifest_found", True),
                self._check("manifest_valid", True),
                identity_check,
                self._check("deployment_mode_image", mode == "image", f"mode={mode!r}"),
                self._check(
                    "protocol_compatible",
                    _updater_value(manifest, "protocol_version") == PROTOCOL_VERSION,
                ),
                self._check(
                    "min_upgrade_from",
                    _version_tuple(current_version) is not None
                    and _version_tuple(min_version) is not None
                    and _version_tuple(current_version) >= _version_tuple(min_version),
                    f"{current_version} >= {min_version}",
                ),
                self._check(
                    "target_newer",
                    target_newer,
                    f"{stable.version} > {current_version}",
                ),
                self._check("already_current", digest_changed, "target digest differs"),
                self._check(
                    "channel_switch_confirmed",
                    not requires_confirmation or confirm_channel_switch,
                ),
                sandbox_pair_check,
            ]
            registry_ok = True
            try:
                await RegistryClient().verify_target(stable)
            except Exception:
                registry_ok = False
            checks.append(self._check("registry_digest_matches", registry_ok))
            try:
                image_result = self.adapter.preflight_image(stable.image)
                if inspect.isawaitable(image_result):
                    await image_result
            except Exception:
                checks.append(self._check("image_manifest_exists", False))
            else:
                checks.append(self._check("image_manifest_exists", True))
            for check_name, sandbox_image in (
                ("sandboxd_image_manifest_exists", sandbox_manifest.sandboxd_ref),
                ("runner_image_manifest_exists", sandbox_manifest.runner_ref),
            ):
                try:
                    sandbox_result = self.adapter.preflight_image(sandbox_image)
                    if inspect.isawaitable(sandbox_result):
                        await sandbox_result
                except Exception:
                    checks.append(self._check(check_name, False))
                else:
                    checks.append(self._check(check_name, True))
            disk_ok, free = await self._disk_check()
            checks.append(
                self._check(
                    "disk_space_sufficient", disk_ok, f"free={free}" if free else None
                )
            )
            assets_ok = await self._asset_check(manifest, stable.version)
            checks.append(self._check("updater_asset_present", assets_ok))
            result = {
                "can_update": all(item["passed"] for item in checks),
                "from_version": current_version,
                "target_version": stable.version,
                "target_image": stable.image,
                "target_channel": "stable",
                "target_digest": stable.digest,
                "target_tag": stable.tag,
                "target_sandboxd_image": sandbox_manifest.sandboxd_ref,
                "target_runner_image": sandbox_manifest.runner_ref,
                "requires_channel_switch_confirmation": requires_confirmation,
                "risk_code": "channel_switch" if requires_confirmation else None,
                "checks": checks,
            }
            self._remember_readiness(
                can_update=result["can_update"],
                checks=checks,
                target_version=stable.version,
                target_image=stable.image,
                channel="stable",
                target_extra={
                    "tag": stable.tag,
                    "digest": stable.digest,
                    "sandboxd_image": sandbox_manifest.sandboxd_ref,
                    "runner_image": sandbox_manifest.runner_ref,
                },
            )
            return result

        raise TargetNotFoundError("stable target")

    def _active_job(self) -> JobState | None:
        store = self._load()
        job = store.current_job
        if (
            store.active_job_id is not None
            and job is not None
            and not job.is_terminal()
        ):
            return job
        return None

    async def prepare_stop(self) -> dict[str, bool]:
        """Atomically reject new jobs after proving that no job is active.

        Lifecycle callers must use this gate before signalling the daemon.  It
        shares the destructive-update lock, so a submit either commits its
        durable active job first (and this call fails) or observes maintenance
        mode before it can commit a new job.
        """

        async with self._lock:
            active = self._active_job()
            if active is not None:
                raise UpdateInProgressError(active.job_id)
            self._accepting_updates = False
            return {"prepared": True}

    async def cancel_stop(self) -> dict[str, bool]:
        """Re-open submissions when a prepared lifecycle operation aborts."""

        async with self._lock:
            self._accepting_updates = True
            return {"prepared": False}

    async def submit_update(
        self,
        target_version: str | dict[str, Any] | None = None,
        *,
        confirm_channel_switch: bool = False,
    ) -> str:
        """Validate and enqueue one destructive update task."""

        if not self._accepting_updates:
            raise UpdaterMaintenanceError("updater is preparing to stop")
        if self._lock.locked():
            active = self._active_job()
            raise UpdateInProgressError(active.job_id if active else "unknown")
        active = self._active_job()
        if active is not None:
            raise UpdateInProgressError(active.job_id)
        if target_version is None:
            checked = await self.check()
            target_version = checked.get("target")
            if not isinstance(target_version, dict):
                raise TargetNotFoundError("latest")
        development = _development(target_version)
        stable = _stable(target_version)
        if development is None and stable is None and isinstance(target_version, dict):
            if target_version.get("channel") != "stable":
                raise RegistryTargetError("unsupported target channel")
            stable = parse_stable_target(target_version)
        if (
            development is None
            and stable is None
            and target_version is not None
            and not isinstance(target_version, str)
        ):
            raise TargetNotFoundError("target")
        preflight = await self.preflight(
            target_version,
            confirm_channel_switch=confirm_channel_switch,
        )
        if not preflight["can_update"]:
            raise PreflightFailedError(preflight["checks"], preflight)
        async with self._lock:
            if not self._accepting_updates:
                raise UpdaterMaintenanceError("updater is preparing to stop")
            active = self._active_job()
            if active is not None:
                raise UpdateInProgressError(active.job_id)
            job_id = f"upd_{uuid.uuid4().hex[:12]}"
            now = _utcnow()
            job = JobState(
                job_id=job_id,
                operation="update",
                deployment="image",
                from_version=preflight.get("from_version"),
                target_version=(
                    development.version
                    if development is not None
                    else stable.version
                    if stable is not None
                    else target_version
                ),
                target_image=preflight.get("target_image"),
                target_channel=preflight.get("target_channel"),
                target_revision=preflight.get("target_revision"),
                target_digest=preflight.get("target_digest"),
                target_tag=preflight.get("target_tag"),
                target_sandboxd_image=preflight.get("target_sandboxd_image"),
                target_runner_image=preflight.get("target_runner_image"),
                state="checking",
                step="checking",
                started_at=now,
                updated_at=now,
            )
            store = self._load()
            store.current_job = job
            store.active_job_id = job_id
            self._persist(store)
            self._log(job, "update job accepted", step="checking")
            task = asyncio.create_task(
                self._run_update_job(job), name=f"updater:{job_id}"
            )
            self._tasks[job_id] = task
            task.add_done_callback(lambda _: self._tasks.pop(job_id, None))
            return job_id

    def _save_job(self, job: JobState) -> None:
        job.updated_at = _utcnow()
        store = self._load()
        store.current_job = job
        self._persist(store)

    def _log(
        self,
        job: JobState,
        message: str,
        *,
        level: str = "info",
        step: str | None = None,
        error_code: str | None = None,
        stderr_lines: list[str] | None = None,
    ) -> None:
        self._logs.append(
            job.job_id,
            message,
            level=level,
            step=step or job.step,
            error_code=error_code,
            stderr_lines=stderr_lines,
        )

    def _transition(self, job: JobState, state: str, step: str | None = None) -> None:
        if state not in P0_STATES:
            raise ValueError(f"unsupported P0 state: {state}")
        job.state = state
        job.step = step or state
        self._save_job(job)

    def _clear_active_gate(self, job: JobState) -> None:
        if not job.is_terminal():
            return
        store = self._load()
        if store.current_job is not None and store.current_job.job_id == job.job_id:
            store.current_job = job
            store.active_job_id = None
            self._persist(store)

    async def _pull_with_timeout_retry(self, job: JobState, image: str | None = None) -> None:
        """Retry one timed-out Docker pull; Docker resumes already downloaded layers."""

        target_image = image or job.target_image
        if not isinstance(target_image, str) or not target_image:
            raise TargetNotFoundError("image")
        while True:
            try:
                await self.adapter.pull(target_image)
                return
            except Exception as exc:
                if (
                    getattr(exc, "error_code", None) != "command_timeout"
                    or job.retry_count >= 1
                ):
                    raise
                job.retry_count += 1
                self._save_job(job)
                self._log(
                    job,
                    "docker pull timed out; retrying with cached layers",
                    level="warning",
                    step="downloading",
                    error_code="command_timeout_retry",
                )

    @staticmethod
    def _error_details(exc: Exception) -> tuple[str, str, list[str] | None]:
        error_code = str(getattr(exc, "error_code", "update_failed"))
        stderr = str(getattr(exc, "stderr", "") or "")
        lines = stderr.splitlines() or None
        return error_code, str(exc), lines

    async def _rollback_after_cancellation(
        self, snapshot: Any, *, already_rolled_back: bool = False
    ) -> Exception | None:
        """Best-effort bounded rollback while preserving task cancellation.

        A cancellation can arrive after ``activate`` has replaced the
        authoritative env but before the health gate.  The rollback itself is
        safety-critical, so run it in a shielded task and wait for that task to
        finish before re-raising ``CancelledError``.  A failed rollback is
        returned to the caller while the active job gate remains durable for
        startup reconcile.
        """

        if already_rolled_back:
            return None
        rollback_method = getattr(self.adapter, "rollback", None)
        if rollback_method is None:
            return RuntimeError("adapter does not provide transactional rollback")
        try:
            result = rollback_method(snapshot)
            if inspect.isawaitable(result):
                rollback_task = asyncio.create_task(result)
                try:
                    await asyncio.shield(rollback_task)
                except asyncio.CancelledError:
                    # The outer task is already being cancelled.  Shield keeps
                    # the actual restore alive; this second await observes its
                    # result without cancelling it.
                    await asyncio.shield(rollback_task)
        except asyncio.CancelledError:
            return RuntimeError("rollback was cancelled before it completed")
        except Exception as exc:
            return exc
        return None

    async def _run_update_job(self, job: JobState) -> None:
        deployment_snapshot: Any = None
        try:
            self._transition(job, "checking", "checking")
            if job.target_channel == "development":
                if not all(
                    (
                        job.target_version,
                        job.target_revision,
                        job.target_digest,
                        job.target_tag,
                    )
                ):
                    raise RegistryTargetError(
                        "persisted development target is incomplete"
                    )
                target = DevelopmentTarget(
                    channel="development",
                    version=job.target_version or "",
                    revision=job.target_revision or "",
                    tag=job.target_tag or "",
                    digest=job.target_digest or "",
                )
                await RegistryClient().verify_target(target)
                job.target_image = target.image
            elif (
                job.target_channel == "stable" and job.target_digest and job.target_tag
            ):
                target = StableTarget(
                    channel="stable",
                    version=job.target_version or "",
                    tag=job.target_tag,
                    digest=job.target_digest,
                )
                target = await self._resolve_stable_target(
                    target.version,
                    supplied=target,
                )
                await RegistryClient().verify_target(target)
                job.target_image = target.image
            else:
                raise RegistryTargetError(
                    "persisted stable target is incomplete; digest pin is required"
                )
            self._transition(job, "update_available", "update_available")
            self._transition(job, "preflight", "preflight")
            if job.target_channel == "development":
                result = await self.preflight(
                    {
                        "channel": "development",
                        "version": job.target_version,
                        "revision": job.target_revision,
                        "tag": job.target_tag,
                        "digest": job.target_digest,
                    },
                    confirm_channel_switch=True,
                )
            elif (
                job.target_channel == "stable" and job.target_digest and job.target_tag
            ):
                result = await self.preflight(
                    {
                        "channel": "stable",
                        "version": job.target_version,
                        "tag": job.target_tag,
                        "digest": job.target_digest,
                    },
                    confirm_channel_switch=True,
                )
            else:
                raise RegistryTargetError(
                    "persisted stable target is incomplete; digest pin is required"
                )
            if not result["can_update"]:
                raise PreflightFailedError(result["checks"], result)
            job.target_sandboxd_image = result.get(
                "target_sandboxd_image", job.target_sandboxd_image
            )
            job.target_runner_image = result.get(
                "target_runner_image", job.target_runner_image
            )
            self._save_job(job)
            # Resolve a moving current anchor without writing the authoritative
            # file.  The adapter snapshot below freezes that anchor in memory;
            # its durable rollback copy is prepared immediately before the
            # first activation write.  This closes the old empty-body path
            # where materialization happened before the rollback snapshot.
            self._transition(job, "preflight", "materialize_current_anchor")
            from_image = await self.deployment.capture_from_image()
            from_digest = await self.deployment.capture_from_digest()
            job.from_image = from_image
            job.from_digest = from_digest
            from_sandboxd, from_runner = await self._current_sandbox_refs()
            job.from_sandboxd_image = from_sandboxd
            job.from_runner_image = from_runner
            # Capture the byte-for-byte rollback snapshot before invoking any
            # materializer.  The production provider accepts ``persist=False``
            # below, but older injected providers may not expose that keyword
            # and can still materialize as part of their compatibility API.
            # Keeping the first capture here makes that fallback recoverable
            # too; a later anchored capture is only used when no write was
            # allowed by the provider contract.
            capture_snapshot = getattr(self.adapter, "capture_snapshot", None)
            capture_parameters: list[inspect.Parameter] = []
            accepts_anchor = False
            if capture_snapshot is not None:
                capture_parameters = list(
                    inspect.signature(capture_snapshot).parameters.values()
                )
                accepts_anchor = any(
                    parameter.kind == inspect.Parameter.VAR_KEYWORD
                    or parameter.name == "anchor_image"
                    for parameter in capture_parameters
                )
                deployment_snapshot = capture_snapshot()
                if inspect.isawaitable(deployment_snapshot):
                    deployment_snapshot = await deployment_snapshot
            materialize = self.deployment.materialize_current_anchor
            materialize_parameters = list(inspect.signature(materialize).parameters.values())
            accepts_persist = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                or parameter.name == "persist"
                for parameter in materialize_parameters
            )
            materialized = (
                await materialize(persist=False)
                if accepts_persist
                else await materialize()
            )
            if materialized:
                job.from_image = materialized
            if capture_snapshot is not None and accepts_anchor and accepts_persist and materialized:
                # The provider explicitly promised that materialization was
                # non-persistent, so replace only the in-memory Web anchor in
                # the adapter snapshot.  Do not recapture after a legacy
                # provider's compatibility materialization may have written
                # the authoritative file.
                deployment_snapshot = capture_snapshot(anchor_image=materialized)
                if inspect.isawaitable(deployment_snapshot):
                    deployment_snapshot = await deployment_snapshot
            self._save_job(job)
            self._transition(job, "downloading", "downloading")
            await self._pull_with_timeout_retry(job, job.target_image)
            if job.target_sandboxd_image and job.target_runner_image:
                await self._pull_with_timeout_retry(job, job.target_sandboxd_image)
                await self._pull_with_timeout_retry(job, job.target_runner_image)
            self._transition(job, "activating", "activating")
            job.activation_started = True
            self._save_job(job)
            activate = self.adapter.activate
            signature = inspect.signature(activate)
            parameters = list(signature.parameters.values())
            accepts_sandbox = any(
                parameter.kind == inspect.Parameter.VAR_POSITIONAL
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                or parameter.name in {"sandboxd_image", "runner_image"}
                for parameter in parameters
            ) or len(parameters) >= 3
            if accepts_sandbox:
                activation_result = activate(
                    job.target_image,
                    job.target_sandboxd_image,
                    job.target_runner_image,
                )
            else:
                # Compatibility for source/development adapters that predate
                # the independent sandbox parameters. Production ImageAdapter
                # always takes the three-image form.
                activation_result = activate(job.target_image)
            if inspect.isawaitable(activation_result):
                await activation_result
            job.rollback_allowed = True
            self._save_job(job)
            self._transition(job, "restarting", "restarting")
            self._transition(job, "health_checking", "health_checking")
            if job.target_channel == "development":
                await self.adapter.health_check(
                    {
                        "version": job.target_version,
                        "channel": "development",
                        "revision": job.target_revision,
                    }
                )
            elif (
                job.target_channel == "stable" and job.target_digest and job.target_tag
            ):
                await self.adapter.health_check(
                    {"version": job.target_version, "channel": "stable"}
                )
            else:
                await self.adapter.health_check(job.target_version)
            finalize_activation = getattr(self.adapter, "finalize_activation", None)
            if finalize_activation is not None:
                finalized = finalize_activation()
                if inspect.isawaitable(finalized):
                    await finalized
            job.rollback_allowed = False
            self._transition(job, "success", "complete")
            completed_checks = [
                {
                    **item,
                    "passed": False,
                    "detail": "target digest is now running",
                }
                if item.get("name") in {"target_newer", "already_current"}
                else dict(item)
                for item in result["checks"]
            ]
            self._remember_readiness(
                can_update=False,
                checks=completed_checks,
                target_version=job.target_version or "",
                target_image=job.target_image or "",
                channel=job.target_channel,
                target_extra={
                    key: value
                    for key, value in {
                        "revision": job.target_revision,
                        "tag": job.target_tag,
                        "digest": job.target_digest,
                        "sandboxd_image": job.target_sandboxd_image,
                        "runner_image": job.target_runner_image,
                    }.items()
                    if value is not None
                },
            )
            self._log(job, "update completed", step="complete")
            self._clear_active_gate(job)
        except asyncio.CancelledError:
            # Cancellation is not a normal failure, but once activation has
            # started it must still restore the exact pre-activation snapshot
            # before this task exits.  Keep the active gate durable so a
            # daemon restart can reconcile any incomplete rollback.
            rollback_error: Exception | None = None
            if job.activation_started and deployment_snapshot is not None:
                job.rollback_attempted = True
                rollback_error = await self._rollback_after_cancellation(
                    deployment_snapshot,
                    already_rolled_back=bool(
                        getattr(self.adapter, "activation_rollback_completed", False)
                    ),
                )
                if rollback_error is not None:
                    job.error_code = "cancelled_rollback_failed"
                    job.error = f"update cancelled; rollback failed: {rollback_error}"
                else:
                    job.error_code = "cancelled"
                    job.error = "update cancelled; deployment restored"
            try:
                self._save_job(job)
            finally:
                raise
        except Exception as exc:
            error_code, message, stderr_lines = self._error_details(exc)
            rollback_error: Exception | None = None
            if job.activation_started and deployment_snapshot is not None:
                job.rollback_attempted = True
                self._save_job(job)
                already_rolled_back = bool(
                    getattr(self.adapter, "activation_rollback_completed", False)
                )
                rollback_method = getattr(self.adapter, "rollback", None)
                if already_rolled_back:
                    rollback_method = None
                elif rollback_method is None:
                    rollback_error = RuntimeError(
                        "adapter does not provide transactional rollback"
                    )
                if rollback_method is not None:
                    try:
                        rollback_result = rollback_method(deployment_snapshot)
                        if inspect.isawaitable(rollback_result):
                            await rollback_result
                        # Do not report success until the restored Web is also
                        # serving. ImageAdapter's rollback converges sandboxd
                        # before Compose, so this health probe cannot silently
                        # bless a new Web with an old/unready sidecar.
                        old_version = job.from_version
                        if old_version:
                            health_result = self.adapter.health_check(old_version)
                            if inspect.isawaitable(health_result):
                                await health_result
                    except Exception as rollback_exc:
                        rollback_error = rollback_exc
                elif already_rolled_back:
                    try:
                        old_version = job.from_version
                        if old_version:
                            health_result = self.adapter.health_check(old_version)
                            if inspect.isawaitable(health_result):
                                await health_result
                    except Exception as rollback_exc:
                        rollback_error = rollback_exc
            if rollback_error is not None:
                message = f"{message}; rollback failed: {rollback_error}"
                error_code = f"{error_code}_rollback_failed"
            job.state = "failed"
            job.step = job.step or "failed"
            job.error_code = error_code
            job.error = message
            # Persist the terminal state before clearing the destructive gate.
            # If durable persistence itself fails, retaining the gate is the
            # safe fail-closed outcome and lets the next daemon startup
            # reconcile the still-active job.
            self._save_job(job)
            self._log(
                job,
                message,
                level="error",
                step=job.step,
                error_code=error_code,
                stderr_lines=stderr_lines,
            )
            self._clear_active_gate(job)

    def get_job(self, job_id: str) -> JobState | None:
        store = self._load()
        if store.current_job is None or store.current_job.job_id != job_id:
            return None
        return store.current_job

    def get_job_logs(self, job_id: str) -> dict[str, Any]:
        """Return the complete logs endpoint payload for one job.

        Keeping the public method aligned with ``GET /v1/jobs/{id}/logs``
        avoids a second payload-only API that callers could accidentally use
        and lose the ``job_id``/``truncated`` metadata.
        """

        return self._logs.snapshot(job_id)

    def get_job_logs_payload(self, job_id: str) -> dict[str, Any]:
        # Backward-compatible alias for the transitional IPC integration.
        return self.get_job_logs(job_id)

    async def wait_for_job(self, job_id: str) -> JobState | None:
        task = self._tasks.get(job_id)
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                raise
            except Exception:
                # The job state already contains the structured failure.
                pass
        return self.get_job(job_id)
