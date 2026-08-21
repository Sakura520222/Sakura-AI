"""Three-way merge packaged YAML defaults into persistent container config.

The managed file list is currently empty: ``strategies.yaml`` and
``labels.yaml`` were migrated into the ``app_config`` table (unified config
store, 2026-08-16), whose leaf-level deep merge replaces this script's job for
those files.  The merge machinery is kept so a future packaged YAML can opt in
by adding its filename to ``CONFIG_FILENAMES``.  Sensitive and other mutable
files (most importantly ``connection.json``) are deliberately outside this
merge contract.  A hidden baseline in the persistent config volume records the
previous packaged defaults so image upgrades can distinguish an unchanged
default from an administrator override.
"""

from __future__ import annotations

import argparse
import copy
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# strategies.yaml/labels.yaml 已由统一配置存储接管（DB 深度合并语义），
# 此处清单为空；如需新的打包默认 YAML，在此追加文件名即可复用合并机制。
CONFIG_FILENAMES: tuple[str, ...] = ()
BASELINE_DIRNAME = ".sakura-ai-packaged-baseline"
_MISSING = object()


class ConfigMergeError(RuntimeError):
    """A packaged or persistent configuration cannot be merged safely."""


def _load_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigMergeError(
            f"cannot parse YAML configuration {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise ConfigMergeError(f"YAML configuration root must be a mapping: {path}")
    return value


def _same_value(left: Any, right: Any) -> bool:
    """Compare YAML values without treating bools and integers as equivalent."""

    if left is _MISSING or right is _MISSING:
        return left is right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _same_value(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _same_value(item, other) for item, other in zip(left, right, strict=True)
        )
    return left == right


def _merge_without_baseline(default: Any, current: Any, *, path: str) -> Any:
    """Legacy two-way merge used once when an old volume has no baseline.

    Existing values are treated as user-owned.  Mappings receive new default
    keys recursively; lists/scalars remain user-owned as a whole.  A mapping
    versus scalar/list conflict is unsafe because nested defaults cannot be
    added without replacing the user's value, so startup fails closed.
    """

    if isinstance(default, dict):
        if not isinstance(current, dict):
            raise ConfigMergeError(f"YAML type conflict at {path}: expected mapping")
        merged = copy.deepcopy(default)
        for key, current_value in current.items():
            if key not in default:
                merged[key] = copy.deepcopy(current_value)
            else:
                merged[key] = _merge_without_baseline(
                    default[key], current_value, path=f"{path}.{key}"
                )
        return merged
    if isinstance(default, list):
        if not isinstance(current, list):
            raise ConfigMergeError(f"YAML type conflict at {path}: expected list")
        return copy.deepcopy(current)
    if default is None:
        return copy.deepcopy(current)
    if isinstance(default, bool):
        expected = bool
    elif isinstance(default, (int, float)) and not isinstance(default, bool):
        expected = type(default)
    elif isinstance(default, str):
        expected = str
    else:
        expected = type(default)
    if not isinstance(current, expected) or (
        expected is int and isinstance(current, bool)
    ):
        raise ConfigMergeError(
            f"YAML type conflict at {path}: expected {expected.__name__}"
        )
    return copy.deepcopy(current)


def _three_way_merge(old: Any, current: Any, new: Any, *, path: str) -> Any:
    """Merge one YAML value using old baseline, current user file, and new defaults."""

    if old is _MISSING:
        if current is _MISSING:
            return copy.deepcopy(new)
        if new is _MISSING:
            return copy.deepcopy(current)
        return _merge_without_baseline(new, current, path=path)

    if current is _MISSING:
        # A missing current key is an explicit user deletion.  It must not be
        # recreated merely because the packaged default still contains it.
        return _MISSING

    if isinstance(old, dict) and isinstance(current, dict):
        if not isinstance(new, dict):
            if _same_value(old, current):
                if new is _MISSING:
                    return _MISSING
                raise ConfigMergeError(
                    f"YAML type conflict at {path}: expected mapping"
                )
            return copy.deepcopy(current)
        merged: dict[Any, Any] = {}
        keys: list[Any] = []
        for mapping in (new, old, current):
            for key in mapping:
                if key not in keys:
                    keys.append(key)
        for key in keys:
            child = _three_way_merge(
                old.get(key, _MISSING),
                current.get(key, _MISSING),
                new.get(key, _MISSING),
                path=f"{path}.{key}",
            )
            if child is not _MISSING:
                merged[key] = child
        return merged

    if _same_value(old, current):
        if new is _MISSING:
            # Packaged key was intentionally removed and the user had not
            # changed it since the previous baseline.
            return _MISSING
        if isinstance(new, dict) != isinstance(old, dict):
            raise ConfigMergeError(f"YAML type conflict at {path}")
        return copy.deepcopy(new)

    # Any changed scalar/list or changed mapping shape is a user override.
    # Preserve it even when the new packaged version removes or reshapes it.
    return copy.deepcopy(current)


def _render_yaml(value: dict[str, Any], *, path: Path) -> str:
    try:
        rendered = yaml.safe_dump(
            value,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        parsed = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        raise ConfigMergeError(
            f"cannot serialize YAML configuration {path}: {exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigMergeError(f"serialized YAML root must be a mapping: {path}")
    return rendered


@dataclass(frozen=True)
class _WritePlan:
    path: Path
    contents: str
    mode: int


def _fsync_directory(directory: Path) -> None:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    if not directory_flag:
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY | directory_flag)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _mode_for(path: Path, *, default: int = 0o644) -> int:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        return default


def _write_batch(plans: list[_WritePlan]) -> None:
    """Stage and atomically replace a batch, rolling back on replacement errors."""

    if not plans:
        return
    temp_paths: list[Path] = []
    backups: dict[Path, Path | None] = {}
    try:
        for plan in plans:
            plan.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{plan.path.name}.", suffix=".tmp", dir=str(plan.path.parent)
            )
            temp_path = Path(temp_name)
            temp_paths.append(temp_path)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(plan.contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temp_path, plan.mode)

        for plan in plans:
            if not plan.path.exists():
                backups[plan.path] = None
                continue
            fd, backup_name = tempfile.mkstemp(
                prefix=f".{plan.path.name}.", suffix=".bak", dir=str(plan.path.parent)
            )
            backup_path = Path(backup_name)
            with os.fdopen(fd, "wb") as stream:
                stream.write(plan.path.read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(backup_path, _mode_for(plan.path))
            backups[plan.path] = backup_path

        for plan, temp_path in zip(plans, temp_paths, strict=True):
            os.replace(temp_path, plan.path)
        for plan in plans:
            _fsync_directory(plan.path.parent)
    except OSError as exc:
        for plan in reversed(plans):
            backup = backups.get(plan.path, _MISSING)
            if backup is _MISSING:
                continue
            try:
                if backup is None:
                    plan.path.unlink(missing_ok=True)
                elif backup.exists():
                    os.replace(backup, plan.path)
            except OSError:
                # Preserve the original failure; the next startup still
                # fail-closes if a runtime file cannot be parsed.
                pass
        raise ConfigMergeError(
            f"cannot atomically update packaged configuration: {exc}"
        ) from exc
    finally:
        for temp_path in temp_paths:
            temp_path.unlink(missing_ok=True)
        for backup_path in backups.values():
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)


def merge_packaged_defaults(config_dir: Path, defaults_dir: Path) -> list[Path]:
    """Three-way merge packaged defaults and update the persistent baseline."""

    baseline_dir = config_dir / BASELINE_DIRNAME
    plans: list[_WritePlan] = []
    changed: list[Path] = []
    for filename in CONFIG_FILENAMES:
        default_path = defaults_dir / filename
        config_path = config_dir / filename
        baseline_path = baseline_dir / filename
        if not default_path.is_file():
            raise ConfigMergeError(f"packaged default is missing: {default_path}")
        new_defaults = _load_mapping(default_path)
        old_baseline = (
            _load_mapping(baseline_path) if baseline_path.is_file() else _MISSING
        )
        current = _load_mapping(config_path) if config_path.is_file() else _MISSING

        if current is _MISSING:
            merged = copy.deepcopy(new_defaults)
        else:
            merged = _three_way_merge(
                old_baseline,
                current,
                new_defaults,
                path=filename,
            )
        if merged is _MISSING or not isinstance(merged, dict):
            raise ConfigMergeError(f"merged YAML root must be a mapping: {config_path}")

        if current is _MISSING or not _same_value(merged, current):
            plans.append(
                _WritePlan(
                    config_path,
                    _render_yaml(merged, path=config_path),
                    _mode_for(config_path),
                )
            )
            changed.append(config_path)
        if old_baseline is _MISSING or not _same_value(old_baseline, new_defaults):
            plans.append(
                _WritePlan(
                    baseline_path,
                    _render_yaml(new_defaults, path=baseline_path),
                    _mode_for(baseline_path, default=0o600),
                )
            )
            changed.append(baseline_path)

    _write_batch(plans)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, required=True)
    parser.add_argument("--defaults-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        changed = merge_packaged_defaults(args.config_dir, args.defaults_dir)
    except ConfigMergeError as exc:
        print(
            f"Sakura AI config initialization failed closed: {exc}", file=os.sys.stderr
        )
        return 78
    for path in changed:
        print(f"Sakura AI packaged config merged: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
