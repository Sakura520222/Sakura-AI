"""Behavioral contracts for start.sh location-independent bootstrap."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _run_bash(command: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash"],
        cwd=ROOT,
        env={**os.environ, "TERM": "dumb"},
        input=command.encode("utf-8"),
        capture_output=True,
        check=False,
    )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def test_runner_script_heredoc_is_literal():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
write_production_runner_script "$T/runner.sh"
bash -n "$T/runner.sh"
grep -Fq "trap 'runner_status=\$?; production_runner_exit \"\$runner_status\"' EXIT" "$T/runner.sh"
grep -Fq 'build_runner "$rebuild" "$prod"' "$T/runner.sh"
! grep -Fq '${rebuild}' "$T/runner.sh"
! grep -Fq '${PRODUCTION_STAGED_ENV_FILE}' "$T/runner.sh"
# start.sh 顶层配置区会在 source 时重置 PRODUCTION_* 事务变量；
# 依赖注入的 export 必须位于 source 之后（回归：source 前导曾导致
# runner 内 DEPLOYMENT_ENV_FILE 落回权威文件而报 missing COMPOSE_PROJECT_NAME）。
src_line=$(grep -n -F 'source "$abs_script_dir/start.sh"' "$T/runner.sh" | cut -d: -f1)
staged_line=$(grep -n -F 'export PRODUCTION_STAGED_ENV_FILE="$staged_env"' "$T/runner.sh" | cut -d: -f1)
[[ -n "$src_line" && -n "$staged_line" && "$src_line" -lt "$staged_line" ]]
'''
    )
    assert result.returncode == 0, result.stderr


def test_ensure_prod_compose_file_downloads_when_missing():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
curl() {
    local out=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-o" ]]; then out="$2"; shift 2; else shift; fi
    done
    [[ -n "$out" ]] || return 1
    printf 'services: {}\n' > "$out"
}
ensure_prod_compose_file
test -f "$T/docker/docker-compose.prod.yml"
grep -q '^services:' "$T/docker/docker-compose.prod.yml"
'''
    )
    assert result.returncode == 0, result.stderr


def test_ensure_prod_compose_file_skips_download_when_present():
    result = _run_bash(
        r'''
set -euo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
mkdir -p "$T/docker"
printf 'services: {}\n' > "$T/docker/docker-compose.prod.yml"
# 任何 curl 调用都会失败；文件已存在时函数必须不发起下载。
curl() { return 1; }
ensure_prod_compose_file
'''
    )
    assert result.returncode == 0, result.stderr


def test_bootstrap_relocates_script_to_install_root():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/src" "$T/install"
cp ./start.sh "$T/src/start.sh"
SAKURA_INSTALL_ROOT="$T/install" bootstrap_canonical_install "$T/src/start.sh" --unknown-flag
echo "should-not-reach" >&2
exit 9
'''
    )
    # exec 后由目标脚本接管；--unknown-flag 使其退出码为 1。
    assert result.returncode == 1
    assert "已安置 start.sh" in result.stdout
    assert "should-not-reach" not in result.stderr


def test_bootstrap_skips_inside_repo_layout():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
bootstrap_canonical_install ./start.sh --unknown-flag
echo REACHED
'''
    )
    assert result.returncode == 0
    assert "REACHED" in result.stdout
    assert "已安置" not in result.stdout


def test_piped_bootstrap_downloads_and_execs():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/install"
curl() {
    local out=""
    while [[ $# -gt 0 ]]; do
        if [[ "$1" == "-o" ]]; then out="$2"; shift 2; else shift; fi
    done
    [[ -n "$out" ]] || return 1
    cp ./start.sh "$out"
}
SAKURA_INSTALL_ROOT="$T/install" bootstrap_piped_install --unknown-flag
echo "should-not-reach" >&2
exit 9
'''
    )
    # exec 后由目标脚本接管；--unknown-flag 使其退出码为 1。
    assert result.returncode == 1
    assert "管道模式" in result.stdout
    assert "should-not-reach" not in result.stderr


def test_do_start_refuses_local_build_outside_repo():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
DEPLOYMENT_ENV_FILE="$T/.deploy/.deployment.env"
docker() { return 0; }
rc=0
out=$(do_start false false 2>&1) || rc=$?
[[ "$rc" -ne 0 ]] || { echo "expected failure" >&2; exit 1; }
printf '%s\n' "$out" | grep -q '本地构建不可用'
'''
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_uninstall_skips_compose_cleanup_when_uninitialized():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
UPDATER_DEPLOYMENT_ENV_FILE="$T/.deploy/deployment.env"
docker() { return 0; }
out=$(sakura_compose_uninstall false 2>&1) && rc=0 || rc=$?
[[ "$rc" -eq 0 ]] || { printf '%s\n' "$out" >&2; exit 1; }
printf '%s\n' "$out" | grep -q '无 Compose 服务需要清理'
'''
    )
    assert result.returncode == 0, result.stderr + result.stdout


def _dev_channel_docker_stub() -> str:
    return r'''
HEXW=1111111111111111111111111111111111111111111111111111111111111111
HEXS=2222222222222222222222222222222222222222222222222222222222222222
HEXR=3333333333333333333333333333333333333333333333333333333333333333
DEVREV=0123456789abcdef0123456789abcdef01234567
DEV_TAG="dev-20260830120000-v3.2.0-$DEVREV"
docker() {
    case "$*" in
        *com.sakura-ai.build.channel*) echo development ;;
        *org.opencontainers.image.revision*) echo "$DEVREV" ;;
        *RepoDigests*)
            echo "ghcr.io/sakura520222/sakura-ai@sha256:$HEXW"
            echo "ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:$HEXS"
            echo "ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:$HEXR"
            ;;
        *) return 0 ;;
    esac
}
curl() {
    case "$*" in
        *token*) printf '{"token":"anon"}' ;;
        *tags/list*) printf '{"name":"sakura-ai","tags":["v3.1.0","latest","dev-20260830120000-v3.2.0-%s","dev-20260830110000-v3.1.0-11111111111111111111111111111111111111111"]}' "$DEVREV" ;;
        *) return 1 ;;
    esac
}
'''


def test_dev_channel_first_deploy_pins_edge_images():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
DEPLOYMENT_ENV_FILE="$T/.deploy/.deployment.env"
COMPOSE="docker"
''' + _dev_channel_docker_stub() + r'''
prod=true
production_prepare_env_stage || exit 1
DEPLOYMENT_ENV_FILE="$PRODUCTION_STAGED_ENV_FILE"
SAKURA_DEPLOY_CHANNEL=development production_prepare_and_pull_images || exit 1
grep -q '^SAKURA_AI_IMAGE=ghcr\.io/sakura520222/sakura-ai:dev-[0-9]\{14\}-v[0-9]\+\.[0-9]\+\.[0-9]\+-[0-9a-f]\{40\}@sha256:[0-9a-f]\{64\}$' "$PRODUCTION_STAGED_ENV_FILE"
grep -q '^SAKURA_SANDBOXD_IMAGE_DIGEST=ghcr\.io/sakura520222/sakura-ai-sandboxd@sha256:[0-9a-f]\{64\}$' "$PRODUCTION_STAGED_ENV_FILE"
grep -q '^SAKURA_AGENT_RUNNER_IMAGE_DIGEST=ghcr\.io/sakura520222/sakura-ai-agent-runner@sha256:[0-9a-f]\{64\}$' "$PRODUCTION_STAGED_ENV_FILE"
# development 部署不写 stable Release 版本标记。
! grep -q '^SAKURA_SANDBOX_RELEASE_VERSION=' "$PRODUCTION_STAGED_ENV_FILE"
'''
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_dev_channel_pinned_restart_does_not_move_dev_reference():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
DEPLOYMENT_ENV_FILE="$T/.deploy/.deployment.env"
COMPOSE="docker"
''' + _dev_channel_docker_stub() + r'''
prod=true
production_prepare_env_stage || exit 1
DEPLOYMENT_ENV_FILE="$PRODUCTION_STAGED_ENV_FILE"
SAKURA_DEPLOY_CHANNEL=development production_prepare_and_pull_images || exit 1
out=$(SAKURA_DEPLOY_CHANNEL=development production_prepare_and_pull_images 2>&1) || exit 1
printf '%s\n' "$out" | grep -q '按已 pin 的 digest 拉取三镜像'
'''
    )
    assert result.returncode == 0, result.stderr + result.stdout


def test_sandbox_persist_identity_skips_release_version_for_dev():
    result = _run_bash(
        r'''
set -uo pipefail
export _START_SH_SOURCED=1
source ./start.sh
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
UPDATER_PROJECT_ROOT="$T"
DEPLOYMENT_ENV_FILE="$T/.deploy/deployment.env"
mkdir -p "$T/.deploy"
printf 'SAKURA_DEPLOY_MODE=image\n' > "$DEPLOYMENT_ENV_FILE"
SANDBOX_IMAGE="ghcr.io/sakura520222/sakura-ai-sandboxd"
SANDBOX_IMAGE_DIGEST="ghcr.io/sakura520222/sakura-ai-sandboxd@sha256:2222222222222222222222222222222222222222222222222222222222222222"
SANDBOX_RUNNER_IMAGE="ghcr.io/sakura520222/sakura-ai-agent-runner"
SANDBOX_RUNNER_DIGEST="ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:3333333333333333333333333333333333333333333333333333333333333333"
SAKURA_DEPLOY_CHANNEL=development sandbox_persist_runtime_identity || exit 1
! grep -q '^SAKURA_SANDBOX_RELEASE_VERSION=' "$DEPLOYMENT_ENV_FILE"
'''
    )
    assert result.returncode == 0, result.stderr + result.stdout
