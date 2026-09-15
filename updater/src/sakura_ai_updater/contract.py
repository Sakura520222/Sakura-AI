"""Shared, dependency-free deployment contract for publisher, Web and host."""

import json
import re

DEPLOYMENT_CAPABILITIES = frozenset({
    "three-image-transaction-v1", "deployment-reconcile-v1", "deployment-manifest-v1",
})
MANIFEST_ANNOTATION = "com.sakura-ai.deployment.v1"
REPOSITORIES = {
    "web": "ghcr.io/sakura520222/sakura-ai",
    "sandboxd": "ghcr.io/sakura520222/sakura-ai-sandboxd",
    "runner": "ghcr.io/sakura520222/sakura-ai-agent-runner",
}


def compatibility(envelope):
    """Protocol reachability does not establish deployment compatibility."""
    envelope = envelope if isinstance(envelope, dict) else {}
    capabilities = envelope.get("capabilities")
    valid = isinstance(capabilities, list) and all(isinstance(x, str) for x in capabilities)
    missing = sorted(DEPLOYMENT_CAPABILITIES - set(capabilities if valid else []))
    return {
        "compatible": type(envelope.get("protocol_version")) is int
        and envelope["protocol_version"] == 1 and not missing,
        "missing_capabilities": missing,
    }


def parse_deployment_manifest(payload, *, channel, version, revision):
    """Read the complete contract bound into the Web OCI index digest.

    The Web platform descriptors are the Web identity; the annotation pins the
    two other components. No circular self-digest and no independently moving
    sandbox tags are needed.
    """
    try:
        manifest = json.loads(payload["annotations"][MANIFEST_ANNOTATION])
        if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or manifest["channel"] != channel or manifest["version"] != version
            or manifest["revision"] != revision
            or not re.fullmatch(r"[0-9a-f]{40}", revision)):
            raise ValueError("deployment manifest identity mismatch")
        for component in ("sandboxd", "runner"):
            value = manifest[component + "_image"]
            if not isinstance(value, str) or not re.fullmatch(
                re.escape(REPOSITORIES[component]) + r"@sha256:[0-9a-f]{64}", value
            ):
                raise ValueError("deployment manifest component identity invalid")
        return manifest
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("complete deployment manifest required") from exc
