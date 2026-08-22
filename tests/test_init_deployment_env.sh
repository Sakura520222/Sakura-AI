#!/usr/bin/env bash
# 测试 init_deployment_env（source start.sh，真正调用实现函数，不重写逻辑）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 跳过 main：start.sh 末尾有 _START_SH_SOURCED guard
export _START_SH_SOURCED=1
# shellcheck disable=SC1091
source "$SCRIPT_DIR/start.sh"
# source 后 start.sh 的 set -euo pipefail 生效；测试断言不触发退出
set +e

pass=0
fail=0
report() { if [ "$1" -eq 0 ]; then echo "[OK] $2"; pass=$((pass+1)); else echo "[FAIL] $2"; fail=$((fail+1)); fi; }
assert_contains() { local file="$1" needle="$2" name="$3"; grep -q "$needle" "$file" && report 0 "$name" || report 1 "$name (expected '$needle' in $file)"; }
assert_not_contains() { local file="$1" needle="$2" name="$3"; grep -q "$needle" "$file" && report 1 "$name (unexpected '$needle' in $file)" || report 0 "$name"; }

# 全局 docker mock：默认 MySQL 数据卷不存在（全新部署场景），各用例可按需
# 覆盖 FAKE_MYSQL_VOLUME=1；D 系列用例会重新定义完整 mock。
docker() {
    case "$1" in
        volume)
            [[ "$2" == "inspect" ]] || return 1
            [[ "${FAKE_MYSQL_VOLUME:-0}" == "1" ]]
            ;;
        *) return 1 ;;
    esac
}

# 场景 1：source 首次初始化（prod=false）
W1=$(mktemp -d)
prod=false DEPLOY_DIR="$W1" DEPLOYMENT_ENV_FILE="$W1/deployment.env" init_deployment_env >/dev/null
assert_contains "$W1/deployment.env" "SAKURA_DEPLOY_MODE=source" "S1: source 模式写 source"
assert_not_contains "$W1/deployment.env" "SAKURA_AI_IMAGE=" "S1: source 模式不写镜像引用"

# 场景 2：image 首次初始化（prod=true，写实际值非表达式）
W2=$(mktemp -d)
prod=true SAKURA_AI_IMAGE="" DEPLOY_DIR="$W2" DEPLOYMENT_ENV_FILE="$W2/deployment.env" init_deployment_env >/dev/null
assert_contains "$W2/deployment.env" "SAKURA_DEPLOY_MODE=image" "S2: image 模式写 image"
assert_contains "$W2/deployment.env" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest" "S2: image 模式写实际镜像值（默认 latest）"
assert_contains "$W2/deployment.env" "COMPOSE_PROJECT_NAME=sakura-ai" "S2: image 模式固定生产 Compose 项目名"
assert_not_contains "$W2/deployment.env" 'SAKURA_AI_IMAGE=${' "S2: 不写 shell 表达式"
password_w2=$(sed -n 's/^SAKURA_DB_PASSWORD=//p' "$W2/deployment.env")
[[ "$password_w2" =~ ^[0-9a-f]{64}$ ]] && report 0 "S2: image 模式生成 64 位十六进制数据库密码" || report 1 "S2: 数据库密码格式错误"

# 场景 2c：再次初始化保持同一 secret（更新/重启不可轮换）
prod=true DEPLOY_DIR="$W2" DEPLOYMENT_ENV_FILE="$W2/deployment.env" init_deployment_env >/dev/null
password_w2_again=$(sed -n 's/^SAKURA_DB_PASSWORD=//p' "$W2/deployment.env")
[[ "$password_w2_again" == "$password_w2" ]] && report 0 "S2c: 重启复用数据库密码" || report 1 "S2c: 数据库密码发生轮换"

# 场景 2b：image 首次初始化（自定义镜像值）
W2b=$(mktemp -d)
prod=true SAKURA_AI_IMAGE="ghcr.io/sakura520222/sakura-ai:v3.1.0" DEPLOY_DIR="$W2b" DEPLOYMENT_ENV_FILE="$W2b/deployment.env" init_deployment_env >/dev/null
assert_contains "$W2b/deployment.env" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0" "S2b: 自定义镜像值被记录"

# 场景 3：已有状态不覆盖（即使模式不同）
W3=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\nCOMPOSE_PROJECT_NAME=sakura-ai\nSAKURA_DB_PASSWORD=%064d\n' 0 > "$W3/deployment.env"
prod=false DEPLOY_DIR="$W3" DEPLOYMENT_ENV_FILE="$W3/deployment.env" init_deployment_env >/dev/null
assert_contains "$W3/deployment.env" "custom:preserved" "S3: 已有状态不被覆盖"
assert_contains "$W3/deployment.env" "SAKURA_DEPLOY_MODE=image" "S3: 已有 mode 不被改回 source"

# 场景 3b：缺数据库密码的 image 状态在 MySQL 卷不存在时自动补全
W3b=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\n' > "$W3b/deployment.env"
prod=false DEPLOY_DIR="$W3b" DEPLOYMENT_ENV_FILE="$W3b/deployment.env" init_deployment_env >/dev/null 2>&1
rc=$?
password_w3b=$(sed -n 's/^SAKURA_DB_PASSWORD=//p' "$W3b/deployment.env")
[[ "$rc" -eq 0 && "$password_w3b" =~ ^[0-9a-f]{64}$ ]] \
    && assert_contains "$W3b/deployment.env" "COMPOSE_PROJECT_NAME=sakura-ai" "S3b: 缺密码/项目名自动补全（卷不存在）" \
    && assert_contains "$W3b/deployment.env" "custom:preserved" "S3b: 已有镜像引用保留" \
    || report 1 "S3b: 缺密码补全失败 rc=$rc"

# 场景 3b-2：MySQL 卷已存在时拒绝自动生成密码（fail-closed）
W3b2=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\n' > "$W3b2/deployment.env"
FAKE_MYSQL_VOLUME=1 prod=false DEPLOY_DIR="$W3b2" DEPLOYMENT_ENV_FILE="$W3b2/deployment.env" init_deployment_env >/dev/null 2>&1
[ "$?" -ne 0 ] && ! grep -q '^SAKURA_DB_PASSWORD=' "$W3b2/deployment.env" \
    && report 0 "S3b-2: 卷存在时拒绝猜测密码" || report 1 "S3b-2"

# 场景 3c：缺项目名的 image 状态自动补全固定项目名（保留既有密码）
W3c=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\nSAKURA_DB_PASSWORD=%064d\n' 0 > "$W3c/deployment.env"
prod=false DEPLOY_DIR="$W3c" DEPLOYMENT_ENV_FILE="$W3c/deployment.env" init_deployment_env >/dev/null 2>&1
rc=$?
password_w3c=$(sed -n 's/^SAKURA_DB_PASSWORD=//p' "$W3c/deployment.env")
[[ "$rc" -eq 0 ]] \
    && assert_contains "$W3c/deployment.env" "COMPOSE_PROJECT_NAME=sakura-ai" "S3c: 缺项目名自动补全" \
    && [[ "$password_w3c" == "$(printf '%064d' 0)" ]] && report 0 "S3c: 已有密码不被轮换" \
    || report 1 "S3c: 缺项目名补全失败 rc=$rc"

# 场景 4：atomic write 不残留临时文件
W4=$(mktemp -d)
prod=true DEPLOY_DIR="$W4" DEPLOYMENT_ENV_FILE="$W4/deployment.env" init_deployment_env >/dev/null
leftovers=$(find "$W4" -name '.deployment.env.*' 2>/dev/null | wc -l)
[ "$leftovers" -eq 0 ] && report 0 "S4: 无临时文件残留" || report 1 "S4: 残留 $leftovers 个临时文件"

# --- apply_channel_image / image_digest_of: 与 updater 一致的 digest-pinned 写入 ---
# 部署状态必须记录 repo:tag@sha256:<digest> 不可变引用（updater deployment.py
# 模型），而不是裸移动别名；否则 updater development 预检无法证明已部署身份，
# WebUI 版本管理会一直提示有可用更新。
DIGEST_DIR=$(mktemp -d)
DIGEST_LOG="$DIGEST_DIR/docker.log"
COMPOSE_CALLS="$DIGEST_DIR/compose.log"
export COMPOSE_CALLS
FAKE_IMAGE_DIGEST="382548dcdbb8acec722bf19cf4097f28c4f1856be126f3bc4baba8a530f244ae"
FAKE_REPO_DIGESTS=""
docker() {
    printf 'docker %s\n' "$*" >> "$DIGEST_LOG"
    case "$1" in
        pull) return 0 ;;
        compose) return 0 ;;
        volume)
            [[ "$2" == "inspect" ]] || return 1
            [[ "${FAKE_MYSQL_VOLUME:-0}" == "1" ]]
            ;;
        image)
            [[ "$2" == "inspect" ]] || return 1
            if [[ -n "$FAKE_REPO_DIGESTS" ]]; then
                printf '%b\n' "$FAKE_REPO_DIGESTS"
            else
                printf 'ghcr.io/sakura520222/sakura-ai@sha256:%s\n' "$FAKE_IMAGE_DIGEST"
            fi
            return 0
            ;;
        *) return 1 ;;
    esac
}
menu_wait_healthy() { return 0; }

DIGEST_ENV="$DIGEST_DIR/deployment.env"
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge\nCOMPOSE_PROJECT_NAME=sakura-ai\nSAKURA_DB_PASSWORD=%064d\n' 0 > "$DIGEST_ENV"

# D1: 单条匹配 RepoDigests 提取 digest
[ "$(image_digest_of "ghcr.io/sakura520222/sakura-ai:edge")" = "sha256:$FAKE_IMAGE_DIGEST" ] \
    && report 0 "D1: image_digest_of 提取匹配 repository 的 digest" || report 1 "D1: image_digest_of"

# D2: 无匹配 / 多条匹配 fail closed（与 updater _select_registry_digest 一致）
FAKE_REPO_DIGESTS="other/repo@sha256:$(printf '%064d' 1)"
image_digest_of "ghcr.io/sakura520222/sakura-ai:edge" >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "D2a: repository 不匹配时拒绝" || report 1 "D2a"
FAKE_REPO_DIGESTS="ghcr.io/sakura520222/sakura-ai@sha256:$(printf '%064d' 1)\nghcr.io/sakura520222/sakura-ai@sha256:$(printf '%064d' 2)"
image_digest_of "ghcr.io/sakura520222/sakura-ai:edge" >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "D2b: 多条 RepoDigests 拒绝（无法证明唯一身份）" || report 1 "D2b"
FAKE_REPO_DIGESTS=""

# D3: development 更新写 digest-pinned 引用而非裸 :edge
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$DIGEST_ENV" apply_channel_image development >/dev/null 2>&1
assert_contains "$DIGEST_ENV" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge@sha256:$FAKE_IMAGE_DIGEST" "D3: development 更新写入 digest-pinned 引用"
assert_not_contains "$DIGEST_ENV" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge$" "D3: 不残留裸移动别名"

# D4: stable 更新同样 pin
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$DIGEST_ENV" apply_channel_image stable >/dev/null 2>&1
assert_contains "$DIGEST_ENV" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest@sha256:$FAKE_IMAGE_DIGEST" "D4: stable 更新写入 digest-pinned 引用"

# D5: digest 解析失败拒绝写入，deployment.env 保持原值
FAKE_REPO_DIGESTS="other/repo@sha256:$(printf '%064d' 3)"
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$DIGEST_ENV" apply_channel_image development >/dev/null 2>&1
[ "$?" -ne 0 ] && assert_contains "$DIGEST_ENV" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest@sha256:$FAKE_IMAGE_DIGEST" "D5: 失败后 deployment.env 保持原值" \
    && report 0 "D5: 解析失败拒绝写入" || report 1 "D5: 解析失败后 deployment.env 被修改"
FAKE_REPO_DIGESTS=""

# D6: 残缺部署状态（缺 COMPOSE_PROJECT_NAME）→ 中止，不执行 compose up
DIGEST_ENV6="$DIGEST_DIR/deployment6.env"
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_DB_PASSWORD=%064d\n' 0 > "$DIGEST_ENV6"
: > "$COMPOSE_CALLS"
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$DIGEST_ENV6" apply_channel_image development >/dev/null 2>&1
rc=$?
assert_contains "$DIGEST_ENV6" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge@sha256:$FAKE_IMAGE_DIGEST" "D6: 镜像引用仍被记录"
[ "$rc" -ne 0 ] && [ ! -s "$COMPOSE_CALLS" ] \
    && report 0 "D6: 残缺部署状态中止且不执行 compose" || report 1 "D6: rc=$rc compose 被调用"

# --- E 系列: 部署状态自动补全与 updater 自动拉起 ---
# E1: 完全残缺（无密码/项目名/镜像）+ 卷不存在 → 自动补全全部
E1_ENV="$DIGEST_DIR/e1.env"
printf 'SAKURA_DEPLOY_MODE=image\n' > "$E1_ENV"
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$E1_ENV" init_deployment_env >/dev/null 2>&1
rc=$?
E1_PW=$(sed -n 's/^SAKURA_DB_PASSWORD=//p' "$E1_ENV")
[[ "$rc" -eq 0 && "$E1_PW" =~ ^[0-9a-f]{64}$ ]] \
    && assert_contains "$E1_ENV" "COMPOSE_PROJECT_NAME=sakura-ai" "E1: 项目名自动补全" \
    && assert_contains "$E1_ENV" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest" "E1: 镜像引用自动补全" \
    || report 1 "E1: 完全残缺补全失败 rc=$rc"
[ "$(grep -c '^$' "$E1_ENV")" -eq 0 ] && report 0 "E1: 补全后无空行" || report 1 "E1: 补全后残留空行"

# E2: MySQL 卷已存在 + 缺密码 → 拒绝自动生成（fail-closed）
E2_ENV="$DIGEST_DIR/e2.env"
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge\nCOMPOSE_PROJECT_NAME=sakura-ai\n' > "$E2_ENV"
FAKE_MYSQL_VOLUME=1 DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$E2_ENV" init_deployment_env >/dev/null 2>&1
[ "$?" -ne 0 ] && ! grep -q '^SAKURA_DB_PASSWORD=' "$E2_ENV" \
    && report 0 "E2: 卷存在时拒绝自动生成密码" || report 1 "E2: 卷存在时未拒绝"

# E3: 卷存在 + 显式环境变量密码 → 补写该密码（用户恢复路径）
E3_ENV="$DIGEST_DIR/e3.env"
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge\nCOMPOSE_PROJECT_NAME=sakura-ai\n' > "$E3_ENV"
E3_PW=$(printf '%064d' 7)
FAKE_MYSQL_VOLUME=1 SAKURA_DB_PASSWORD="$E3_PW" DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$E3_ENV" init_deployment_env >/dev/null 2>&1
assert_contains "$E3_ENV" "SAKURA_DB_PASSWORD=$E3_PW" "E3: 环境变量密码被补写"

# E4: 完整合法状态不触发补全（内容不变）
E4_ENV="$DIGEST_DIR/e4.env"
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:edge@sha256:%s\nCOMPOSE_PROJECT_NAME=sakura-ai\nSAKURA_DB_PASSWORD=%064d\n' "$FAKE_IMAGE_DIGEST" 8 > "$E4_ENV"
E4_BEFORE=$(cat "$E4_ENV")
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$E4_ENV" init_deployment_env >/dev/null 2>&1
[[ "$(cat "$E4_ENV")" == "$E4_BEFORE" ]] && report 0 "E4: 完整状态不重写" || report 1 "E4: 完整状态被修改"

# E5: 镜像部署成功后自动拉起 host updater daemon
ENSURE_LOG="$DIGEST_DIR/ensure.log"
ensure_updater_running() { echo ENSURE >> "$ENSURE_LOG"; return 0; }
: > "$ENSURE_LOG"
DEPLOY_DIR="$DIGEST_DIR" DEPLOYMENT_ENV_FILE="$DIGEST_ENV" apply_channel_image development >/dev/null 2>&1
grep -q '^ENSURE$' "$ENSURE_LOG" && report 0 "E5: 部署后自动 ensure updater" || report 1 "E5: 未自动 ensure updater"

rm -rf "$DIGEST_DIR"

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
