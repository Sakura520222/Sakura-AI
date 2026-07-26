"""Provider-native reasoning artifact capture policy."""

from __future__ import annotations

from dataclasses import dataclass

REASONING_UNAVAILABLE = "unavailable"
REASONING_OMITTED = "omitted"
REASONING_SUMMARIZED = "summarized"
REASONING_PROVIDER_EXPOSED = "provider_exposed"
REASONING_ENCRYPTED_OPAQUE = "encrypted_opaque"
VALID_AVAILABILITY = frozenset(
    {
        REASONING_UNAVAILABLE,
        REASONING_OMITTED,
        REASONING_SUMMARIZED,
        REASONING_PROVIDER_EXPOSED,
        REASONING_ENCRYPTED_OPAQUE,
    }
)

CAPTURE_METADATA_ONLY = "metadata_only"
CAPTURE_SAFE_SUMMARY = "safe_summary"
CAPTURE_ARTIFACT = "artifact"
VALID_CAPTURE_MODES = frozenset(
    {CAPTURE_METADATA_ONLY, CAPTURE_SAFE_SUMMARY, CAPTURE_ARTIFACT}
)

VISIBILITY_INTERNAL = "internal"
VISIBILITY_ADMIN_ONLY = "admin_only"
VALID_VISIBILITY = frozenset({VISIBILITY_INTERNAL, VISIBILITY_ADMIN_ONLY})


@dataclass(frozen=True)
class ReasoningCapturePolicy:
    """Configuration-driven retention and display policy."""

    capture_mode: str = CAPTURE_METADATA_ONLY
    summary_visibility: str = VISIBILITY_ADMIN_ONLY
    artifact_visibility: str = VISIBILITY_ADMIN_ONLY
    provider_allowlist: frozenset[str] = frozenset()
    protocol_allowlist: frozenset[str] = frozenset()
    encryption_required: bool = True
    retention_days: int | None = None

    def __post_init__(self) -> None:
        if self.capture_mode not in VALID_CAPTURE_MODES:
            raise ValueError(f"unknown capture_mode: {self.capture_mode}")
        if self.summary_visibility not in VALID_VISIBILITY:
            raise ValueError(f"unknown summary_visibility: {self.summary_visibility}")
        if self.artifact_visibility not in VALID_VISIBILITY:
            raise ValueError(f"unknown artifact_visibility: {self.artifact_visibility}")
        if self.capture_mode == CAPTURE_ARTIFACT and not self.encryption_required:
            raise ValueError("artifact capture requires encryption_required=True")
        if self.retention_days is not None:
            if isinstance(self.retention_days, bool) or self.retention_days <= 0:
                raise ValueError("retention_days must be a positive integer or None")

    def allows_provider(self, provider: str, protocol_family: str) -> bool:
        """Return whether provider and protocol are explicitly permitted."""
        return (
            (not self.provider_allowlist or provider in self.provider_allowlist)
            and (
                not self.protocol_allowlist
                or protocol_family in self.protocol_allowlist
            )
        )

    def should_persist_payload(
        self,
        availability: str,
        provider: str,
        protocol_family: str,
    ) -> bool:
        """Return whether a provider-returned payload may be retained."""
        if availability not in VALID_AVAILABILITY:
            raise ValueError(f"unknown availability: {availability}")
        if self.capture_mode == CAPTURE_METADATA_ONLY:
            return False
        if not self.allows_provider(provider, protocol_family):
            return False
        if availability == REASONING_ENCRYPTED_OPAQUE:
            return self.capture_mode == CAPTURE_ARTIFACT
        return availability in (REASONING_SUMMARIZED, REASONING_PROVIDER_EXPOSED)

    def may_display(
        self,
        availability: str,
        is_admin: bool,
        *,
        visibility: str | None = None,
        provider: str = "",
        protocol_family: str = "",
    ) -> bool:
        """Return whether a safe provider projection may be displayed."""
        if availability not in (REASONING_SUMMARIZED, REASONING_PROVIDER_EXPOSED):
            return False
        if not is_admin:
            return False
        expected_visibility = self.summary_visibility if visibility is None else visibility
        return expected_visibility in VALID_VISIBILITY and self.allows_provider(
            provider, protocol_family
        )


def build_compatibility_key(
    provider_family: str, protocol_family: str, model_family: str, endpoint_scope: str
) -> str:
    """Build the exact provider replay compatibility key."""
    return f"{provider_family}|{protocol_family}|{model_family}|{endpoint_scope}"


def safe_summary_or_none(
    availability: str,
    payload: str | None,
    policy: ReasoningCapturePolicy,
    *,
    provider_family: str = "",
    protocol_family: str = "",
    visibility: str | None = None,
) -> str | None:
    """Return only an allowlisted provider summary, never opaque content."""
    if payload is None or not policy.may_display(
        availability,
        True,
        visibility=visibility,
        provider=provider_family,
        protocol_family=protocol_family,
    ):
        return None
    return payload
