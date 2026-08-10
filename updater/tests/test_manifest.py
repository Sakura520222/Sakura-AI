"""Release manifest v1 schema contracts."""

from __future__ import annotations

from copy import deepcopy

import pytest
from sakura_ai_updater.manifest import ManifestError, parse_manifest

VALID_MANIFEST = {
    "schema_version": 1,
    "version": "3.1.0",
    "channel": "stable",
    "min_upgrade_from": "0.0.0",
    "image": "ghcr.io/sakura520222/sakura-ai:v3.1.0",
    "updater": {
        "protocol_version": 1,
        "asset_linux_amd64": "sakura-ai-updater-linux-amd64",
        "asset_linux_arm64": "sakura-ai-updater-linux-arm64",
    },
}


def test_parse_manifest_accepts_nested_updater_assets():
    manifest = parse_manifest(VALID_MANIFEST, expected_version="3.1.0")

    assert manifest.schema_version == 1
    assert manifest.version == "3.1.0"
    assert manifest.min_upgrade_from == "0.0.0"
    assert manifest.image.endswith(":v3.1.0")
    assert manifest.updater.protocol_version == 1
    assert manifest.updater.asset_linux_amd64 == "sakura-ai-updater-linux-amd64"
    assert manifest.updater.asset_linux_arm64 == "sakura-ai-updater-linux-arm64"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 2),
        ("version", "3.1"),
        ("version", "03.1.0"),
        ("channel", "beta"),
        ("min_upgrade_from", "3.0.0"),
        ("image", "ghcr.io/sakura520222/sakura-ai:latest"),
    ],
)
def test_parse_manifest_rejects_invalid_top_level_values(field: str, value):
    payload = deepcopy(VALID_MANIFEST)
    payload[field] = value

    with pytest.raises(ManifestError):
        parse_manifest(payload, expected_version="3.1.0")


def test_parse_manifest_rejects_version_mismatch():
    with pytest.raises(ManifestError, match="does not match"):
        parse_manifest(VALID_MANIFEST, expected_version="3.2.0")


@pytest.mark.parametrize(
    "field",
    ["protocol_version", "asset_linux_amd64", "asset_linux_arm64"],
)
def test_parse_manifest_rejects_invalid_nested_updater_values(field: str):
    payload = deepcopy(VALID_MANIFEST)
    payload["updater"][field] = 0 if field == "protocol_version" else ""

    with pytest.raises(ManifestError):
        parse_manifest(payload, expected_version="3.1.0")


def test_parse_manifest_rejects_top_level_updater_asset_fields():
    payload = deepcopy(VALID_MANIFEST)
    payload["asset_linux_amd64"] = "sakura-ai-updater-linux-amd64"
    payload["asset_linux_arm64"] = "sakura-ai-updater-linux-arm64"

    with pytest.raises(ManifestError, match="invalid manifest keys"):
        parse_manifest(payload, expected_version="3.1.0")


@pytest.mark.parametrize(
    "updater_patch",
    [
        {"protocol_version": 2},
        {"asset_linux_amd64": "other"},
        {"asset_linux_arm64": "../escape"},
    ],
)
def test_parse_manifest_rejects_incompatible_updater_schema(updater_patch: dict):
    payload = deepcopy(VALID_MANIFEST)
    payload["updater"].update(updater_patch)

    with pytest.raises(ManifestError):
        parse_manifest(payload, expected_version="3.1.0")


def test_parse_manifest_rejects_unknown_schema_keys():
    payload = deepcopy(VALID_MANIFEST)
    payload["future_field"] = True

    with pytest.raises(ManifestError):
        parse_manifest(payload, expected_version="3.1.0")
