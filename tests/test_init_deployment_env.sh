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

# 场景 3b：不完整的 image 状态无兼容或补写路径
W3b=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\n' > "$W3b/deployment.env"
prod=false DEPLOY_DIR="$W3b" DEPLOYMENT_ENV_FILE="$W3b/deployment.env" init_deployment_env >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "S3b: 不完整生产状态直接无效" || report 1 "S3b: 不完整状态未 fail-closed"
assert_not_contains "$W3b/deployment.env" "SAKURA_DB_PASSWORD=" "S3b: 未偷偷追加新密码"

# 场景 3c：生产项目字段缺失时无补写兼容
W3c=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\nSAKURA_DB_PASSWORD=%064d\n' 0 > "$W3c/deployment.env"
prod=false DEPLOY_DIR="$W3c" DEPLOYMENT_ENV_FILE="$W3c/deployment.env" init_deployment_env >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "S3c: 缺少固定项目名的生产状态直接无效" || report 1 "S3c: 缺少项目名的状态未 fail-closed"
assert_not_contains "$W3c/deployment.env" "COMPOSE_PROJECT_NAME=" "S3c: 未补写兼容字段"

# 场景 4：atomic write 不残留临时文件
W4=$(mktemp -d)
prod=true DEPLOY_DIR="$W4" DEPLOYMENT_ENV_FILE="$W4/deployment.env" init_deployment_env >/dev/null
leftovers=$(find "$W4" -name '.deployment.env.*' 2>/dev/null | wc -l)
[ "$leftovers" -eq 0 ] && report 0 "S4: 无临时文件残留" || report 1 "S4: 残留 $leftovers 个临时文件"

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
