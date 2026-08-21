"""Strict Semantic Versioning helpers used by the host updater.

The backend has a deliberately small SemVer parser for release discovery.  The
updater keeps the same no-leading-zero policy while also handling the optional
pre-release and build metadata fields defined by SemVer 2.0.0.  No
``packaging`` dependency is needed for this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering
from typing import Any

_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True, slots=True)
class SemVer:
    """A parsed SemVer value.

    ``build`` is retained for validation and round-tripping, but is ignored
    for precedence as required by SemVer 2.0.0.  The small tuple-like helpers
    make the core ``major/minor/patch`` values convenient for callers that
    previously consumed the backend's ``tuple[int, int, int]`` parser.
    """

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    @property
    def core(self) -> tuple[int, int, int]:
        """Return the numeric major/minor/patch tuple."""

        return self.major, self.minor, self.patch

    def __iter__(self):
        """Iterate over the numeric core for backend-compatible use."""

        return iter(self.core)

    def __getitem__(self, index: int) -> int:
        return self.core[index]

    def __len__(self) -> int:
        return 3

    def __str__(self) -> str:
        value = ".".join(str(part) for part in self.core)
        if self.prerelease:
            value += "-" + ".".join(self.prerelease)
        if self.build:
            value += "+" + ".".join(self.build)
        return value

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SemVer):
            # Build metadata does not affect SemVer precedence/equality.
            return self._precedence_key() == other._precedence_key()
        if isinstance(other, tuple) and len(other) == 3:
            return self.core == other and not self.prerelease and not self.build
        return NotImplemented

    def __hash__(self) -> int:
        """Hash the same precedence fields used by ``__eq__``.

        ``dataclass(frozen=True)`` would otherwise generate a hash from every
        field, including build metadata.  That would violate Python's
        equal-objects/same-hash invariant because SemVer build metadata is
        intentionally ignored for equality and precedence.
        """

        return hash(self._precedence_key())

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._precedence_key() < other._precedence_key()

    def _precedence_key(self) -> tuple[Any, ...]:
        """Build a key that follows SemVer's pre-release precedence rules."""

        if not self.prerelease:
            # A normal release has higher precedence than any pre-release of
            # the same numeric core.  ``(1, ())`` sorts after ``(0, ...)``.
            return self.core + (1, ())
        identifiers: list[tuple[int, int | str]] = []
        for identifier in self.prerelease:
            if identifier.isdigit():
                identifiers.append((0, int(identifier)))
            else:
                identifiers.append((1, identifier))
        # A shorter pre-release list has lower precedence when its prefix is
        # equal.  The marker ``0`` keeps all pre-releases below normal builds.
        return self.core + (0, tuple(identifiers))


def parse_semver(version: str) -> SemVer | None:
    """Parse *version* according to SemVer 2.0.0, or return ``None``.

    The parser rejects non-string values, a leading ``v``, whitespace,
    leading zeroes in numeric identifiers, empty identifiers and malformed
    build/pre-release sections.  This mirrors the backend's strict policy and
    prevents PEP 440 values from entering update decisions.
    """

    if not isinstance(version, str):
        return None
    match = _SEMVER_RE.fullmatch(version)
    if match is None:
        return None

    prerelease_text, build_text = match.group(4), match.group(5)
    prerelease = tuple(prerelease_text.split(".")) if prerelease_text else ()
    # Numeric pre-release identifiers may not contain leading zeroes.  The
    # main regex already guarantees their character set and non-empty value.
    if any(
        identifier.isdigit() and len(identifier) > 1 and identifier.startswith("0")
        for identifier in prerelease
    ):
        return None

    build = tuple(build_text.split(".")) if build_text else ()
    return SemVer(
        major=int(match.group(1)),
        minor=int(match.group(2)),
        patch=int(match.group(3)),
        prerelease=prerelease,
        build=build,
    )


def is_newer_version(current: str, candidate: str) -> bool:
    """Return whether valid *candidate* is newer than valid *current*.

    Invalid values are never considered newer.  Build metadata is ignored for
    precedence, so ``1.0.0+host`` is not newer than ``1.0.0``.
    """

    current_version = parse_semver(current)
    candidate_version = parse_semver(candidate)
    if current_version is None or candidate_version is None:
        return False
    return candidate_version > current_version


__all__ = ["SemVer", "is_newer_version", "parse_semver"]
