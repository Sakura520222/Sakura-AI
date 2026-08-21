"""Release ``update-manifest.json`` v1 validation.

The manifest is an updater trust boundary.  Keep this parser deliberately
strict: accepting an unknown shape here could make a later preflight consume
an untrusted image or updater asset.  P0 publishes only stable image releases
and the updater protocol is fixed at version 1.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import PROTOCOL_VERSION
from .semver import parse_semver

MANIFEST_SCHEMA_VERSION = 1
MIN_UPGRADE_FROM = "0.0.0"
RELEASE_CHANNEL = "stable"
IMAGE_REPOSITORY = "ghcr.io/sakura520222/sakura-ai"
UPDATER_ASSET_AMD64 = "sakura-ai-updater-linux-amd64"
UPDATER_ASSET_ARM64 = "sakura-ai-updater-linux-arm64"

_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ManifestError(ValueError):
    """Raised when a release manifest does not satisfy the v1 schema."""


@dataclass(frozen=True, slots=True)
class UpdaterManifest:
    """Updater protocol and release asset names from a v1 manifest."""

    protocol_version: int
    asset_linux_amd64: str
    asset_linux_arm64: str


@dataclass(frozen=True, slots=True)
class Manifest:
    """Validated release manifest v1."""

    schema_version: int
    version: str
    channel: str
    min_upgrade_from: str
    image: str
    updater: UpdaterManifest

    @property
    def protocol_version(self) -> int:
        """Convenience access to the nested updater protocol version."""

        return self.updater.protocol_version

    @property
    def asset_linux_amd64(self) -> str:
        """Convenience access to the nested amd64 asset name."""

        return self.updater.asset_linux_amd64

    @property
    def asset_linux_arm64(self) -> str:
        """Convenience access to the nested arm64 asset name."""

        return self.updater.asset_linux_arm64


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestError(f"{label} must be an object")
    return value


def _require_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{label} must be a non-empty string")
    return value


def _require_integer(value: Any, label: str) -> int:
    # bool is an int subclass, but is never a valid schema integer here.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestError(f"{label} must be an integer")
    return value


def _validate_asset_name(value: Any, label: str, expected: str) -> str:
    asset = _require_string(value, label)
    if _ASSET_NAME_RE.fullmatch(asset) is None:
        raise ManifestError(f"{label} contains invalid characters")
    if asset != expected:
        raise ManifestError(f"{label} must be {expected!r}")
    return asset


def parse_manifest(
    data: Mapping[str, Any] | Any,
    *,
    expected_version: str | None = None,
) -> Manifest:
    """Validate and parse a release manifest v1.

    ``expected_version`` is supplied by the release/tag being checked.  When
    present it must itself be strict SemVer and match ``data['version']``;
    omitting it is supported for callers that only need schema validation.
    P0 intentionally fixes ``min_upgrade_from`` to ``0.0.0``.
    """

    root = _require_mapping(data, "manifest")
    required_keys = {
        "schema_version",
        "version",
        "channel",
        "min_upgrade_from",
        "image",
        "updater",
    }
    # Reject both missing and unknown keys.  In particular, v1 does not allow
    # asset_linux_* at the top level; both assets belong under ``updater``.
    if set(root) != required_keys:
        missing = sorted(required_keys - set(root))
        unknown = sorted(set(root) - required_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        raise ManifestError(
            "invalid manifest keys" + (f" ({'; '.join(details)})" if details else "")
        )

    schema_version = _require_integer(root["schema_version"], "schema_version")
    if schema_version != MANIFEST_SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema_version: {schema_version}")

    version = _require_string(root["version"], "version")
    if parse_semver(version) is None:
        raise ManifestError("version must be strict SemVer")
    if expected_version is not None:
        if parse_semver(expected_version) is None:
            raise ManifestError("expected_version must be strict SemVer")
        if version != expected_version:
            raise ManifestError(
                f"manifest version {version!r} does not match expected {expected_version!r}"
            )

    channel = _require_string(root["channel"], "channel")
    if channel != RELEASE_CHANNEL:
        raise ManifestError(f"unsupported channel: {channel!r}")

    min_upgrade_from = _require_string(root["min_upgrade_from"], "min_upgrade_from")
    if parse_semver(min_upgrade_from) is None:
        raise ManifestError("min_upgrade_from must be strict SemVer")
    if min_upgrade_from != MIN_UPGRADE_FROM:
        raise ManifestError("P0 min_upgrade_from must be '0.0.0'")

    image = _require_string(root["image"], "image")
    expected_image = f"{IMAGE_REPOSITORY}:v{version}"
    if image != expected_image:
        raise ManifestError(
            f"image must be the version tag for this release: {expected_image!r}"
        )

    updater = _require_mapping(root["updater"], "updater")
    updater_keys = {
        "protocol_version",
        "asset_linux_amd64",
        "asset_linux_arm64",
    }
    if set(updater) != updater_keys:
        missing = sorted(updater_keys - set(updater))
        unknown = sorted(set(updater) - updater_keys)
        details: list[str] = []
        if missing:
            details.append(f"missing={','.join(missing)}")
        if unknown:
            details.append(f"unknown={','.join(unknown)}")
        raise ManifestError(
            "invalid updater keys" + (f" ({'; '.join(details)})" if details else "")
        )

    protocol_version = _require_integer(
        updater["protocol_version"], "updater.protocol_version"
    )
    if protocol_version != PROTOCOL_VERSION:
        raise ManifestError(f"unsupported updater protocol_version: {protocol_version}")

    parsed_updater = UpdaterManifest(
        protocol_version=protocol_version,
        asset_linux_amd64=_validate_asset_name(
            updater["asset_linux_amd64"],
            "updater.asset_linux_amd64",
            UPDATER_ASSET_AMD64,
        ),
        asset_linux_arm64=_validate_asset_name(
            updater["asset_linux_arm64"],
            "updater.asset_linux_arm64",
            UPDATER_ASSET_ARM64,
        ),
    )
    return Manifest(
        schema_version=schema_version,
        version=version,
        channel=channel,
        min_upgrade_from=min_upgrade_from,
        image=image,
        updater=parsed_updater,
    )


__all__ = [
    "IMAGE_REPOSITORY",
    "MANIFEST_SCHEMA_VERSION",
    "MIN_UPGRADE_FROM",
    "RELEASE_CHANNEL",
    "UPDATER_ASSET_AMD64",
    "UPDATER_ASSET_ARM64",
    "Manifest",
    "ManifestError",
    "UpdaterManifest",
    "parse_manifest",
]
