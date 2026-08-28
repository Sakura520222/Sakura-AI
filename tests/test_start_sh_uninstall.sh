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

# fake docker 记录 argv 供断言；compose 与 rmi 默认成功。
FAKE_LOG="$TMPDIR/docker_calls.log"
docker() {
    printf '%s\n' "$*" >> "$FAKE_LOG"
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

# --- P 组：镜像清理循环（按仓库前缀枚举） ---

# image_ls.txt 模拟 docker image ls 输出：历史版本 tag、latest、无 tag 的
# sandboxd、sakura digest-pull dangling、无关 dangling、无关仓库。
cat > "$TMPDIR/image_ls.txt" <<'EOF'
abc111 ghcr.io/sakura520222/sakura-ai v1.2.3
abc222 ghcr.io/sakura520222/sakura-ai latest
abc333 ghcr.io/sakura520222/sakura-ai-sandboxd <none>
abc444 <none> <none>
abc555 <none> <none>
abc666 mysql 8.4
EOF

docker() {
    printf '%s\n' "$*" >> "$FAKE_LOG"
    case "$1 $2" in
        "image ls") cat "$TMPDIR/image_ls.txt" ;;
        "image inspect")
            # argv: image inspect --format <template> <id> → id 为 $5。
            case "$5" in
                abc444) printf '%s\n' '[ghcr.io/sakura520222/sakura-ai-agent-runner@sha256:0123]' ;;
                abc555) printf '%s\n' '[postgres@sha256:0123]' ;;
            esac
            ;;
    esac
    return 0
}

# P1: sakura 仓库镜像全删（含历史 tag 与 RepoDigests 归属的 dangling）；无关镜像保留
: > "$FAKE_LOG"
purge_sakura_images >/dev/null 2>&1
grep -q "rmi -f abc111" "$FAKE_LOG" \
    && grep -q "rmi -f abc222" "$FAKE_LOG" \
    && grep -q "rmi -f abc333" "$FAKE_LOG" \
    && grep -q "rmi -f abc444" "$FAKE_LOG" \
    && ! grep -q "rmi -f abc555" "$FAKE_LOG" \
    && ! grep -q "rmi -f abc666" "$FAKE_LOG" \
    && report 0 "P1: 仓库前缀枚举删除，无关镜像保留" || report 1 "P1: 仓库前缀枚举删除，无关镜像保留"

# P2: rmi 失败不阻断（全部删除失败仍返回 0）
docker() {
    case "$1" in rmi) return 1 ;; esac
    return 0
}
purge_sakura_images >/dev/null 2>&1
report $? "P2: rmi 失败仅告警不阻断"

# P3: 无本地镜像时安全跳过
cat > "$TMPDIR/image_ls.txt" <<'EOF'
EOF
docker() {
    case "$1 $2" in "image ls") cat "$TMPDIR/image_ls.txt" ;; esac
    return 0
}
: > "$FAKE_LOG"
purge_sakura_images >/dev/null 2>&1
if [ $? -eq 0 ] && [ ! -s "$FAKE_LOG" ]; then
    report 0 "P3: 无本地镜像时安全跳过"
else
    report 1 "P3: 无本地镜像时安全跳过"
fi

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
