"""Strict development-channel registry target validation.

Stable updates intentionally continue to use ``update-manifest.json`` v1.  A
development target is a separate protocol object and is never converted into
or accepted as a stable manifest.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = "ghcr.io/sakura520222/sakura-ai"
SANDBOXD_REPOSITORY = "ghcr.io/sakura520222/sakura-ai-sandboxd"
RUNNER_REPOSITORY = "ghcr.io/sakura520222/sakura-ai-agent-runner"
_TAG_RE = re.compile(
    r"^dev-(?P<created>[0-9]{14})-v(?P<version>(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*))-(?P<revision>[0-9a-f]{40})$"
)
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MANIFEST_ACCEPT = (
    "application/vnd.oci.image.index.v1+json, "
    "application/vnd.docker.distribution.manifest.list.v2+json, "
    "application/vnd.oci.image.manifest.v1+json, "
    "application/vnd.docker.distribution.manifest.v2+json"
)
_CONFIG_ACCEPT = (
    "application/vnd.oci.image.config.v1+json, "
    "application/vnd.docker.container.image.v1+json, "
    "application/json"
)
_IMAGE_MANIFEST_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    }
)
_INDEX_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
    }
)
_CONFIG_MEDIA_TYPES = frozenset(
    {
        "application/vnd.oci.image.config.v1+json",
        "application/vnd.docker.container.image.v1+json",
    }
)
_ATTESTATION_MEDIA_TYPES = frozenset(
    {
        "application/vnd.in-toto+json",
        "application/vnd.oci.artifact.manifest.v1+json",
    }
)


class RegistryTargetError(ValueError):
    """Target is not an exact, current development image identity."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class DevelopmentTarget:
    channel: str
    version: str
    revision: str
    tag: str
    digest: str
    repository: str = REPOSITORY

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"

    def to_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "version": self.version,
            "revision": self.revision,
            "tag": self.tag,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class StableTarget:
    channel: str
    version: str
    tag: str
    digest: str
    repository: str = REPOSITORY

    @property
    def image(self) -> str:
        return f"{self.repository}:{self.tag}@{self.digest}"

    def to_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "version": self.version,
            "tag": self.tag,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class DevelopmentSandboxPair:
    """Immutable sandbox images built from one development revision."""

    revision: str
    sandboxd_image: str
    runner_image: str

    @property
    def sandboxd_ref(self) -> str:
        return self.sandboxd_image

    @property
    def runner_ref(self) -> str:
        return self.runner_image


def parse_development_target(value: Any) -> DevelopmentTarget:
    if not isinstance(value, dict):
        raise RegistryTargetError("target must be an object")
    if value.get("channel") != "development":
        raise RegistryTargetError("target channel must be development")
    repository = value.get("repository", REPOSITORY)
    if repository != REPOSITORY:
        raise RegistryTargetError("target repository is not trusted")
    tag = value.get("tag")
    digest = value.get("digest")
    version = value.get("version")
    revision = value.get("revision")
    if not all(isinstance(item, str) for item in (tag, digest, version, revision)):
        raise RegistryTargetError("target fields must be strings")
    match = _TAG_RE.fullmatch(tag)
    if match is None:
        raise RegistryTargetError("tag is not an immutable development tag")
    if match.group("version") != version or match.group("revision") != revision:
        raise RegistryTargetError("tag identity does not match version/revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RegistryTargetError("revision must be lowercase full SHA")
    if not _DIGEST_RE.fullmatch(digest):
        raise RegistryTargetError("digest must be a sha256 manifest digest")
    return DevelopmentTarget("development", version, revision, tag, digest, repository)


def parse_stable_target(value: Any) -> StableTarget:
    if not isinstance(value, dict):
        raise RegistryTargetError("target must be an object")
    if value.get("channel") != "stable":
        raise RegistryTargetError("target channel must be stable")
    repository = value.get("repository", REPOSITORY)
    if repository != REPOSITORY:
        raise RegistryTargetError("target repository is not trusted")
    version = value.get("version")
    tag = value.get("tag")
    digest = value.get("digest")
    if not all(isinstance(item, str) for item in (version, tag, digest)):
        raise RegistryTargetError("target fields must be strings")
    if not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise RegistryTargetError("stable target version is invalid")
    if tag != f"v{version}":
        raise RegistryTargetError("stable tag does not match version")
    if not _DIGEST_RE.fullmatch(digest):
        raise RegistryTargetError("digest must be a sha256 manifest digest")
    return StableTarget("stable", version, tag, digest, repository)


class RegistryClient:
    """Minimal standard-library GHCR client used by the host trust boundary."""

    def __init__(self, repository: str = REPOSITORY, *, timeout: float = 15.0):
        if repository != REPOSITORY:
            raise ValueError("registry repository is fixed")
        self.repository = repository
        self.timeout = timeout

    def _json_sync(
        self, url: str, headers: dict[str, str]
    ) -> tuple[Any, dict[str, str]]:
        try:
            with urlopen(
                Request(url, headers=headers), timeout=self.timeout
            ) as response:
                return json.loads(response.read().decode("utf-8")), {
                    str(k).lower(): str(v) for k, v in response.headers.items()
                }
        except HTTPError as exc:
            raise RegistryTargetError(
                "registry request failed", status_code=int(exc.code)
            ) from exc
        except (
            URLError,
            OSError,
            TimeoutError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise RegistryTargetError("registry request failed") from exc

    def _manifest_sync(self, tag: str, token: str) -> str:
        return self._manifest_sync_for(self.repository, tag, token)

    def _manifest_sync_for(self, repository: str, tag: str, token: str) -> str:
        """Read one manifest digest from a trusted GHCR repository."""

        _, headers = self._manifest_response_sync(repository, tag, token)
        return self._manifest_digest(headers)

    @staticmethod
    def _manifest_digest(headers: Any) -> str:
        """Read a strict Docker-Content-Digest header without type hazards."""

        digest = None
        if isinstance(headers, dict):
            for key, value in headers.items():
                if str(key).lower() == "docker-content-digest":
                    digest = value
                    break
        if not isinstance(digest, str):
            raise RegistryTargetError("registry did not return a manifest digest")
        digest = digest.lower()
        if not _DIGEST_RE.fullmatch(digest):
            raise RegistryTargetError("registry did not return a manifest digest")
        return digest

    def _manifest_response_sync(
        self, repository: str, reference: str, token: str
    ) -> tuple[Any, dict[str, str]]:
        """Fetch one trusted registry manifest and its response headers."""

        if repository not in {
            self.repository,
            SANDBOXD_REPOSITORY,
            RUNNER_REPOSITORY,
        }:
            raise RegistryTargetError("registry repository is not trusted")
        registry, path = repository.split("/", 1)
        return self._json_sync(
            f"https://{registry}/v2/{path}/manifests/{reference}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": _MANIFEST_ACCEPT,
            },
        )

    def _config_response_sync(
        self, repository: str, digest: str, token: str
    ) -> tuple[Any, dict[str, str]]:
        """Fetch one image config blob from a trusted registry repository."""

        if repository not in {
            self.repository,
            SANDBOXD_REPOSITORY,
            RUNNER_REPOSITORY,
        }:
            raise RegistryTargetError("registry repository is not trusted")
        if not _DIGEST_RE.fullmatch(digest):
            raise RegistryTargetError("image config digest is invalid")
        registry, path = repository.split("/", 1)
        return self._json_sync(
            f"https://{registry}/v2/{path}/blobs/{digest}",
            {
                "Authorization": f"Bearer {token}",
                "Accept": _CONFIG_ACCEPT,
            },
        )

    @staticmethod
    def _is_attestation_descriptor(descriptor: dict[str, Any]) -> bool:
        """Identify BuildKit provenance/SBOM descriptors in an OCI index."""

        media_type = descriptor.get("mediaType")
        if media_type in _ATTESTATION_MEDIA_TYPES:
            return True
        annotations = descriptor.get("annotations")
        return isinstance(annotations, dict) and (
            annotations.get("vnd.docker.reference.type") == "attestation-manifest"
        )

    @staticmethod
    def _image_descriptors(payload: Any) -> list[dict[str, Any]]:
        """Return image manifest descriptors from a strict OCI/Docker response."""

        if not isinstance(payload, dict) or payload.get("schemaVersion") != 2:
            raise RegistryTargetError("registry manifest schema is invalid")
        media_type = payload.get("mediaType")
        if media_type in _IMAGE_MANIFEST_MEDIA_TYPES:
            return [payload]
        if media_type not in _INDEX_MEDIA_TYPES:
            raise RegistryTargetError("registry manifest media type is invalid")
        descriptors = payload.get("manifests")
        if not isinstance(descriptors, list):
            raise RegistryTargetError("registry image index has no manifests")
        image_descriptors: list[dict[str, Any]] = []
        for descriptor in descriptors:
            if not isinstance(descriptor, dict):
                raise RegistryTargetError("registry image index has an invalid descriptor")
            if RegistryClient._is_attestation_descriptor(descriptor):
                continue
            if descriptor.get("mediaType") not in _IMAGE_MANIFEST_MEDIA_TYPES:
                raise RegistryTargetError(
                    "registry image index contains an unsupported manifest descriptor"
                )
            digest = descriptor.get("digest")
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise RegistryTargetError("registry image manifest digest is invalid")
            image_descriptors.append(descriptor)
        if not image_descriptors:
            raise RegistryTargetError("registry image index has no platform manifests")
        return image_descriptors

    def _validate_image_manifest_config_sync(
        self,
        repository: str,
        manifest: Any,
        token: str,
        *,
        expected_revision: str,
        expected_component: str,
        expected_channel: str,
        expected_version: str,
    ) -> None:
        """Validate one image manifest's config labels at the trust boundary."""

        if (
            not isinstance(manifest, dict)
            or manifest.get("schemaVersion") != 2
            or manifest.get("mediaType") not in _IMAGE_MANIFEST_MEDIA_TYPES
        ):
            raise RegistryTargetError("registry image manifest is invalid")
        config = manifest.get("config")
        if not isinstance(config, dict):
            raise RegistryTargetError("registry image manifest has no config descriptor")
        config_media_type = config.get("mediaType")
        config_digest = config.get("digest")
        if config_media_type not in _CONFIG_MEDIA_TYPES:
            raise RegistryTargetError("registry image config media type is invalid")
        if not isinstance(config_digest, str) or not _DIGEST_RE.fullmatch(config_digest):
            raise RegistryTargetError("registry image config digest is invalid")
        config_payload, _ = self._config_response_sync(repository, config_digest, token)
        if not isinstance(config_payload, dict):
            raise RegistryTargetError("registry image config is not an object")
        config_object = config_payload.get("config")
        labels = config_object.get("Labels") if isinstance(config_object, dict) else None
        if not isinstance(labels, dict):
            raise RegistryTargetError("registry image config has no Labels object")
        if labels.get("org.opencontainers.image.revision") != expected_revision:
            raise RegistryTargetError(
                "registry image revision label does not match development target"
            )
        if labels.get("com.sakura-ai.component") != expected_component:
            raise RegistryTargetError("registry image component label is invalid")
        if labels.get("com.sakura-ai.build.channel") != expected_channel:
            raise RegistryTargetError("registry image channel label is invalid")
        if labels.get("org.opencontainers.image.version") != expected_version:
            raise RegistryTargetError("registry image version label is invalid")

    def _image_reference_sync(
        self,
        repository: str,
        reference: str,
        token: str,
        *,
        expected_revision: str,
        expected_component: str,
        expected_channel: str,
        expected_version: str,
    ) -> str:
        """Resolve and validate an image tag, including OCI config labels."""

        payload, headers = self._manifest_response_sync(repository, reference, token)
        try:
            digest = self._manifest_digest(headers)
        except RegistryTargetError as exc:
            raise RegistryTargetError(
                f"{repository}:{reference} did not return a manifest digest"
            ) from exc
        descriptors = self._image_descriptors(payload)
        if len(descriptors) == 1 and descriptors[0] is payload:
            self._validate_image_manifest_config_sync(
                repository,
                payload,
                token,
                expected_revision=expected_revision,
                expected_component=expected_component,
                expected_channel=expected_channel,
                expected_version=expected_version,
            )
            return digest

        for descriptor in descriptors:
            child_digest = str(descriptor["digest"]).lower()
            child, child_headers = self._manifest_response_sync(
                repository, child_digest, token
            )
            try:
                resolved_child_digest = self._manifest_digest(child_headers)
            except RegistryTargetError as exc:
                raise RegistryTargetError(
                    "registry image descriptor manifest has no valid digest"
                ) from exc
            if resolved_child_digest != child_digest:
                raise RegistryTargetError(
                    "registry image descriptor digest does not match its manifest"
                )
            self._validate_image_manifest_config_sync(
                repository,
                child,
                token,
                expected_revision=expected_revision,
                expected_component=expected_component,
                expected_channel=expected_channel,
                expected_version=expected_version,
            )
        return digest

    def _token_sync(self, repository: str) -> str:
        """Obtain a short-lived pull token for one fixed GHCR repository."""

        if repository not in {
            self.repository,
            SANDBOXD_REPOSITORY,
            RUNNER_REPOSITORY,
        }:
            raise RegistryTargetError("registry repository is not trusted")
        registry, path = repository.split("/", 1)
        payload, _ = self._json_sync(
            f"https://{registry}/token?scope=repository:{path}:pull",
            {"Accept": "application/json"},
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RegistryTargetError("registry token response invalid")
        return token

    async def resolve_development_sandbox_pair(
        self, target: DevelopmentTarget
    ) -> DevelopmentSandboxPair:
        """Resolve sandbox images built from exactly ``target.revision``.

        The Web registry catalog supplies the development target's full tag and
        digest.  Sandbox images are published with that same canonical
        ``dev-...-<revision>`` tag plus a revision-only immutable tag.  Require
        both tags to resolve to the same manifest digest for each repository.
        Every OCI/Docker image manifest behind those tags is then followed to
        its config blob and required to carry the target full revision plus
        the server-owned component label. This prevents the updater from
        silently retaining the previous sandbox pair or accepting an image
        rebuilt for another revision/component.
        """

        if not isinstance(target, DevelopmentTarget):
            raise RegistryTargetError("development sandbox target is invalid")
        # Re-run the structural checks here for callers that construct the
        # dataclass directly rather than going through the parser.
        parsed = parse_development_target(target.to_dict())
        revision_tag = f"sha-{parsed.revision}"

        async def resolve(repository: str, component: str) -> str:
            token = await asyncio.to_thread(self._token_sync, repository)
            exact, revision = await asyncio.gather(
                asyncio.to_thread(
                    self._image_reference_sync,
                    repository,
                    parsed.tag,
                    token,
                    expected_revision=parsed.revision,
                    expected_component=component,
                    expected_channel=parsed.channel,
                    expected_version=parsed.version,
                ),
                asyncio.to_thread(
                    self._image_reference_sync,
                    repository,
                    revision_tag,
                    token,
                    expected_revision=parsed.revision,
                    expected_component=component,
                    expected_channel=parsed.channel,
                    expected_version=parsed.version,
                ),
            )
            if exact != revision:
                raise RegistryTargetError(
                    f"{repository} development tags do not resolve to revision "
                    f"{parsed.revision}"
                )
            return f"{repository}@{exact}"

        sandboxd_ref, runner_ref = await asyncio.gather(
            resolve(SANDBOXD_REPOSITORY, "sandboxd"),
            resolve(RUNNER_REPOSITORY, "agent-runner"),
        )
        return DevelopmentSandboxPair(
            revision=parsed.revision,
            sandboxd_image=sandboxd_ref,
            runner_image=runner_ref,
        )

    async def development_sandbox_refs(
        self, target: DevelopmentTarget
    ) -> tuple[str, str]:
        """Compatibility projection of the verified development sandbox pair."""

        pair = await self.resolve_development_sandbox_pair(target)
        return pair.sandboxd_ref, pair.runner_ref

    async def resolve_stable_target(
        self,
        version: str,
        *,
        expected_digest: str | None = None,
    ) -> StableTarget:
        """Resolve one stable version to the current, digest-pinned target.

        A release manifest contains a human-readable ``vX.Y.Z`` tag, but that
        tag is mutable at the registry boundary.  Resolve the tag and the
        moving ``latest`` alias independently and require both to return the
        same manifest digest.  The resulting object is the only stable image
        identity that callers may pass to Docker or persist in a job.
        """

        if not isinstance(version, str) or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            version,
        ) is None:
            raise RegistryTargetError("stable target version is invalid")
        token = await asyncio.to_thread(self._token_sync, self.repository)
        target_digest, latest_digest = await asyncio.gather(
            asyncio.to_thread(self._manifest_sync, f"v{version}", token),
            asyncio.to_thread(self._manifest_sync, "latest", token),
        )
        if expected_digest is not None:
            if not isinstance(expected_digest, str) or not _DIGEST_RE.fullmatch(
                expected_digest
            ):
                raise RegistryTargetError("expected stable digest is invalid")
            if target_digest != expected_digest:
                raise RegistryTargetError(
                    "stable release tag digest does not match the requested target"
                )
        if target_digest != latest_digest:
            raise RegistryTargetError(
                "stable target is not the current latest head"
            )
        return parse_stable_target(
            {
                "channel": "stable",
                "version": version,
                "tag": f"v{version}",
                "digest": target_digest,
            }
        )

    async def verify_target(
        self, target: DevelopmentTarget | StableTarget
    ) -> DevelopmentTarget | StableTarget:
        if isinstance(target, DevelopmentTarget):
            target = parse_development_target(target.to_dict())
        elif isinstance(target, StableTarget):
            target = parse_stable_target(target.to_dict())
        else:
            raise RegistryTargetError("target type is invalid")
        if target.repository != self.repository:
            raise RegistryTargetError("target repository mismatch")
        token = await asyncio.to_thread(self._token_sync, self.repository)
        if target.channel == "development":
            actual = await asyncio.to_thread(
                self._image_reference_sync,
                self.repository,
                target.tag,
                token,
                expected_revision=target.revision,
                expected_component="web",
                expected_channel=target.channel,
                expected_version=target.version,
            )
        else:
            actual = await asyncio.to_thread(self._manifest_sync, target.tag, token)
        if actual != target.digest:
            raise RegistryTargetError("registry manifest digest mismatch")
        # ``edge``/``latest`` are only moving discovery aliases; an update
        # request must still point at the current channel head, never an
        # arbitrary historical tag. Compare the alias independently to close
        # the TOCTOU window between catalog and destructive submission.
        head_tag = "edge" if target.channel == "development" else "latest"
        head_digest = await asyncio.to_thread(self._manifest_sync, head_tag, token)
        if head_digest != target.digest:
            raise RegistryTargetError(
                f"{target.channel} target is not the current {head_tag} head"
            )
        return target


__all__ = [
    "REPOSITORY",
    "RUNNER_REPOSITORY",
    "SANDBOXD_REPOSITORY",
    "DevelopmentSandboxPair",
    "DevelopmentTarget",
    "RegistryClient",
    "RegistryTargetError",
    "StableTarget",
    "parse_development_target",
    "parse_stable_target",
]
