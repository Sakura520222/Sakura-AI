"""Contracts for packaged-default merging in the production image."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

import scripts.merge_packaged_config as merge_module
from scripts.merge_packaged_config import (
    BASELINE_DIRNAME,
    ConfigMergeError,
    merge_packaged_defaults,
)

ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
COMPOSE = ROOT / "docker" / "docker-compose.prod.yml"


def _write_yaml(path: Path, value: object) -> None:
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_dockerfile_keeps_defaults_outside_persistent_config_volume():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY config ./config-defaults" in dockerfile
    assert "COPY config ./config\n" not in dockerfile
    assert 'ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]' in dockerfile
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    mounts = compose["services"]["web"]["volumes"]
    assert "config_data:/app/config" in mounts


def test_entrypoint_invokes_merge_before_application_command():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert '"$python_bin" "$merge_script" --config-dir "$config_dir"' in script
    assert "exec \"$@\"" in script
    assert "source " not in script
    assert "eval " not in script


def test_entrypoint_runs_merge_and_seeds_only_missing_files():
    python_bin = "/usr/bin/python3"
    command = rf"""
set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/defaults" "$work/config"
printf 'root:\n  baseline: one\n  added: from-image\n' > "$work/defaults/strategies.yaml"
printf 'labels:\n  fresh:\n    color: "abcdef"\n' > "$work/defaults/labels.yaml"
printf 'root:\n  baseline: user\n' > "$work/config/strategies.yaml"
printf '{{"database_url":"mysql://preserve"}}\n' > "$work/config/connection.json"
SAKURA_CONFIG_DIR="$work/config" \
SAKURA_DEFAULT_CONFIG_DIR="$work/defaults" \
SAKURA_CONFIG_MERGE_SCRIPT="$PWD/scripts/merge_packaged_config.py" \
SAKURA_CONFIG_PYTHON="{python_bin}" \
bash scripts/docker-entrypoint.sh true
grep -q 'baseline: user' "$work/config/strategies.yaml"
grep -q 'added: from-image' "$work/config/strategies.yaml"
grep -q 'fresh:' "$work/config/labels.yaml"
grep -q 'mysql://preserve' "$work/config/connection.json"
"""
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={
            **os.environ,
            "TERM": "dumb",
        },
        input=command.encode(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout.decode(errors="replace") + result.stderr.decode(
        errors="replace"
    )


def test_upgrade_merge_preserves_overrides_adds_defaults_and_is_idempotent(tmp_path):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(
        defaults / "strategies.yaml",
        {"strategies": {"quick": {"conditions": {"max_files": 5}}}},
    )
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "d73a4a"}}})
    (config / "connection.json").write_text(
        '{"database_url":"mysql://user:secret"}\n', encoding="utf-8"
    )

    merge_packaged_defaults(config, defaults)
    _write_yaml(
        config / "strategies.yaml",
        {
            "strategies": {
                "quick": {"conditions": {"max_files": 9}},
                "custom": {"prompt": "keep me"},
            }
        },
    )
    (defaults / "strategies.yaml").write_text(
        "strategies:\n  quick:\n    conditions:\n      max_files: 5\n      max_lines: 2000\n  standard:\n    prompt: new\n",
        encoding="utf-8",
    )
    before = (config / "strategies.yaml").read_text(encoding="utf-8")
    changed = merge_packaged_defaults(config, defaults)
    merged = yaml.safe_load((config / "strategies.yaml").read_text(encoding="utf-8"))
    assert (config / "labels.yaml").is_file()
    assert (config / "connection.json").read_text(encoding="utf-8") == (
        '{"database_url":"mysql://user:secret"}\n'
    )
    assert (config / "strategies.yaml") in changed
    assert merged["strategies"]["quick"]["conditions"] == {
        "max_files": 9,
        "max_lines": 2000,
    }
    assert merged["strategies"]["custom"] == {"prompt": "keep me"}
    assert merged["strategies"]["standard"] == {"prompt": "new"}
    after = (config / "strategies.yaml").read_text(encoding="utf-8")
    assert after != before
    assert merge_packaged_defaults(config, defaults) == []
    assert (config / "strategies.yaml").read_text(encoding="utf-8") == after


def test_three_way_merge_updates_unchanged_scalars_and_lists(tmp_path):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(
        defaults / "strategies.yaml",
        {"review": {"limit": 5, "extensions": [".py"]}},
    )
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "red"}}})
    merge_packaged_defaults(config, defaults)

    _write_yaml(defaults / "strategies.yaml", {"review": {"limit": 10, "extensions": [".py", ".go"]}})
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "blue"}}})
    merge_packaged_defaults(config, defaults)
    current = yaml.safe_load((config / "strategies.yaml").read_text(encoding="utf-8"))
    labels = yaml.safe_load((config / "labels.yaml").read_text(encoding="utf-8"))
    assert current == {"review": {"limit": 10, "extensions": [".py", ".go"]}}
    assert labels == {"labels": {"bug": {"color": "blue"}}}
    baseline = yaml.safe_load(
        (config / BASELINE_DIRNAME / "strategies.yaml").read_text(encoding="utf-8")
    )
    assert baseline == current


def test_three_way_merge_preserves_changed_scalar_and_list_overrides(tmp_path):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(defaults / "strategies.yaml", {"review": {"limit": 5, "extensions": [".py"]}})
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "red"}}})
    merge_packaged_defaults(config, defaults)
    _write_yaml(
        config / "strategies.yaml",
        {"review": {"limit": 7, "extensions": [".rs"]}},
    )
    _write_yaml(defaults / "strategies.yaml", {"review": {"limit": 10, "extensions": [".py", ".go"]}})
    merge_packaged_defaults(config, defaults)
    current = yaml.safe_load((config / "strategies.yaml").read_text(encoding="utf-8"))
    assert current == {"review": {"limit": 7, "extensions": [".rs"]}}


def test_three_way_merge_removes_unchanged_deleted_default_but_keeps_user_override(tmp_path):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(
        defaults / "strategies.yaml",
        {"review": {"removed": "old", "customizable": "old"}},
    )
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "red"}}})
    merge_packaged_defaults(config, defaults)
    current = yaml.safe_load((config / "strategies.yaml").read_text(encoding="utf-8"))
    current["review"]["customizable"] = "user"
    _write_yaml(config / "strategies.yaml", current)
    _write_yaml(defaults / "strategies.yaml", {"review": {"customizable": "new"}})
    merge_packaged_defaults(config, defaults)
    merged = yaml.safe_load((config / "strategies.yaml").read_text(encoding="utf-8"))
    assert merged == {"review": {"customizable": "user"}}


def test_legacy_volume_without_baseline_preserves_values_and_records_baseline(tmp_path):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(defaults / "strategies.yaml", {"review": {"limit": 10, "new": True}})
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "red"}}})
    _write_yaml(config / "strategies.yaml", {"review": {"limit": 5}})
    _write_yaml(config / "labels.yaml", {"labels": {"legacy": {"color": "blue"}}})
    merge_packaged_defaults(config, defaults)
    strategies = yaml.safe_load((config / "strategies.yaml").read_text(encoding="utf-8"))
    labels = yaml.safe_load((config / "labels.yaml").read_text(encoding="utf-8"))
    assert strategies == {"review": {"limit": 5, "new": True}}
    assert labels == {
        "labels": {"bug": {"color": "red"}, "legacy": {"color": "blue"}}
    }
    assert (config / BASELINE_DIRNAME / "strategies.yaml").is_file()
    assert (config / BASELINE_DIRNAME / "labels.yaml").is_file()


def test_batch_replace_failure_rolls_back_runtime_and_baseline(tmp_path, monkeypatch):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(defaults / "strategies.yaml", {"review": {"limit": 5}})
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "red"}}})
    merge_packaged_defaults(config, defaults)
    before_runtime = (config / "strategies.yaml").read_bytes()
    before_baseline = (config / BASELINE_DIRNAME / "strategies.yaml").read_bytes()
    _write_yaml(defaults / "strategies.yaml", {"review": {"limit": 10}})

    original_replace = merge_module.os.replace
    calls = 0

    def fail_second_replace(source, target):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated replacement failure")
        return original_replace(source, target)

    monkeypatch.setattr(merge_module.os, "replace", fail_second_replace)
    with pytest.raises(ConfigMergeError):
        merge_packaged_defaults(config, defaults)
    assert (config / "strategies.yaml").read_bytes() == before_runtime
    assert (config / BASELINE_DIRNAME / "strategies.yaml").read_bytes() == before_baseline


@pytest.mark.parametrize("kind", ["invalid", "type-conflict"])
def test_invalid_yaml_or_type_conflict_fails_without_replacing_user_file(tmp_path, kind):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    _write_yaml(defaults / "labels.yaml", {"labels": {"bug": {"color": "d73a4a"}}})
    _write_yaml(defaults / "strategies.yaml", {"strategies": {"quick": {"enabled": True}}})
    user_path = config / "strategies.yaml"
    if kind == "invalid":
        user_path.write_text("strategies: [unterminated\n", encoding="utf-8")
    else:
        user_path.write_text("strategies: scalar\n", encoding="utf-8")
    original = user_path.read_text(encoding="utf-8")
    with pytest.raises(ConfigMergeError):
        merge_packaged_defaults(config, defaults)
    assert user_path.read_text(encoding="utf-8") == original
