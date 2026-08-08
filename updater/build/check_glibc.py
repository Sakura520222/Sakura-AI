"""Check GLIBC needs of the outer PyInstaller ELF bootloader only.

PyInstaller onefile embeds a Python CArchive after its outer ELF bootloader.
This checker deliberately invokes ``readelf`` on the final file and parses only
that outer ELF's version-needs output; it never extracts or scans the embedded
CArchive. The build image remains the compatibility strategy, while this
ceiling is a pollution guard against an accidentally newer toolchain.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

GLIBC_CEILING = (2, 31)
_GLIBC_NEED_RE = re.compile(r"\bGLIBC_(\d+)\.(\d+)")


def parse_glibc_version_needs(readelf_output: str) -> list[tuple[int, int]]:
    """Return numeric GLIBC version needs, excluding non-numeric private tags."""
    versions = {
        (int(major), int(minor))
        for major, minor in _GLIBC_NEED_RE.findall(readelf_output)
    }
    return sorted(versions)


def _read_outer_elf_version_info(binary: Path) -> str:
    """Read version needs from the supplied outer ELF/bootloader path."""
    try:
        with binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                raise ValueError(f"not an ELF file: {binary}")
    except OSError as exc:
        raise ValueError(f"cannot read outer ELF {binary}: {exc}") from exc

    try:
        result = subprocess.run(
            ["readelf", "--version-info", "--wide", str(binary)],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise RuntimeError(f"cannot execute readelf for outer ELF gate: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "readelf failed"
        raise ValueError(f"readelf could not inspect outer ELF {binary}: {detail}")
    return result.stdout


def check_outer_elf(binary: Path) -> tuple[int, int]:
    """Validate the outer onefile bootloader and return its maximum GLIBC need."""
    output = _read_outer_elf_version_info(binary)
    versions = parse_glibc_version_needs(output)
    if not versions:
        raise ValueError(
            "outer ELF/bootloader has no numeric GLIBC version needs; "
            "refusing to treat it as compatible"
        )
    maximum = max(versions)
    if maximum > GLIBC_CEILING:
        raise ValueError(
            f"outer ELF/bootloader GLIBC ceiling exceeded: "
            f"{maximum[0]}.{maximum[1]} > {GLIBC_CEILING[0]}.{GLIBC_CEILING[1]}"
        )
    return maximum


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="check final onefile outer ELF/bootloader GLIBC needs"
    )
    parser.add_argument("binary", type=Path)
    args = parser.parse_args(argv)
    try:
        maximum = check_outer_elf(args.binary)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        "outer ELF/bootloader GLIBC maximum: "
        f"{maximum[0]}.{maximum[1]} "
        f"(ceiling {GLIBC_CEILING[0]}.{GLIBC_CEILING[1]})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
