"""Immutable build identity injected by the container build.

The module deliberately has no dependency on deployment configuration.  In a
source checkout the environment variables are absent and the returned
``channel`` is ``source``; an image build supplies the four ``SAKURA_BUILD_*``
variables through the Dockerfile. Image deployments also expose the exact
``SAKURA_AI_IMAGE`` selected by deployment.env so health consumers can verify
the running manifest digest rather than inferring identity from a revision.
"""

from __future__ import annotations

import os
import re

from backend.core.time_service import format_rfc3339, parse_rfc3339

_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST_RE = re.compile(
    r"^.+:[^@\s]+@(?P<digest>sha256:[0-9a-f]{64})$"
)
_CHANNELS = {"stable", "development"}


def _created(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_rfc3339(parse_rfc3339(value))
    except ValueError:
        return None


def _image_digest(value: str | None) -> str | None:
    if not value:
        return None
    match = _IMAGE_DIGEST_RE.fullmatch(value.strip())
    return match.group("digest") if match is not None else None


def get_build_info() -> dict[str, str | None]:
    """Return a safe, JSON-serialisable build identity projection."""

    raw_channel = os.environ.get("SAKURA_BUILD_CHANNEL", "").strip().lower()
    channel = raw_channel if raw_channel in _CHANNELS else "source"
    raw_revision = os.environ.get("SAKURA_BUILD_REVISION", "").strip()
    revision = raw_revision if _REVISION_RE.fullmatch(raw_revision) else None
    created_at = _created(os.environ.get("SAKURA_BUILD_CREATED"))
    return {
        "channel": channel,
        "revision": revision,
        "created_at": created_at,
        "digest": _image_digest(os.environ.get("SAKURA_AI_IMAGE")),
    }


__all__ = ["get_build_info"]
