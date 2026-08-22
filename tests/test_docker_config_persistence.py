"""Contracts for the packaged-default merge hook and legacy YAML retirement.

strategies.yaml/labels.yaml now live in the app_config table (unified config
store); the merge script keeps its machinery but manages no files today.
These tests pin the deployment contract: no config directory ships in the
image, the entrypoint still runs the (empty) merge before the application
command, and a legacy yaml left in the persistent volume is never touched.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

from scripts.merge_packaged_config import (
    BASELINE_DIRNAME,
    merge_packaged_defaults,
)

ROOT = Path(__file__).parents[1]
DOCKERFILE = ROOT / "docker" / "Dockerfile"
ENTRYPOINT = ROOT / "scripts" / "docker-entrypoint.sh"
COMPOSE = ROOT / "docker" / "docker-compose.prod.yml"


def test_dockerfile_ships_no_config_directory():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    # 统一配置存储后 config/ 无追踪文件；镜像内不打包 config 目录，
    # /app/config 完全由持久化卷提供
    assert "COPY config" not in dockerfile
    assert 'ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]' in dockerfile
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    mounts = compose["services"]["web"]["volumes"]
    assert "config_data:/app/config" in mounts


def test_entrypoint_invokes_merge_before_application_command():
    script = ENTRYPOINT.read_text(encoding="utf-8")
    assert '"$python_bin" "$merge_script" --config-dir "$config_dir"' in script
    assert 'exec "$@"' in script
    assert "source " not in script
    assert "eval " not in script


def test_entrypoint_merge_leaves_legacy_volume_files_untouched():
    command = r"""
set -euo pipefail
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/defaults" "$work/config"
printf 'root:\n  baseline: user\n' > "$work/config/strategies.yaml"
printf 'labels:\n  legacy:\n    color: "abcdef"\n' > "$work/config/labels.yaml"
printf '{"database_url":"mysql://preserve"}\n' > "$work/config/connection.json"
python_bin="$(command -v python || command -v python3)"
SAKURA_CONFIG_DIR="$work/config" \
SAKURA_DEFAULT_CONFIG_DIR="$work/defaults" \
SAKURA_CONFIG_MERGE_SCRIPT="$PWD/scripts/merge_packaged_config.py" \
SAKURA_CONFIG_PYTHON="$python_bin" \
bash scripts/docker-entrypoint.sh true
grep -q 'baseline: user' "$work/config/strategies.yaml"
grep -q 'legacy:' "$work/config/labels.yaml"
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
    assert result.returncode == 0, result.stdout.decode(
        errors="replace"
    ) + result.stderr.decode(errors="replace")


def test_merge_packaged_defaults_is_noop_without_managed_files(tmp_path):
    defaults = tmp_path / "defaults"
    config = tmp_path / "config"
    defaults.mkdir()
    config.mkdir()
    legacy = config / "strategies.yaml"
    legacy.write_text("strategies: {}\n", encoding="utf-8")

    changed = merge_packaged_defaults(config, defaults)

    assert changed == []
    # 残留旧 yaml 与空 defaults 目录均不被触碰、不产生 baseline
    assert legacy.read_text(encoding="utf-8") == "strategies: {}\n"
    assert not (config / BASELINE_DIRNAME).exists()
