"""P0 update job orchestration and durable state transitions."""

from __future__ import annotations

import asyncio
import inspect
import os
import uuid
from typing import Any

from sakura_ai_updater import PROTOCOL_VERSION
from sakura_ai_updater.deployment import DeploymentStateProvider
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


class TargetNotFoundError(UpdateOrchestrationError):
    """The requested release/manifest does not exist."""


class PreflightFailedError(UpdateOrchestrationError):
    """A valid manifest failed one or more readiness gates."""

    def __init__(self, checks: list[dict[str, Any]], result: dict[str, Any] | None = None) -> None:
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
    except (ImportError, ValueError, TypeError):
        pass
    parts = version.split(".")
    if len(parts) != 3 or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)  # type: ignore[return-value]


def _is_newer(target: str | None, current: str | None) -> bool:
    target_tuple = _version_tuple(target)
    current_tuple = _version_tuple(current)
    return target_tuple is not None and current_tuple is not None and target_tuple > current_tuple


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
            raise ManifestInvalidError("release client does not provide a manifest method")
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

    async def check(self) -> dict[str, Any]:
        """Read latest stable release and readiness without mutating deployment.env."""

        current_version = await self.deployment.resolve_current_version()
        manifest = await self._manifest(None)
        latest_version = _value(manifest, "version")
        target_image = _value(manifest, "image")
        if not isinstance(latest_version, str) or not isinstance(target_image, str):
            raise ManifestInvalidError("manifest is missing version or image")
        ready = await self.preflight(latest_version)
        readiness = self._readiness_from_checks(ready["checks"])
        target = {
            "version": latest_version,
            "image": target_image,
            "channel": _value(manifest, "channel"),
        }
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

        directory = os.path.dirname(getattr(self.deployment, "deployment_env", ".")) or "."
        usage = await asyncio.to_thread(shutil.disk_usage, directory)
        return usage.free >= self.disk_space_threshold, usage.free

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
            target_version = target_version.get("version")
        if development is None and stable is None and not isinstance(target_version, str):
            raise TargetNotFoundError("target")
        if development is not None:
            current_version = await self.deployment.resolve_current_version()
            mode = self.deployment.read_deploy_mode()
            checks: list[dict[str, Any]] = [
                self._check("deployment_mode_image", mode == "image", f"mode={mode!r}"),
                self._check("target_identity_valid", True),
                self._check("target_channel_head", True),
            ]
            current_state = {}
            state_method = getattr(self.deployment, "current_state", None)
            if state_method is not None:
                value = state_method()
                current_state = await value if inspect.isawaitable(value) else value
            current_channel = current_state.get("current_channel") if isinstance(current_state, dict) else None
            current_digest = current_state.get("running_container_digest") if isinstance(current_state, dict) else None
            same_channel = current_channel == "development"
            digest_changed = current_digest != development.digest
            # A missing/legacy health identity is not evidence that the host is
            # already on the requested channel.  Require the explicit channel
            # switch confirmation for every current channel except a positively
            # identified development deployment.
            requires_confirmation = current_channel != "development"
            if same_channel or current_digest is not None:
                checks.append(self._check("target_newer", digest_changed, "development digest differs"))
            else:
                checks.append(self._check("target_newer", True, "channel switch"))
            checks.append(self._check("channel_switch_confirmed", not requires_confirmation or confirm_channel_switch))
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
            disk_ok, free = await self._disk_check()
            checks.append(self._check("disk_space_sufficient", disk_ok, f"free={free}" if free else None))
            result = {
                "can_update": all(item["passed"] for item in checks),
                "from_version": current_version,
                "target_version": development.version,
                "target_image": development.image,
                "target_channel": "development",
                "target_revision": development.revision,
                "target_digest": development.digest,
                "target_tag": development.tag,
                "requires_channel_switch_confirmation": requires_confirmation,
                "risk_code": "channel_switch" if requires_confirmation else None,
                "checks": checks,
            }
            self._remember_readiness(
                can_update=result["can_update"], checks=checks,
                target_version=development.version, target_image=development.image,
                channel="development",
                target_extra={
                    "revision": development.revision,
                    "tag": development.tag,
                    "digest": development.digest,
                },
            )
            return result

        if stable is not None:
            manifest = await self._manifest(stable.version)
            manifest_version = _value(manifest, "version")
            manifest_image = _value(manifest, "image")
            expected_tag_image = f"{stable.repository}:{stable.tag}"
            if manifest_version != stable.version or manifest_image != expected_tag_image:
                raise ManifestInvalidError("stable target does not match release manifest")
            current_version = await self.deployment.resolve_current_version()
            min_version = _value(manifest, "min_upgrade_from", "0.0.0")
            mode = self.deployment.read_deploy_mode()
            current_state = {}
            state_method = getattr(self.deployment, "current_state", None)
            if state_method is not None:
                state_value = state_method()
                current_state = await state_value if inspect.isawaitable(state_value) else state_value
            current_channel = current_state.get("current_channel") if isinstance(current_state, dict) else None
            current_digest = current_state.get("running_container_digest") if isinstance(current_state, dict) else None
            requires_confirmation = current_channel != "stable"
            digest_changed = current_digest != stable.digest
            target_newer = (
                _is_newer(stable.version, current_version)
                if current_channel == "stable"
                else digest_changed
            )
            checks: list[dict[str, Any]] = [
                self._check("manifest_found", True),
                self._check("manifest_valid", True),
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
                self._check("target_newer", target_newer, f"{stable.version} > {current_version}"),
                self._check("already_current", digest_changed, "target digest differs"),
                self._check(
                    "channel_switch_confirmed",
                    not requires_confirmation or confirm_channel_switch,
                ),
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
            disk_ok, free = await self._disk_check()
            checks.append(self._check("disk_space_sufficient", disk_ok, f"free={free}" if free else None))
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
                "requires_channel_switch_confirmation": requires_confirmation,
                "risk_code": "channel_switch" if requires_confirmation else None,
                "checks": checks,
            }
            self._remember_readiness(
                can_update=result["can_update"], checks=checks,
                target_version=stable.version, target_image=stable.image,
                channel="stable",
                target_extra={"tag": stable.tag, "digest": stable.digest},
            )
            return result

        manifest = await self._manifest(target_version)
        manifest_version = _value(manifest, "version")
        if manifest_version != target_version:
            raise ManifestInvalidError(
                f"manifest version {manifest_version!r} does not match target {target_version!r}"
            )
        target_image = _value(manifest, "image")
        if not isinstance(target_image, str) or not target_image:
            raise ManifestInvalidError("manifest image is missing")
        current_version = await self.deployment.resolve_current_version()
        mode = self.deployment.read_deploy_mode()
        min_version = _value(manifest, "min_upgrade_from", "0.0.0")
        protocol = _updater_value(manifest, "protocol_version")
        checks: list[dict[str, Any]] = [
            self._check("manifest_found", True),
            self._check("manifest_valid", True),
            self._check(
                "deployment_mode_image",
                mode == "image",
                f"mode={mode!r}",
            ),
            self._check(
                "protocol_compatible",
                protocol == PROTOCOL_VERSION,
                f"manifest={protocol!r} current={PROTOCOL_VERSION}",
            ),
            self._check(
                "target_newer",
                _is_newer(target_version, current_version),
                f"{target_version} > {current_version}",
            ),
            self._check(
                "min_upgrade_from",
                _version_tuple(current_version) is not None
                and _version_tuple(min_version) is not None
                and _version_tuple(current_version) >= _version_tuple(min_version),
                f"{current_version} >= {min_version}",
            ),
        ]
        image_exists = True
        try:
            result = self.adapter.preflight_image(target_image)
            if inspect.isawaitable(result):
                await result
        except Exception:
            image_exists = False
        checks.append(self._check("image_manifest_exists", image_exists))
        disk_ok, free = await self._disk_check()
        detail = f"free={free} threshold={self.disk_space_threshold}" if free is not None else None
        checks.append(self._check("disk_space_sufficient", disk_ok, detail))
        assets_ok = await self._asset_check(manifest, target_version)
        checks.append(self._check("updater_asset_present", assets_ok))
        result = {
            "can_update": all(item["passed"] for item in checks),
            "from_version": current_version,
            "target_version": target_version,
            "target_image": target_image,
            "checks": checks,
        }
        self._remember_readiness(
            can_update=result["can_update"],
            checks=checks,
            target_version=target_version,
            target_image=target_image,
            channel=_value(manifest, "channel"),
        )
        return result

    def _active_job(self) -> JobState | None:
        store = self._load()
        job = store.current_job
        if store.active_job_id is not None and job is not None and not job.is_terminal():
            return job
        return None

    async def submit_update(
        self,
        target_version: str | dict[str, Any] | None = None,
        *,
        confirm_channel_switch: bool = False,
    ) -> str:
        """Validate and enqueue one destructive update task."""

        if self._lock.locked():
            active = self._active_job()
            raise UpdateInProgressError(active.job_id if active else "unknown")
        active = self._active_job()
        if active is not None:
            raise UpdateInProgressError(active.job_id)
        if target_version is None:
            checked = await self.check()
            target_version = checked.get("latest_version")
            if not isinstance(target_version, str):
                raise TargetNotFoundError("latest")
        development = _development(target_version)
        stable = _stable(target_version)
        if development is None and stable is None and isinstance(target_version, dict):
            if target_version.get("channel") != "stable":
                raise RegistryTargetError("unsupported target channel")
            target_version = target_version.get("version")
        if development is None and stable is None and target_version is not None and not isinstance(target_version, str):
            raise TargetNotFoundError("target")
        preflight = await self.preflight(
            target_version,
            confirm_channel_switch=confirm_channel_switch,
        )
        if not preflight["can_update"]:
            raise PreflightFailedError(preflight["checks"], preflight)
        async with self._lock:
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
                    development.version if development is not None
                    else stable.version if stable is not None
                    else target_version
                ),
                target_image=preflight.get("target_image"),
                target_channel=preflight.get("target_channel"),
                target_revision=preflight.get("target_revision"),
                target_digest=preflight.get("target_digest"),
                target_tag=preflight.get("target_tag"),
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
            task = asyncio.create_task(self._run_update_job(job), name=f"updater:{job_id}")
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

    async def _pull_with_timeout_retry(self, job: JobState) -> None:
        """Retry one timed-out Docker pull; Docker resumes already downloaded layers."""

        while True:
            try:
                await self.adapter.pull(job.target_image)
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

    async def _run_update_job(self, job: JobState) -> None:
        try:
            self._transition(job, "checking", "checking")
            if job.target_channel == "development":
                if not all((job.target_version, job.target_revision, job.target_digest, job.target_tag)):
                    raise RegistryTargetError("persisted development target is incomplete")
                target = DevelopmentTarget(
                    channel="development",
                    version=job.target_version or "",
                    revision=job.target_revision or "",
                    tag=job.target_tag or "",
                    digest=job.target_digest or "",
                )
                await RegistryClient().verify_target(target)
                job.target_image = target.image
            elif job.target_channel == "stable" and job.target_digest and job.target_tag:
                target = StableTarget(
                    channel="stable",
                    version=job.target_version or "",
                    tag=job.target_tag,
                    digest=job.target_digest,
                )
                await RegistryClient().verify_target(target)
                job.target_image = target.image
            else:
                manifest = await self._manifest(job.target_version)
                job.target_image = _value(manifest, "image", job.target_image)
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
            elif job.target_channel == "stable" and job.target_digest and job.target_tag:
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
                result = await self.preflight(job.target_version or "")
            if not result["can_update"]:
                raise PreflightFailedError(result["checks"], result)
            # :latest materialization is destructive by definition and is not
            # reached by check()/preflight() callers.
            self._transition(job, "preflight", "materialize_current_anchor")
            from_image = await self.deployment.capture_from_image()
            from_digest = await self.deployment.capture_from_digest()
            job.from_image = from_image
            job.from_digest = from_digest
            materialized = await self.deployment.materialize_current_anchor()
            if materialized:
                job.from_image = materialized
            self._save_job(job)
            self._transition(job, "downloading", "downloading")
            await self._pull_with_timeout_retry(job)
            self._transition(job, "activating", "activating")
            await self.adapter.activate(job.target_image)
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
            elif job.target_channel == "stable" and job.target_digest and job.target_tag:
                await self.adapter.health_check(
                    {"version": job.target_version, "channel": "stable"}
                )
            else:
                await self.adapter.health_check(job.target_version)
            self._transition(job, "success", "complete")
            self._log(job, "update completed", step="complete")
            self._clear_active_gate(job)
        except asyncio.CancelledError:
            # Cancellation is not a normal failure.  Persist the current
            # non-terminal state and retain active_job_id for daemon reconcile.
            try:
                self._save_job(job)
            finally:
                raise
        except Exception as exc:
            error_code, message, stderr_lines = self._error_details(exc)
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
