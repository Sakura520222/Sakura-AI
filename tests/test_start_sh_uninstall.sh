#!/usr/bin/env bash
# start.sh 卸载行为的函数级测试：确认门禁、compose purge 参数、镜像清理循环。
# Function-level tests for start.sh uninstall behaviour: confirmation gate,
# compose purge flags, and the recorded-image removal loop.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export _START_SH_SOURCED=1
source "$SCRIPT_DIR/start.sh"
set +e

pass=0; fail=0
report() { if [ "$1" -eq 0 ]; then echo "[OK] $2"; pass=$((pass+1)); else echo "[FAIL] $2"; fail=$((fail+1)); fi; }

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# fake docker 记录 argv 供断言；image inspect 仅对预设存在的镜像返回 0。
FAKE_LOG="$TMPDIR/docker_calls.log"
EXISTING_IMAGE="ghcr.io/sakura520222/sakura-ai-sandboxd:persisted"
docker() {
    printf '%s\n' "$*" >> "$FAKE_LOG"
    case "$1" in
        image)
            # 仅 sandboxd 镜像视为存在；runner 镜像不存在以覆盖跳过分支。
            [ "$2" = "inspect" ] && [ "$3" = "$EXISTING_IMAGE" ] && return 0
            return 1
            ;;
    esac
    return 0
}

# --- C 组：确认门禁 ---

# C1: --yes 直接通过，无需交互终端
confirm_sakura_uninstall false true
report $? "C1: --yes 跳过确认"

# C2: 非交互且无 --yes 时拒绝（stdin 非 tty）
confirm_sakura_uninstall false false </dev/null >/dev/null 2>&1
[ $? -ne 0 ] && report 0 "C2: 非交互无 --yes 拒绝" || report 1 "C2: 非交互无 --yes 拒绝"

# C3: 确认提示只包含统一确认词 UNINSTALL；旧词已移除
if declare -F confirm_sakura_uninstall >/dev/null; then
    if ! grep -q "PURGE SAKURA-AI" "$SCRIPT_DIR/start.sh"; then
        report 0 "C3: 统一 UNINSTALL 确认词（无 PURGE SAKURA-AI）"
    else
        report 1 "C3: 统一 UNINSTALL 确认词（无 PURGE SAKURA-AI）"
    fi
else
    report 1 "C3: 统一 UNINSTALL 确认词（无 PURGE SAKURA-AI）"
fi

# --- K 组：compose purge 参数 ---

select_compose_from_deployment_mode() { :; }
UPDATER_DEPLOYMENT_ENV_FILE=/dev/null
COMPOSE_PROJECT=""
COMPOSE_FILE="$TMPDIR/docker-compose.yml"

: > "$FAKE_LOG"
sakura_compose_uninstall false >/dev/null 2>&1
grep -q "down --remove-orphans" "$FAKE_LOG" \
    && ! grep -q -- "--rmi" "$FAKE_LOG" \
    && report 0 "K1: 标准卸载不加 --rmi" || report 1 "K1: 标准卸载不加 --rmi"

: > "$FAKE_LOG"
sakura_compose_uninstall true >/dev/null 2>&1
grep -q -- "down --remove-orphans --volumes --rmi all" "$FAKE_LOG" \
    && report 0 "K2: 完全卸载追加 --volumes --rmi all" || report 1 "K2: 完全卸载追加 --volumes --rmi all"

# --- P 组：镜像清理循环 ---

cat > "$TMPDIR/deployment.env" <<EOF
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest
SAKURA_SANDBOXD_IMAGE=$EXISTING_IMAGE
SAKURA_AGENT_RUNNER_IMAGE=ghcr.io/sakura520222/sakura-ai-agent-runner:persisted
EOF
DEPLOYMENT_ENV_FILE="$TMPDIR/deployment.env"
SANDBOX_IMAGE=""
SANDBOX_RUNNER_IMAGE=""

# P1: 按 deployment.env 记录逐个删除；inspect 失败的镜像跳过
: > "$FAKE_LOG"
purge_sakura_images >/dev/null 2>&1
grep -q -- "rmi $EXISTING_IMAGE" "$FAKE_LOG" \
    && ! grep -q -- "rmi .*runner:persisted" "$FAKE_LOG" \
    && report 0 "P1: 记录镜像删除，缺失镜像跳过" || report 1 "P1: 记录镜像删除，缺失镜像跳过"

# P2: rmi 失败不阻断（inspect 命中但删除失败仍返回 0）
docker() {
    case "$1" in
        image)
            [ "$2" = "inspect" ] && return 0
            ;;
    esac
    return 1
}
purge_sakura_images >/dev/null 2>&1
report $? "P2: rmi 失败仅告警不阻断"

# P3: deployment.env 缺失时安全跳过（inspect 默认引用不存在 → 无 rmi）
: > "$FAKE_LOG"
SANDBOX_IMAGE=""
SANDBOX_RUNNER_IMAGE=""
rm -f "$TMPDIR/deployment.env"
purge_sakura_images >/dev/null 2>&1
if ! grep -q "rmi" "$FAKE_LOG"; then
    report 0 "P3: 无部署状态时跳过镜像删除"
else
    report 1 "P3: 无部署状态时跳过镜像删除"
fi

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
