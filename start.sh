#!/usr/bin/env bash
# Sakura AI 快速启动脚本
#
# 支持断线续跑：构建/安装步骤在后台 nohup 运行，SSH 断线不中断。
# 重新运行脚本时会自动跳过已完成的阶段。
#
# 用法:
#   ./start.sh                # 交互式菜单（支持更新镜像、切换 stable/development 频道）
#   ./start.sh --rebuild      # 强制重建镜像
#   ./start.sh --prod         # 生产模式：拉取 GHCR 镜像一键部署（跳过本地构建）
#   ./start.sh --status       # 查看当前构建/运行状态
#   ./start.sh --attach       # 附加到正在进行的构建日志
#   ./start.sh --stop         # 停止正在进行的构建
#   ./start.sh --ps           # 查看服务容器状态
#   ./start.sh --down         # 停止服务
#   ./start.sh uninstall      # 卸载服务（默认保留 Docker 数据卷）
#   ./start.sh uninstall --purge  # 同时删除 Docker 数据卷和 .deploy 状态
#   ./start.sh updater [action]  # 管理 host updater daemon（含 reinstall/uninstall）
#   ./start.sh --help         # 显示帮助

set -euo pipefail

UPDATER_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ============================================================
# 配置
# ============================================================

COMPOSE_FILE="docker/docker-compose.yml"
PROD_COMPOSE_FILE="docker/docker-compose.prod.yml"
COMPOSE_PROJECT=""
DEFAULT_PROD_COMPOSE_PROJECT="sakura-ai"
DEPLOY_DIR=".deploy"
BUILD_LOG="$DEPLOY_DIR/build.log"
PHASE_FILE="$DEPLOY_DIR/phase"         # 当前阶段: preflight / build / pip / pull / start / health / done
PHASE_RESULT="$DEPLOY_DIR/phase_result" # 上一阶段结果: ok / fail
PID_FILE="$DEPLOY_DIR/build.pid"
RUNNER_IDENTITY_FILE="$DEPLOY_DIR/build-runner.identity"
HASH_FILE="$DEPLOY_DIR/requirements.hash"
DOCKERFILE_HASH_FILE="$DEPLOY_DIR/dockerfile.hash"
HEALTH_TIMEOUT=90

# ============================================================
# 工具函数
# ============================================================

BOLD="\033[1m"
DIM="\033[2m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
CYAN="\033[0;36m"
RESET="\033[0m"

info()  { echo -e "${CYAN}[INFO]${RESET} $*"; }
ok()    { echo -e "${GREEN}[OK]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${RESET} $*"; }
fail()  { echo -e "${RED}[FAIL]${RESET} $*"; }

set_phase() {
    echo "$1" > "$PHASE_FILE"
    echo "${2:-ok}" > "$PHASE_RESULT"
}

get_phase()  { cat "$PHASE_FILE" 2>/dev/null || echo "none"; }
get_result() { cat "$PHASE_RESULT" 2>/dev/null || echo ""; }

runner_pid_file_path() {
    if [[ "$PID_FILE" == /* ]]; then
        printf '%s\n' "$PID_FILE"
    else
        printf '%s/%s\n' "$UPDATER_PROJECT_ROOT" "$PID_FILE"
    fi
}

runner_identity_file_path() {
    if [[ "$RUNNER_IDENTITY_FILE" == /* ]]; then
        printf '%s\n' "$RUNNER_IDENTITY_FILE"
    else
        printf '%s/%s\n' "$UPDATER_PROJECT_ROOT" "$RUNNER_IDENTITY_FILE"
    fi
}

runner_read_pid() {
    local pid pid_file
    pid_file=$(runner_pid_file_path) || return 1
    [[ -f "$pid_file" ]] || return 1
    pid=$(cat "$pid_file" 2>/dev/null) || return 1
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$pid"
}

runner_pid_is_live() {
    local pid
    pid=$(runner_read_pid) || return 1
    kill -0 "$pid" 2>/dev/null
}

runner_process_starttime() {
    local pid="$1" stat_line fields
    [[ -r "/proc/$pid/stat" ]] || return 1
    IFS= read -r stat_line < "/proc/$pid/stat" || return 1
    [[ "$stat_line" == *") "* ]] || return 1
    fields="${stat_line##*) }"
    # After pid/comm, field 3 is the first token; starttime is field 22.
    set -- $fields
    [[ $# -ge 20 && "${20}" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "${20}"
}

runner_process_command_matches() {
    local pid="$1" expected_runner="$2" arg
    local -a argv=()
    [[ -r "/proc/$pid/cmdline" ]] || return 1
    while IFS= read -r -d '' arg; do
        argv+=("$arg")
    done < "/proc/$pid/cmdline"
    [[ ${#argv[@]} -ge 2 ]] || return 1
    [[ "${argv[0]##*/}" == "bash" && "${argv[1]}" == "$expected_runner" ]]
}

runner_identity_matches() {
    local pid expected_starttime expected_runner current_starttime identity_file
    pid=$(runner_read_pid) || return 1
    identity_file=$(runner_identity_file_path) || return 1
    [[ -f "$identity_file" ]] || return 1
    {
        IFS= read -r expected_starttime
        IFS= read -r expected_runner
    } < "$identity_file" || return 1
    [[ "$expected_starttime" =~ ^[0-9]+$ && -n "$expected_runner" ]] || return 1
    current_starttime=$(runner_process_starttime "$pid") || return 1
    [[ "$current_starttime" == "$expected_starttime" ]] || return 1
    runner_process_command_matches "$pid" "$expected_runner"
}

runner_write_identity() {
    local pid="$1" expected_runner="$2" starttime="" identity_file tmp attempts=0
    # setsid/nohup exec into bash asynchronously. Wait only for the exact child
    # identity; a dead or different PID never gets recorded as the runner.
    while [[ "$attempts" -lt 50 ]]; do
        if starttime=$(runner_process_starttime "$pid") \
            && runner_process_command_matches "$pid" "$expected_runner"; then
            break
        fi
        kill -0 "$pid" 2>/dev/null || return 1
        sleep 0.02
        attempts=$((attempts + 1))
    done
    [[ -n "$starttime" && "$attempts" -lt 50 ]] || return 1
    identity_file=$(runner_identity_file_path) || return 1
    tmp="$identity_file.tmp.$$"
    if ! printf '%s\n%s\n' "$starttime" "$expected_runner" > "$tmp"; then
        rm -f -- "$tmp"
        return 1
    fi
    mv -f -- "$tmp" "$identity_file"
    runner_identity_matches
}

clear_runner_identity() {
    rm -f -- "$(runner_pid_file_path)" "$(runner_identity_file_path)"
}

is_running() {
    runner_identity_matches
}

wait_for_pid() {
    local pid=$1 label=${2:-process}
    local elapsed=0
    # Print a live dot while waiting
    while kill -0 "$pid" 2>/dev/null; do
        sleep 2
        elapsed=$((elapsed + 2))
        printf "\r%s 进行中... %ds " "$label" "$elapsed"
    done
    echo ""
    wait "$pid"
    return $?
}

compose_pull_with_native_progress() {
    local compose_help=""
    # The deployment runner is detached, so Compose would normally downgrade to
    # noisy plain layer logs. Force its native TTY renderer; tail/--attach passes
    # the control stream to the administrator's terminal while the pull itself
    # remains owned by the background runner after Ctrl+C or SSH disconnect.
    compose_help=$(docker compose --help 2>/dev/null || true)
    if [[ "$compose_help" == *--progress* ]]; then
        $COMPOSE --ansi always --progress tty pull
    else
        warn "当前 Docker Compose 不支持原生 TTY 进度条，回退到普通拉取输出"
        $COMPOSE pull
    fi
}

# ============================================================
# 部署状态初始化 / Deployment state bootstrap
# ============================================================

# deployment.env 权威部署状态文件路径（见 auto-update 设计 §9.5）
DEPLOYMENT_ENV_FILE="$DEPLOY_DIR/deployment.env"

# 首次启动时初始化部署状态：写入部署模式（source/image）、实际镜像引用
# 与生产 MySQL 密码。密码只在首次创建 image deployment.env 时生成；
# 已存在但缺密码的生产状态拒绝猜测/轮换，避免与已有 mysql_data 脱节。
# - 已存在且包含密码则不覆盖（updater 或之前初始化已写入）。
# - 写实际值（非 ${...} 表达式）：deployment.env 记录"当时实际选择的镜像"。
# - durability：write temp → fsync(sync -d) → atomic mv，满足 spec §9.5。
# - digest 具体化（:latest → :vX.Y.Z@sha256:...）留给 Slice 4 updater activate。
generate_deployment_db_password() {
    local generated=""
    if command -v openssl >/dev/null 2>&1; then
        generated="$(openssl rand -hex 32 2>/dev/null || true)"
    elif command -v python3 >/dev/null 2>&1; then
        generated="$(python3 -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || true)"
    elif command -v python >/dev/null 2>&1; then
        generated="$(python -c 'import secrets; print(secrets.token_hex(32))' 2>/dev/null || true)"
    fi

    if [[ ! "$generated" =~ ^[0-9a-f]{64}$ ]]; then
        fail "无法生成 64 位十六进制 SAKURA_DB_PASSWORD；请安装 openssl 或 Python 3" >&2
        return 1
    fi
    printf '%s' "$generated"
}

# MySQL 数据卷状态（compose 项目名固定 sakura-ai 前缀 + mysql_data 卷）。
# 输出 exists / missing / error 三种状态；Docker daemon、权限或 CLI 异常绝不能
# 被当作卷不存在，否则残缺 deployment.env 会错误轮换既有 MySQL 密码。
deployment_mysql_volume_state() {
    local volume_name="${DEFAULT_PROD_COMPOSE_PROJECT}_mysql_data"
    local volume_names=""
    local name=""

    if ! volume_names="$(docker volume ls --format '{{.Name}}' 2>/dev/null)"; then
        printf 'error\n'
        return 0
    fi

    while IFS= read -r name; do
        if [[ "$name" == "$volume_name" ]]; then
            printf 'exists\n'
            return 0
        fi
    done <<< "$volume_names"

    printf 'missing\n'
}

# 兼容旧调用方的布尔探测；部署状态修复逻辑必须使用上面的三态结果。
deployment_mysql_volume_exists() {
    [[ "$(deployment_mysql_volume_state)" == "exists" ]]
}

# 原子补全 deployment.env 的多个键值（KEY=VALUE 参数），保留其余行与 0600
# 权限。与 write_deployment_env_image 相同的 durability 顺序；调用方只传解析
# 后的最终值，未缺失的键写回原值，保证幂等。
write_deployment_env_keys() {
    local tmp="" arg key replaced line
    if [[ ! -f "$DEPLOYMENT_ENV_FILE" ]]; then
        fail "缺少部署状态文件: $DEPLOYMENT_ENV_FILE" >&2
        return 1
    fi
    tmp="$DEPLOY_DIR/.deployment.env.keys.$$"
    : > "$tmp"
    while IFS= read -r line || [[ -n "$line" ]]; do
        replaced=0
        for arg in "$@"; do
            if [[ "$line" == "${arg%%=*}="* ]]; then
                printf '%s\n' "$arg" >> "$tmp"
                replaced=1
                break
            fi
        done
        if [[ "$replaced" -eq 0 ]]; then
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$DEPLOYMENT_ENV_FILE"
    # 追加文件中不存在的键（原文件尾无换行时先补换行；
    # 命令替换会剥离尾部换行符，输出为空即表示以换行结尾）
    if [[ -s "$tmp" ]] && [[ -n "$(tail -c 1 "$tmp")" ]]; then
        printf '\n' >> "$tmp"
    fi
    for arg in "$@"; do
        if ! grep -q "^${arg%%=*}=" "$tmp"; then
            printf '%s\n' "$arg" >> "$tmp"
        fi
    done
    if ! chmod 600 "$tmp"; then
        rm -f "$tmp"
        fail "无法将 deployment.env 权限设置为 0600；拒绝写入数据库凭据" >&2
        return 1
    fi
    sync -d "$tmp" 2>/dev/null || sync 2>/dev/null || true
    if ! mv -f -- "$tmp" "$DEPLOYMENT_ENV_FILE"; then
        rm -f "$tmp"
        fail "deployment.env 原子替换失败" >&2
        return 1
    fi
}

init_deployment_env() {
    local mode="source"
    if ${prod:-false}; then
        mode="image"
    fi

    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        local persisted_mode=""
        local persisted_password=""
        local persisted_project=""
        local persisted_image=""
        local line
        while IFS= read -r line || [[ -n "$line" ]]; do
            case "$line" in
                SAKURA_DEPLOY_MODE=*) persisted_mode="${line#SAKURA_DEPLOY_MODE=}" ;;
                SAKURA_DB_PASSWORD=*) persisted_password="${line#SAKURA_DB_PASSWORD=}" ;;
                COMPOSE_PROJECT_NAME=*) persisted_project="${line#COMPOSE_PROJECT_NAME=}" ;;
                SAKURA_AI_IMAGE=*) persisted_image="${line#SAKURA_AI_IMAGE=}" ;;
            esac
        done < "$DEPLOYMENT_ENV_FILE"

        case "$persisted_mode" in
            source)
                ;;
            image)
                local need_write=0
                # 自动补全缺失的部署状态（数据库凭据/项目名/镜像引用），让残缺
                # 文件也能直接部署：
                # - 缺数据库密码：仅当 MySQL 数据卷不存在（全新部署）才生成新
                #   密码；卷已存在时密码必须与既有数据一致，无法猜测，fail-closed。
                # - 项目名缺失补固定值 sakura-ai；已存在但不合法仍拒绝（不覆盖）。
                # - 镜像缺失补默认 latest（频道别名，后续更新会 pin 成 digest 引用）。
                if [[ ! "$persisted_password" =~ ^[0-9a-f]{64}$ ]]; then
                    if [[ "${SAKURA_DB_PASSWORD:-}" =~ ^[0-9a-f]{64}$ ]]; then
                        persisted_password="$SAKURA_DB_PASSWORD"
                        need_write=1
                    else
                        local mysql_volume_state
                        mysql_volume_state="$(deployment_mysql_volume_state)"
                        case "$mysql_volume_state" in
                            missing)
                                persisted_password="$(generate_deployment_db_password)" || return 1
                                need_write=1
                                ;;
                            exists)
                                fail "deployment.env 缺少数据库密码，且 MySQL 数据卷 ${DEFAULT_PROD_COMPOSE_PROJECT}_mysql_data 已存在；无法自动生成" >&2
                                fail "恢复：从备份还原 deployment.env，或设置环境变量 SAKURA_DB_PASSWORD=<原密码> 后重新运行" >&2
                                return 1
                                ;;
                            error)
                                fail "无法确认 MySQL 数据卷 ${DEFAULT_PROD_COMPOSE_PROJECT}_mysql_data 状态；Docker 不可用或权限不足，拒绝生成新密码" >&2
                                fail "恢复：确认 Docker daemon 和权限后重试，或设置环境变量 SAKURA_DB_PASSWORD=<原密码> 后重新运行" >&2
                                return 1
                                ;;
                            *)
                                fail "无法识别 MySQL 数据卷探测结果: $mysql_volume_state" >&2
                                return 1
                                ;;
                        esac
                    fi
                fi
                if [[ -z "$persisted_project" ]]; then
                    persisted_project="$DEFAULT_PROD_COMPOSE_PROJECT"
                    need_write=1
                elif [[ "$persisted_project" != "$DEFAULT_PROD_COMPOSE_PROJECT" ]]; then
                    fail "invalid production deployment state: COMPOSE_PROJECT_NAME must be '$DEFAULT_PROD_COMPOSE_PROJECT'" >&2
                    return 1
                fi
                if [[ -z "$persisted_image" ]]; then
                    persisted_image="ghcr.io/sakura520222/sakura-ai:latest"
                    need_write=1
                fi
                if [[ "$need_write" -eq 1 ]]; then
                    write_deployment_env_keys \
                        "SAKURA_DB_PASSWORD=$persisted_password" \
                        "COMPOSE_PROJECT_NAME=$persisted_project" \
                        "SAKURA_AI_IMAGE=$persisted_image" || return 1
                    info "已补全部署状态: $DEPLOYMENT_ENV_FILE"
                fi
                ;;
            *)
                fail "invalid deployment state: SAKURA_DEPLOY_MODE must be 'source' or 'image'" >&2
                return 1
                ;;
        esac
        chmod 600 "$DEPLOYMENT_ENV_FILE" || {
            fail "无法将 deployment.env 权限收紧为 0600；拒绝启动以保护数据库凭据" >&2
            return 1
        }
        return 0
    fi

    mkdir -p "$DEPLOY_DIR"
    local tmp
    tmp="$DEPLOY_DIR/.deployment.env.$$"
    local db_password=""
    if [[ "$mode" == "image" ]]; then
        db_password="$(generate_deployment_db_password)"
    fi
    {
        echo "# Sakura AI 部署状态（由 start.sh 初始化；updater 接管后以 atomic write 维护）"
        echo "SAKURA_DEPLOY_MODE=$mode"
        if [[ "$mode" == "image" ]]; then
            # 写实际值：解析当前 SAKURA_AI_IMAGE 环境变量，缺省用默认 latest
            local image="${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}"
            echo "SAKURA_AI_IMAGE=$image"
            echo "COMPOSE_PROJECT_NAME=$DEFAULT_PROD_COMPOSE_PROJECT"
            # 仅写入由本函数生成的 URL-safe hex secret；绝不记录到日志。
            echo "SAKURA_DB_PASSWORD=$db_password"
        fi
    } > "$tmp"

    chmod 600 "$tmp" || {
        rm -f "$tmp"
        fail "无法将 deployment.env 权限设置为 0600；拒绝写入数据库凭据" >&2
        return 1
    }

    # durability：fsync 文件数据后再 atomic rename。
    # sync -d 是 GNU coreutils 的 file-data sync；不支持 -d 时 fallback 全局 sync；
    # 两者都不可用时静默降级（atomic mv 仍是主保护）。
    sync -d "$tmp" 2>/dev/null || sync 2>/dev/null || true
    mv "$tmp" "$DEPLOYMENT_ENV_FILE"
    info "已初始化部署状态: $DEPLOYMENT_ENV_FILE (mode=$mode)"
}

# 原子更新 deployment.env 的 SAKURA_AI_IMAGE：保留其余键值与 0600 权限。
# 与 init_deployment_env 相同的 durability 顺序（tmp -> chmod -> sync -> mv）。
# 用于菜单的手动镜像更新 / stable-development 频道切换；调用方必须先确认
# 没有后台部署 runner 或 updater 活动 job，避免与 daemon 的 atomic write 竞争。
write_deployment_env_image() {
    local new_image="$1" tmp replaced=0 line
    if [[ ! -f "$DEPLOYMENT_ENV_FILE" ]]; then
        fail "缺少部署状态文件: $DEPLOYMENT_ENV_FILE" >&2
        return 1
    fi
    tmp="$DEPLOY_DIR/.deployment.env.image.$$"
    : > "$tmp"
    while IFS= read -r line || [[ -n "$line" ]]; do
        if [[ "$line" == SAKURA_AI_IMAGE=* ]]; then
            printf 'SAKURA_AI_IMAGE=%s\n' "$new_image" >> "$tmp"
            replaced=1
        else
            printf '%s\n' "$line" >> "$tmp"
        fi
    done < "$DEPLOYMENT_ENV_FILE"
    if [[ "$replaced" != "1" ]]; then
        printf 'SAKURA_AI_IMAGE=%s\n' "$new_image" >> "$tmp"
    fi
    if ! chmod 600 "$tmp"; then
        rm -f "$tmp"
        fail "无法将 deployment.env 权限设置为 0600；拒绝写入" >&2
        return 1
    fi
    sync -d "$tmp" 2>/dev/null || sync 2>/dev/null || true
    if ! mv -f -- "$tmp" "$DEPLOYMENT_ENV_FILE"; then
        rm -f "$tmp"
        fail "deployment.env 原子替换失败" >&2
        return 1
    fi
}

# ============================================================
# Host Updater daemon management
# ============================================================

UPDATER_STATE_DIR="${UPDATER_STATE_DIR:-$UPDATER_PROJECT_ROOT/$DEPLOY_DIR/updater}"
UPDATER_BINARY="$UPDATER_STATE_DIR/sakura-ai-updater"
UPDATER_SOCKET_PATH="/run/sakura-ai/updater.sock"
UPDATER_SOURCE_COMPOSE_FILE="$UPDATER_PROJECT_ROOT/docker/docker-compose.yml"
UPDATER_PROD_COMPOSE_FILE="$UPDATER_PROJECT_ROOT/docker/docker-compose.prod.yml"
UPDATER_DEPLOYMENT_ENV_FILE="${UPDATER_DEPLOYMENT_ENV_FILE:-$UPDATER_PROJECT_ROOT/$DEPLOYMENT_ENV_FILE}"
UPDATER_BACKEND_VERSION_FILE="${UPDATER_BACKEND_VERSION_FILE:-$UPDATER_PROJECT_ROOT/backend/__init__.py}"
UPDATER_RELEASE_BASE_URL="https://github.com/Sakura520222/Sakura-AI/releases/download"
UPDATER_RELEASE_API_URL="https://api.github.com/repos/Sakura520222/Sakura-AI/releases/latest"
UPDATER_HEALTH_URL="${UPDATER_HEALTH_URL:-http://localhost:8000/health}"

# 依据持久化部署模式选择 updater 使用的 Compose 定义。
#
# deployment.env 是 updater 的权威运行时状态。这里仅逐行读取精确的
# SAKURA_DEPLOY_MODE=... 字段，绝不 source/eval runtime 文件，避免把其中的
# 值当作 shell 代码执行。source 使用开发 Compose；image 使用生产 Compose。
# 缺失或未知模式属于无效的 3.0.0 部署状态，不提供兼容回退。
read_deployment_value() {
    local key="$1" state_file="${2:-$DEPLOYMENT_ENV_FILE}" value="" line
    if [[ -r "$state_file" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "${line%%=*}" == "$key" ]]; then
                value="${line#*=}"
            fi
        done < "$state_file"
    fi
    printf '%s\n' "$value"
}

read_deployment_mode() {
    read_deployment_value "SAKURA_DEPLOY_MODE" "${1:-$DEPLOYMENT_ENV_FILE}"
}

compose_project_name_is_valid() {
    [[ "$1" =~ ^[a-z0-9][a-z0-9_-]*$ ]]
}

select_production_compose_project() {
    local state_file="${1:-$DEPLOYMENT_ENV_FILE}" project=""
    project="$(read_deployment_value "COMPOSE_PROJECT_NAME" "$state_file")"

    if [[ -z "$project" ]]; then
        fail "missing COMPOSE_PROJECT_NAME in production deployment state" >&2
        return 1
    fi

    if ! compose_project_name_is_valid "$project"; then
        fail "invalid COMPOSE_PROJECT_NAME in deployment state: '$project'" >&2
        return 1
    fi
    if [[ "$project" != "$DEFAULT_PROD_COMPOSE_PROJECT" ]]; then
        fail "unsupported production COMPOSE_PROJECT_NAME '$project'; expected '$DEFAULT_PROD_COMPOSE_PROJECT'" >&2
        return 1
    fi
    COMPOSE_PROJECT="$project"
}

select_compose_for_operation() {
    local requested_prod="${1:-false}"
    if should_use_production_mode "$requested_prod"; then
        COMPOSE_FILE="$PROD_COMPOSE_FILE"
        select_production_compose_project "$DEPLOYMENT_ENV_FILE"
    else
        COMPOSE_FILE="docker/docker-compose.yml"
        COMPOSE_PROJECT=""
    fi
}

should_use_production_mode() {
    local requested_prod="${1:-false}"
    [[ "$requested_prod" == "true" ]] \
        || [[ "$(read_deployment_mode "$DEPLOYMENT_ENV_FILE")" == "image" ]]
}

select_compose_from_deployment_mode() {
    local mode
    mode="$(read_deployment_mode "$UPDATER_DEPLOYMENT_ENV_FILE")"

    case "$mode" in
        image)
            COMPOSE_FILE="$UPDATER_PROD_COMPOSE_FILE"
            select_production_compose_project "$UPDATER_DEPLOYMENT_ENV_FILE"
            ;;
        source)
            COMPOSE_FILE="$UPDATER_SOURCE_COMPOSE_FILE"
            COMPOSE_PROJECT=""
            ;;
        *)
            fail "invalid deployment state: SAKURA_DEPLOY_MODE must be 'source' or 'image'" >&2
            return 1
            ;;
    esac
}

# Host metadata helpers are isolated so Linux uses real inode data while Git Bash
# tests can inject owner/mode values without changing permissions on other directories.
# Linux 使用真实 inode 元数据；Git Bash 测试可注入 owner/mode，避免 chmod 他人目录。
updater_current_uid() { id -u; }
updater_binary_owner_uid() { stat -c '%u' "$1"; }
updater_binary_mode() { stat -c '%a' "$1"; }
updater_directory_owner_uid() { stat -c '%u' "$1"; }
updater_directory_mode() { stat -c '%a' "$1"; }
updater_path_is_symlink() { [[ -L "$1" ]]; }
updater_path_exists() { [[ -e "$1" || -L "$1" ]]; }
updater_chown() { chown "$2:$3" "$1"; }
updater_chmod() { chmod "$@"; }
updater_sync_temp() { sync "$1"; }
updater_sync_state_dir() { sync "$1"; }
updater_flock() { flock -n "$1"; }
updater_mv() { mv -f -- "$1" "$2"; }

# Curl is constrained to the fixed HTTPS release endpoint and bounded time.
# curl 仅允许固定 HTTPS 发布地址，并设置有界超时与重试。
updater_curl() {
    local url="$1" output="$2" headers="${3:-}" http_status curl_rc=0
    local -a args=(
        curl --fail --location
        --proto '=https' --proto-redir '=https'
        --connect-timeout 10 --max-time 120
        --retry 2 --retry-delay 1
        --output "$output"
        --write-out '%{http_code}'
    )
    if [[ -n "$headers" ]]; then
        args+=(--dump-header "$headers")
    fi
    # --fail makes curl return non-zero for HTTP 4xx/5xx, but --write-out still
    # provides the response status. Capture both independently so callers can
    # distinguish an explicit 404 from transport failure (curl reports 000).
    http_status=$("${args[@]}" "$url") || curl_rc=$?
    http_status=${http_status//$'\r'/}
    if [[ ! "$http_status" =~ ^[0-9]{3}$ ]]; then
        http_status=000
    fi
    UPDATER_LAST_HTTP_STATUS="$http_status"
    [[ "$curl_rc" -eq 0 && "$http_status" =~ ^2[0-9][0-9]$ ]]
}

# Read the already-running image version without requiring a source checkout.
# 读取已运行镜像的实际版本，使最小 Curl + Compose 部署无需源码版本文件。
updater_health_payload() {
    curl --fail --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --header 'Accept: application/json' \
        "$UPDATER_HEALTH_URL"
}

# Probe the Host Updater itself. This is deliberately separate from the
# application /health check above: a live UDS listener without matching daemon
# metadata indicates a checkout-replacement orphan and must block a duplicate
# start instead of racing to unlink or bind the same socket.
# Lifecycle safety needs a transport probe, not a health check. Any listener
# that completes an HTTP exchange is live even when it returns 404 or 500.
updater_socket_listener_responds() {
    local http_status curl_rc=0
    if http_status=$(curl --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --output /dev/null --write-out '%{http_code}' \
        --unix-socket "$UPDATER_SOCKET_PATH" \
        --header 'Accept: application/json' \
        http://localhost/v1/health); then
        [[ "$http_status" =~ ^[0-9]{3}$ && "$http_status" != "000" ]]
        return $?
    else
        curl_rc=$?
    fi
    # curl 7 is a failed connect (no listener/stale socket). Once a connection
    # was accepted, malformed, reset, empty, or timed-out HTTP is still proof
    # of a live listener and lifecycle operations must fail closed.
    [[ "$curl_rc" -ne 7 ]]
}

updater_require_root() {
    local uid
    uid=$(updater_current_uid) || return 1
    if [[ "$uid" != "0" ]]; then
        fail "updater lifecycle operation requires root" >&2
        return 1
    fi
}

updater_deployment_is_running() {
    runner_identity_matches
}

updater_require_idle_deployment() {
    if updater_deployment_is_running; then
        fail "deployment is still running in the background; updater lifecycle changes are blocked" >&2
        fail "wait for ./start.sh --status to finish, or explicitly stop it with ./start.sh --stop" >&2
        return 1
    fi
    if runner_pid_is_live; then
        fail "build.pid refers to a live process whose runner identity cannot be verified" >&2
        fail "refusing updater lifecycle changes without matching starttime and command metadata" >&2
        return 1
    fi
}

updater_prepare_stop() {
    if ! updater_socket_listener_responds; then
        return 0
    fi
    if curl --fail --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --request POST \
        --unix-socket "$UPDATER_SOCKET_PATH" \
        --header 'Accept: application/json' \
        http://localhost/v1/lifecycle/prepare-stop >/dev/null; then
        return 0
    fi
    if updater_socket_listener_responds; then
        fail "updater refused the atomic lifecycle gate; an update may be active or the daemon is incompatible" >&2
        fail "wait for active updates to finish; older daemons must be stopped explicitly before replacement" >&2
        return 1
    fi
}

updater_cancel_stop() {
    if updater_socket_listener_responds; then
        curl --fail --silent --show-error \
            --connect-timeout 2 --max-time 5 \
            --request POST \
            --unix-socket "$UPDATER_SOCKET_PATH" \
            --header 'Accept: application/json' \
            http://localhost/v1/lifecycle/cancel-stop >/dev/null 2>&1 || true
    fi
}

resolve_running_image_version() {
    local payload
    if ! payload=$(updater_health_payload); then
        return 1
    fi
    if [[ "$payload" =~ \"version\"[[:space:]]*:[[:space:]]*\"([0-9]+\.[0-9]+\.[0-9]+)\" ]]; then
        printf '%s\n' "${BASH_REMATCH[1]}"
        return 0
    fi
    return 1
}

updater_sha256() {
    sha256sum -- "$1" | awk '{print $1}'
}

updater_mode_has_no_shared_write() {
    local mode="${1:-}"
    [[ "$mode" =~ ^[0-7]+$ ]] || return 1
    (( (8#$mode & 8#022) == 0 ))
}

updater_mode_is_0700() {
    local mode="${1:-}"
    [[ "$mode" =~ ^[0-7]+$ ]] || return 1
    (( 8#$mode == 8#0700 ))
}

updater_binary_is_safe() {
    local binary="$1" owner mode
    if updater_path_is_symlink "$binary"; then
        return 1
    fi
    [[ -f "$binary" && -x "$binary" ]] || return 1
    if ! owner=$(updater_binary_owner_uid "$binary"); then
        return 1
    fi
    if ! mode=$(updater_binary_mode "$binary"); then
        return 1
    fi
    [[ "$owner" == "0" ]] || return 1
    updater_mode_has_no_shared_write "$mode"
}

updater_directory_is_safe() {
    local directory="$1" exact_mode="${2:-0}" owner mode
    if updater_path_is_symlink "$directory"; then
        return 1
    fi
    [[ -d "$directory" ]] || return 1
    if ! owner=$(updater_directory_owner_uid "$directory"); then
        return 1
    fi
    if ! mode=$(updater_directory_mode "$directory"); then
        return 1
    fi
    [[ "$owner" == "0" ]] || return 1
    if [[ "$exact_mode" == "1" ]]; then
        updater_mode_is_0700 "$mode"
    else
        updater_mode_has_no_shared_write "$mode"
    fi
}

updater_prepare_runtime_tmp() {
    local runtime_tmp="$1"
    if updater_path_is_symlink "$runtime_tmp" || [[ -e "$runtime_tmp" && ! -d "$runtime_tmp" ]]; then
        fail "refusing unsafe updater TMPDIR: $runtime_tmp" >&2
        return 1
    fi
    if [[ ! -e "$runtime_tmp" ]]; then
        if ! mkdir -p "$runtime_tmp"; then
            fail "cannot create updater TMPDIR: $runtime_tmp" >&2
            return 1
        fi
        if ! updater_chown "$runtime_tmp" 0 0 || ! updater_chmod 0700 "$runtime_tmp"; then
            fail "cannot secure updater TMPDIR: $runtime_tmp" >&2
            return 1
        fi
    fi
    if ! updater_directory_is_safe "$runtime_tmp" 1; then
        fail "refusing unsafe updater TMPDIR: $runtime_tmp" >&2
        return 1
    fi
}

updater_cleanup_download_temps() {
    local path
    for path in "$@"; do
        if [[ -n "$path" ]]; then
            rm -f -- "$path" 2>/dev/null || true
        fi
    done
}

updater_close_lock() {
    local lock_fd="${1:-}"
    if [[ "$lock_fd" =~ ^[0-9]+$ ]]; then
        eval "exec ${lock_fd}>&-" 2>/dev/null || true
    fi
}

updater_abort_acquisition() {
    local lock_fd="$1"
    shift
    updater_cleanup_download_temps "$@"
    updater_close_lock "$lock_fd"
}

updater_validate_content_length() {
    local payload="$1" headers="$2" expected actual
    [[ -s "$headers" ]] || return 0
    expected=$(awk '
        BEGIN { IGNORECASE = 1 }
        tolower($1) == "content-length:" {
            value = $2
            gsub(/\r/, "", value)
        }
        END { if (value != "") print value }
    ' "$headers") || return 1
    [[ -z "$expected" ]] && return 0
    [[ "$expected" =~ ^[0-9]+$ ]] || return 1
    actual=$(wc -c < "$payload") || return 1
    actual=${actual//[[:space:]]/}
    [[ "$actual" == "$expected" ]]
}

updater_read_expected_checksum() {
    local sums_file="$1" asset="$2" line filename expected="" count=0
    [[ -f "$sums_file" ]] || {
        fail "checksum file is not a regular file" >&2
        return 1
    }
    while IFS= read -r line || [[ -n "$line" ]]; do
        line=${line%$'\r'}
        [[ -n "$line" ]] || {
            fail "malformed SHA256SUMS line" >&2
            return 1
        }
        if [[ "$line" =~ ^([0-9A-Fa-f]{64})[[:space:]]+\*?([^[:space:]]+)$ ]]; then
            filename="${BASH_REMATCH[2]}"
            if [[ "$filename" == "$asset" ]]; then
                expected="${BASH_REMATCH[1]}"
                count=$((count + 1))
            fi
        else
            fail "malformed SHA256SUMS line" >&2
            return 1
        fi
    done < "$sums_file"
    if [[ "$count" -ne 1 ]]; then
        fail "SHA256SUMS must contain exactly one entry for $asset" >&2
        return 1
    fi
    printf '%s\n' "$expected"
}

updater_prepare_state_dir() {
    local state_dir="$UPDATER_STATE_DIR" owner mode
    # symlink / non-directory → fail-closed (never chmod or chown a symlink target)
    if updater_path_is_symlink "$state_dir" || [[ -e "$state_dir" && ! -d "$state_dir" ]]; then
        fail "refusing unsafe updater state directory: $state_dir" >&2
        return 1
    fi
    if [[ ! -e "$state_dir" ]]; then
        if ! mkdir -p "$state_dir"; then
            fail "cannot create updater state directory: $state_dir" >&2
            return 1
        fi
        if ! updater_chown "$state_dir" 0 0 || ! updater_chmod 0700 "$state_dir"; then
            fail "cannot secure updater state directory: $state_dir" >&2
            return 1
        fi
    else
        # existing directory: must be root-owned with no group/other write.
        # Hardening permissions on a non-root-owned directory would mask a real
        # compromise, so owner is verified first and harden only runs on root-owned dirs.
        if ! owner=$(updater_directory_owner_uid "$state_dir"); then
            fail "cannot inspect updater state directory owner: $state_dir" >&2
            return 1
        fi
        [[ "$owner" == "0" ]] || {
            fail "refusing non-root updater state directory (owner=$owner): $state_dir" >&2
            return 1
        }
        if ! updater_directory_is_safe "$state_dir" 0; then
            fail "refusing unsafe updater state directory (group/other writable): $state_dir" >&2
            return 1
        fi
        if ! updater_chmod 0700 "$state_dir"; then
            fail "cannot harden updater state directory to 0700: $state_dir" >&2
            return 1
        fi
    fi
    # final invariant: directory must be root-owned and exactly 0700
    if ! updater_directory_is_safe "$state_dir" 1; then
        fail "refusing unsafe updater state directory after prepare: $state_dir" >&2
        return 1
    fi
    if ! updater_sync_state_dir "$state_dir"; then
        fail "cannot persist updater state directory metadata: $state_dir" >&2
        return 1
    fi
}

# 最近 stable Release 的 updater 版本（GitHub releases/latest API）。
# development 镜像版本可能超前 stable Release（没有对应的 updater 资产），
# install 下载 404 时回退使用最近 stable 版本的 updater binary。
resolve_latest_stable_updater_version() {
    local payload_file="" headers_file="" tag=""
    payload_file=$(mktemp) || return 1
    headers_file=$(mktemp) || { rm -f -- "$payload_file"; return 1; }
    if ! updater_curl "$UPDATER_RELEASE_API_URL" "$payload_file" "$headers_file"; then
        rm -f -- "$payload_file" "$headers_file"
        return 1
    fi
    rm -f -- "$headers_file"
    tag=$(sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"v\([0-9][0-9.]*\)".*$/\1/p' "$payload_file" | head -n 1)
    rm -f -- "$payload_file"
    if [[ ! "$tag" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        return 1
    fi
    printf '%s\n' "$tag"
}

resolve_updater_app_version() {
    local deploy_mode="" image_version="" running_version="" package_version="" line image version

    if [[ -f "$UPDATER_DEPLOYMENT_ENV_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            case "$line" in
                SAKURA_DEPLOY_MODE=*) deploy_mode=${line#SAKURA_DEPLOY_MODE=} ;;
                SAKURA_AI_IMAGE=*)
                    image=${line#SAKURA_AI_IMAGE=}
                    image_version=""
                    if [[ "$image" =~ ^ghcr\.io/sakura520222/sakura-ai:v([0-9]+\.[0-9]+\.[0-9]+)(@sha256:[0-9a-f]{64})?$ ]]; then
                        image_version="${BASH_REMATCH[1]}"
                    fi
                    ;;
            esac
        done < "$UPDATER_DEPLOYMENT_ENV_FILE"
    fi

    case "$deploy_mode" in
        image)
            if [[ -n "$image_version" ]]; then
                version="$image_version"
            elif running_version=$(resolve_running_image_version); then
                version="$running_version"
            fi
            ;;
        source) ;;
        *)
            fail "invalid or missing SAKURA_DEPLOY_MODE in deployment state" >&2
            return 1
            ;;
    esac

    if [[ -z "${version:-}" && -f "$UPDATER_BACKEND_VERSION_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^__version__[[:space:]]*=[[:space:]]*\"([0-9]+\.[0-9]+\.[0-9]+)\"[[:space:]]*$ ]]; then
                package_version="${BASH_REMATCH[1]}"
                break
            fi
        done < "$UPDATER_BACKEND_VERSION_FILE"
    fi

    if [[ "$deploy_mode" == "source" || -z "${version:-}" ]]; then
        version="$package_version"
    fi
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        fail "cannot determine concrete Sakura AI version for $deploy_mode deployment" >&2
        return 1
    fi
    printf '%s\n' "$version"
}

updater_uname_s() { uname -s; }
updater_uname_m() { uname -m; }

resolve_updater_asset() {
    local system arch
    if ! system=$(updater_uname_s); then
        fail "cannot determine updater operating system" >&2
        return 1
    fi
    [[ "$system" == "Linux" ]] || {
        fail "updater binary install supports Linux only" >&2
        return 1
    }
    if ! arch=$(updater_uname_m); then
        fail "cannot determine updater architecture" >&2
        return 1
    fi
    case "$arch" in
        x86_64|amd64) printf '%s\n' 'sakura-ai-updater-linux-amd64' ;;
        aarch64|arm64) printf '%s\n' 'sakura-ai-updater-linux-arm64' ;;
        *)
            fail "unsupported updater architecture: $arch" >&2
            return 1
            ;;
    esac
}

install_updater_binary() {
    local binary="$UPDATER_BINARY" lock_path lock_fd=""
    local version stable_version asset binary_url sums_url
    local binary_tmp="" sums_tmp="" binary_headers_tmp="" sums_headers_tmp=""
    local expected_hash actual_hash binary_http_status

    # Root gate precedes every filesystem or network operation.
    # root gate 必须早于任何文件系统或网络操作。
    if [[ "$(updater_current_uid)" != "0" ]]; then
        fail "updater binary installation requires root" >&2
        return 1
    fi
    if ! updater_prepare_state_dir; then
        return 1
    fi
    if updater_path_exists "$binary" && ! updater_binary_is_safe "$binary"; then
        fail "refusing unsafe updater executable: $binary" >&2
        return 126
    fi

    lock_path="$UPDATER_STATE_DIR/install.lock"
    if updater_path_is_symlink "$lock_path"; then
        fail "refusing symlinked updater install lock: $lock_path" >&2
        return 1
    fi
    if ! exec {lock_fd}>>"$lock_path"; then
        fail "cannot open updater install lock: $lock_path" >&2
        return 1
    fi
    if ! updater_chmod 0600 "$lock_path"; then
        updater_close_lock "$lock_fd"
        fail "cannot secure updater install lock: $lock_path" >&2
        return 1
    fi
    if ! updater_flock "$lock_fd"; then
        updater_close_lock "$lock_fd"
        fail "updater install already in progress" >&2
        return 1
    fi

    if ! version=$(resolve_updater_app_version); then
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! asset=$(resolve_updater_asset); then
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi

    if ! binary_tmp=$(mktemp "$UPDATER_STATE_DIR/.updater-download.XXXXXX"); then
        fail "cannot create updater binary temporary file" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! sums_tmp=$(mktemp "$UPDATER_STATE_DIR/.updater-checksums.XXXXXX"); then
        fail "cannot create updater checksum temporary file" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! binary_headers_tmp=$(mktemp "$UPDATER_STATE_DIR/.updater-binary-headers.XXXXXX"); then
        fail "cannot create updater binary header temporary file" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! sums_headers_tmp=$(mktemp "$UPDATER_STATE_DIR/.updater-checksum-headers.XXXXXX"); then
        fail "cannot create updater checksum header temporary file" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! updater_chmod 0600 "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"; then
        fail "cannot secure updater temporary files; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi

    binary_url="$UPDATER_RELEASE_BASE_URL/v${version}/${asset}"
    sums_url="$UPDATER_RELEASE_BASE_URL/v${version}/SHA256SUMS"
    UPDATER_LAST_HTTP_STATUS=000
    if updater_curl "$binary_url" "$binary_tmp" "$binary_headers_tmp"; then
        :
    else
        binary_http_status="${UPDATER_LAST_HTTP_STATUS:-000}"
        # 目标版本无 Release 资产（development 镜像版本超前 stable 发布）；
        # 回退到最近 stable Release 的 updater binary（updater 随 stable 发布）。
        if [[ "$binary_http_status" == "404" ]] \
            && stable_version=$(resolve_latest_stable_updater_version) \
            && [[ "$stable_version" != "$version" ]]; then
            warn "v${version} 无对应 Release 资产（development 构建超前 stable），回退使用最近 stable v${stable_version} 的 updater" >&2
            version="$stable_version"
            binary_url="$UPDATER_RELEASE_BASE_URL/v${version}/${asset}"
            sums_url="$UPDATER_RELEASE_BASE_URL/v${version}/SHA256SUMS"
            UPDATER_LAST_HTTP_STATUS=000
            if ! updater_curl "$binary_url" "$binary_tmp" "$binary_headers_tmp"; then
                fail "updater binary download failed (incl. stable fallback); old binary unchanged" >&2
                updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
                return 1
            fi
        else
            fail "updater binary download failed; old binary unchanged" >&2
            updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
            return 1
        fi
    fi
    if ! updater_validate_content_length "$binary_tmp" "$binary_headers_tmp"; then
        fail "updater binary Content-Length mismatch; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! updater_curl "$sums_url" "$sums_tmp" "$sums_headers_tmp"; then
        fail "updater checksum download failed; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! updater_validate_content_length "$sums_tmp" "$sums_headers_tmp"; then
        fail "updater checksum Content-Length mismatch; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi

    if ! expected_hash=$(updater_read_expected_checksum "$sums_tmp" "$asset"); then
        fail "invalid updater SHA256SUMS; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! actual_hash=$(updater_sha256 "$binary_tmp"); then
        fail "cannot checksum updater binary; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    actual_hash=${actual_hash//$'\r'/}
    if [[ ! "$actual_hash" =~ ^[0-9A-Fa-f]{64}$ ]] || [[ "${actual_hash,,}" != "${expected_hash,,}" ]]; then
        fail "updater checksum mismatch; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi

    # Pre-commit: secure and fsync the temp inode before touching final path.
    # 提交前先完成权限、inode 安全检查和临时文件 fsync，绝不触碰 final。
    if ! updater_chmod 0700 "$binary_tmp"; then
        fail "cannot make updater temporary binary executable; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! updater_binary_is_safe "$binary_tmp"; then
        fail "updater temporary binary failed safety validation; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! updater_sync_temp "$binary_tmp"; then
        fail "updater temporary binary fsync failed; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if updater_path_exists "$binary" && ! updater_binary_is_safe "$binary"; then
        fail "refusing unsafe updater executable at commit point; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 126
    fi
    if ! updater_mv "$binary_tmp" "$binary"; then
        fail "atomic updater binary install failed; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    binary_tmp=""

    # Rename is the commit point; directory metadata durability is a post-commit gate.
    # rename 是提交点；目录 metadata durability 属于提交后的独立门禁。
    if ! updater_sync_state_dir "$UPDATER_STATE_DIR"; then
        fail "updater durability failure: new inode may already be installed; backend install skipped" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi
    if ! updater_binary_is_safe "$binary"; then
        fail "post-commit final safety failure: new inode may already be installed; backend install skipped" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
    fi

    updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
    return 0
}

updater_backend() {
    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    local runtime_tmp="$UPDATER_STATE_DIR/tmp"
    if updater_binary_is_safe "$binary"; then
        if ! updater_prepare_runtime_tmp "$runtime_tmp"; then
            return 1
        fi
        TMPDIR="$runtime_tmp" "$binary" backend "$@"
    elif [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]]; then
        "${SAKURA_UPDATER_PYTHON:-python3}" -m sakura_ai_updater backend "$@"
    elif updater_path_exists "$binary"; then
        fail "refusing unsafe updater executable: $binary" >&2
        return 126
    else
        fail "updater executable not installed: $binary" >&2
        return 127
    fi
}

cmd_updater_install() {
    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    local was_running=0
    local uid

    # Production entry is gated before any binary/path filesystem inspection.
    # production 入口先做 root gate，避免非 root 触碰 updater 路径。
    uid=$(updater_current_uid) || return 1
    if [[ "$uid" != "0" ]]; then
        fail "updater install requires root" >&2
        return 1
    fi

    if [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]]; then
        if updater_backend install \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" "$@"; then
            return 0
        else
            local dev_install_rc=$?
            return "$dev_install_rc"
        fi
    fi

    if updater_binary_is_safe "$binary"; then
        if updater_backend is-running \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1; then
            was_running=1
        fi
        if install_updater_binary; then
            :
        else
            local install_rc=$?
            return "$install_rc"
        fi
        if updater_backend install \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" "$@"; then
            :
        else
            local existing_backend_rc=$?
            return "$existing_backend_rc"
        fi
    elif updater_path_exists "$binary"; then
        fail "refusing unsafe updater executable: $binary" >&2
        return 126
    else
        if install_updater_binary; then
            :
        else
            local install_rc=$?
            return "$install_rc"
        fi
        if updater_backend install \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" "$@"; then
            :
        else
            local acquired_backend_rc=$?
            return "$acquired_backend_rc"
        fi
    fi

    if [[ "$was_running" -eq 1 ]]; then
        warn "updater binary installed while daemon was already running; restart-required (not restarting automatically)" >&2
        return 0
    fi
    # 安装完成后自动拉起 daemon（daemon 未运行时；与 ensure_updater_running
    # 的引导语义一致，避免"已安装但未运行"的中间状态）。
    if updater_start_daemon; then
        :
    else
        warn "updater 已安装但 daemon 启动失败" >&2
        return 1
    fi
}

updater_existing_state_dir_is_safe() {
    local state_dir="$UPDATER_STATE_DIR" owner
    if ! updater_path_exists "$state_dir"; then
        return 0
    fi
    if updater_path_is_symlink "$state_dir" || [[ ! -d "$state_dir" ]]; then
        fail "refusing unsafe updater state directory: $state_dir" >&2
        return 1
    fi
    if ! owner=$(updater_directory_owner_uid "$state_dir") || [[ "$owner" != "0" ]]; then
        fail "refusing non-root updater state directory: $state_dir" >&2
        return 1
    fi
    if ! updater_directory_is_safe "$state_dir" 0; then
        fail "refusing group/other-writable updater state directory: $state_dir" >&2
        return 1
    fi
}

stop_verified_updater() {
    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    if updater_binary_is_safe "$binary"; then
        updater_backend stop \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" || return $?
    elif [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]] && ! updater_path_exists "$binary"; then
        updater_backend stop \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" || return $?
    elif updater_path_exists "$binary"; then
        fail "refusing to execute unsafe updater binary while stopping: $binary" >&2
        return 126
    fi
    if updater_socket_listener_responds; then
        fail "updater socket is still live after stop; refusing to replace or remove files" >&2
        fail "inspect the verified listener with: sudo ss -xlpn | grep -F '$UPDATER_SOCKET_PATH'" >&2
        return 1
    fi
}

cmd_updater_reinstall() {
    local was_running=0 install_rc=0 start_rc=0 stop_rc=0
    updater_require_root || return $?
    updater_require_idle_deployment || return $?
    if updater_socket_listener_responds; then
        was_running=1
    fi
    updater_prepare_stop || return $?
    if stop_verified_updater; then
        :
    else
        stop_rc=$?
        updater_cancel_stop
        return "$stop_rc"
    fi
    if cmd_updater_install; then
        :
    else
        install_rc=$?
        if [[ "$was_running" -eq 1 ]]; then
            warn "updater reinstallation failed; restarting the preserved installed binary" >&2
            if ! ensure_updater_running; then
                fail "updater reinstallation failed and the preserved daemon could not be restarted" >&2
            fi
        fi
        return "$install_rc"
    fi
    if ensure_updater_running; then
        :
    else
        start_rc=$?
        return "$start_rc"
    fi
    ok "updater 已重新安装并启动"
    updater_backend status \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH"
}

cmd_updater_uninstall() {
    local stop_rc=0
    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    updater_require_root || return $?
    updater_require_idle_deployment || return $?
    updater_existing_state_dir_is_safe || return $?
    if [[ "$binary" != "$UPDATER_STATE_DIR/sakura-ai-updater" ]]; then
        fail "refusing unexpected updater binary path during uninstall: $binary" >&2
        return 1
    fi
    updater_prepare_stop || return $?
    if stop_verified_updater; then
        :
    else
        stop_rc=$?
        updater_cancel_stop
        return "$stop_rc"
    fi

    # Only exact, updater-owned paths under the verified state directory are removed.
    rm -f -- \
        "$binary" \
        "$UPDATER_STATE_DIR/daemon-meta.json" \
        "$UPDATER_STATE_DIR/updater.log" \
        "$UPDATER_STATE_DIR/updater.lock" \
        "$UPDATER_STATE_DIR/update-state.json" \
        "$UPDATER_STATE_DIR/install.lock"
    rm -rf -- "$UPDATER_STATE_DIR/tmp"
    rmdir -- "$UPDATER_STATE_DIR" 2>/dev/null || true
    if [[ -S "$UPDATER_SOCKET_PATH" || -L "$UPDATER_SOCKET_PATH" ]]; then
        rm -f -- "$UPDATER_SOCKET_PATH"
    fi
    ok "updater 已卸载"
}

# 拉起 updater daemon（install 与 ensure_updater_running 共用；避免递归）。
updater_start_daemon() {
    select_compose_from_deployment_mode
    if ! updater_backend start \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" \
        --compose-file "$COMPOSE_FILE" \
        --deployment-env "$UPDATER_DEPLOYMENT_ENV_FILE"; then
        fail "updater 启动失败" >&2
        fail "  若无 binary，设 SAKURA_UPDATER_DEV=1 用源码模式" >&2
        return 1
    fi
    ok "updater daemon 已运行"
}

ensure_updater_running() {
    if updater_backend is-running \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1; then
        return 0
    fi
    if updater_socket_listener_responds; then
        fail "updater socket is live but daemon metadata is missing or stale; refusing duplicate start" >&2
        fail "stop the verified listener process, then run: sudo ./start.sh updater install && sudo ./start.sh updater start" >&2
        return 1
    fi
    warn "updater daemon 未运行，正在拉起..."

    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    local install_rc
    if updater_binary_is_safe "$binary"; then
        if updater_backend install \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" "$@"; then
            :
        else
            install_rc=$?
            fail "updater bootstrap failed; see previous error" >&2
            return "$install_rc"
        fi
    elif [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]]; then
        if updater_backend install \
            --state-dir "$UPDATER_STATE_DIR" \
            --socket-path "$UPDATER_SOCKET_PATH" "$@"; then
            :
        else
            install_rc=$?
            fail "updater bootstrap failed; see previous error" >&2
            return "$install_rc"
        fi
    elif updater_path_exists "$binary"; then
        fail "refusing unsafe updater executable: $binary" >&2
        return 126
    else
        # cmd_updater_install 成功即已完成安装并启动 daemon，直接返回。
        if cmd_updater_install; then
            return 0
        else
            install_rc=$?
            fail "updater bootstrap failed; see previous error" >&2
            return "$install_rc"
        fi
    fi

    updater_start_daemon
}

cmd_updater() {
    local action="${1:-status}"
    shift || true
    case "$action" in
        install)
            cmd_updater_install "$@"
            ;;
        reinstall)
            cmd_updater_reinstall "$@"
            ;;
        uninstall)
            cmd_updater_uninstall "$@"
            ;;
        start)
            ensure_updater_running "$@"
            ;;
        stop|status|is-running)
            updater_backend "$action" \
                --state-dir "$UPDATER_STATE_DIR" \
                --socket-path "$UPDATER_SOCKET_PATH" "$@"
            ;;
        *)
            fail "未知 updater 子命令: $action"
            echo "用法: ./start.sh updater [install|reinstall|uninstall|start|stop|status|is-running]" >&2
            return 1
            ;;
    esac
}

# ============================================================
# 检测 docker compose
# ============================================================

detect_compose() {
    local env_file_opt=""
    local project_opt=""
    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        env_file_opt="--env-file $DEPLOYMENT_ENV_FILE"
    fi
    if [[ -n "$COMPOSE_PROJECT" ]]; then
        project_opt="--project-name $COMPOSE_PROJECT"
    fi
    if docker compose version &>/dev/null; then
        echo "docker compose $env_file_opt $project_opt -f $COMPOSE_FILE"
    elif command -v docker-compose &>/dev/null; then
        fail "Docker Compose V2 is required; install the 'docker compose' plugin (docker-compose V1 cannot run the Host Updater)" >&2
        echo ""
    else
        echo ""
    fi
}

# ============================================================
# 子命令: --status
# ============================================================

cmd_status() {
    local build_active=0 runner_verified=0
    if is_running; then
        build_active=1
        runner_verified=1
    elif runner_pid_is_live; then
        build_active=1
        warn "build.pid is live but runner identity is unverified; lifecycle signals are disabled"
    fi

    # updater daemon 状态快照。构建仍在运行或应用尚未健康时只报告状态，不提前
    # acquisition；image :latest 必须等 /health 提供具体版本后才能安全安装。
    if updater_backend is-running \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1; then
        ok "host updater daemon 运行中"
    else
        if [[ "$build_active" -eq 1 ]]; then
            warn "host updater daemon 未运行；部署仍在进行，等待应用健康后再恢复"
        elif updater_binary_is_safe "${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}" \
            || [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]] \
            || updater_path_exists "${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"; then
            if ensure_updater_running; then
                ok "host updater daemon 运行中"
            else
                warn "host updater daemon 不可用"
            fi
        elif updater_health_payload >/dev/null 2>&1; then
            if ensure_updater_running; then
                ok "host updater daemon 运行中"
            else
                warn "host updater daemon 不可用"
            fi
        else
            warn "host updater daemon 未运行；应用尚未健康，暂不尝试安装 updater"
        fi
    fi

    if [[ "$build_active" -eq 1 ]]; then
        local pid
        pid=$(runner_read_pid)
        local phase
        phase=$(get_phase)
        echo ""
        info "构建进程正在运行 (PID: $pid, 阶段: $phase)"
        echo ""
        echo "最近日志 (最后 20 行):"
        echo "──────────────────────────"
        tail -20 "$BUILD_LOG" 2>/dev/null || echo "(无日志)"
        echo "──────────────────────────"
        echo ""
        echo "使用 ./start.sh --attach 查看完整实时日志"
        if [[ "$runner_verified" -ne 1 ]]; then
            warn "该进程缺少匹配的 starttime/command 元数据；请人工检查，脚本不会向其发送信号"
        fi
    else
        local phase result
        phase=$(get_phase)
        result=$(get_result)
        echo ""
        if [[ "$phase" == "done" ]]; then
            ok "上次启动流程已完成"
        elif [[ "$phase" == "none" ]]; then
            info "尚未运行过启动流程"
        else
            warn "上次流程在阶段 [$phase] 结束 (结果: ${result:-未知})"
            echo "  重新运行 ./start.sh 可从中断处继续"
        fi
        echo ""
    fi
}

# ============================================================
# 子命令: --attach
# ============================================================

cmd_attach() {
    if ! is_running; then
        if runner_pid_is_live; then
            fail "build.pid refers to a live process whose runner identity cannot be verified"
            exit 1
        fi
        fail "没有正在进行的构建进程"
        exit 1
    fi
    info "附加到构建日志 (Ctrl+C 退出查看，不会中断构建)..."
    trap 'trap - INT; return 0' INT
    tail -f "$BUILD_LOG" || true
    trap - INT
}

# ============================================================
# 子命令: --stop
# ============================================================

cmd_stop() {
    if ! is_running; then
        if runner_pid_is_live; then
            fail "build.pid refers to a live process whose runner identity cannot be verified"
            exit 1
        fi
        fail "没有正在进行的构建进程"
        exit 1
    fi
    local pid
    pid=$(runner_read_pid)
    warn "正在终止构建进程 (PID: $pid)..."
    runner_identity_matches || {
        fail "refusing to signal an unverified build runner"
        exit 1
    }
    # Signal the process group first while the verified session leader exists;
    # signalling the leader first could let it exit before its children receive TERM.
    kill -TERM -- -"$pid" 2>/dev/null || true
    runner_identity_matches && kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    if runner_identity_matches; then
        kill -KILL -- -"$pid" 2>/dev/null || true
        runner_identity_matches && kill -KILL "$pid" 2>/dev/null || true
    fi
    clear_runner_identity
    ok "已终止"
}

# ============================================================
# 主流程: 后台构建 runner
# ============================================================

build_runner() {
    local rebuild=$1 prod=$2
    local need_build=false
    local need_pip=false
    local current_hash=""
    local dockerfile_hash=""

    # runner 会重新 source start.sh，因此必须从持久化状态恢复 Compose 文件和项目。
    select_compose_for_operation "$prod"

    COMPOSE=$(detect_compose)
    if [[ -z "$COMPOSE" ]]; then
        fail "Docker Compose 未安装"
        set_phase "preflight" "fail"
        return 1
    fi

    # --- preflight ---
    set_phase "preflight"

    if $prod; then
        # 生产模式：镜像不可变，跳过本地构建判定（requirements/Dockerfile 哈希），
        # 直接拉取 GHCR 已发布镜像；--rebuild 仅表示重新 up -d 拉取最新镜像
        info "生产模式：跳过本地构建，直接拉取镜像"
    else
        if [[ -f "requirements.txt" ]]; then
            current_hash=$(md5sum requirements.txt | awk '{print $1}')
        fi
        if [[ -f "docker/Dockerfile" ]]; then
            dockerfile_hash=$(md5sum docker/Dockerfile | awk '{print $1}')
        fi

        if [[ "$rebuild" == "true" ]]; then
            info "强制重建模式"
            need_build=true
        elif [[ ! -f "$HASH_FILE" ]]; then
            info "首次部署，需要构建镜像"
            need_build=true
        elif [[ -f "$DOCKERFILE_HASH_FILE" ]] && [[ "$dockerfile_hash" != "$(cat "$DOCKERFILE_HASH_FILE")" ]]; then
            info "检测到 Dockerfile 变更，需要重建镜像"
            need_build=true
        elif [[ "$current_hash" != "$(cat "$HASH_FILE")" ]]; then
            info "检测到依赖变更，将使用临时容器安装新依赖"
            need_pip=true
        else
            ok "依赖未变更，跳过构建"
        fi
    fi

    # --- build ---
    if $need_build; then
        set_phase "build"
        info "停止现有容器..."
        $COMPOSE down >> "$BUILD_LOG" 2>&1 || true

        info "构建并启动服务..."
        if $COMPOSE up -d --build >> "$BUILD_LOG" 2>&1; then
            echo "$current_hash" > "$HASH_FILE"
            [[ -n "$dockerfile_hash" ]] && echo "$dockerfile_hash" > "$DOCKERFILE_HASH_FILE"
            ok "镜像构建完成，依赖哈希已更新"
        else
            set_phase "build" "fail"
            return 1
        fi
    # --- pip install ---
    elif $need_pip; then
        set_phase "pip"
        info "停止现有容器..."
        $COMPOSE down >> "$BUILD_LOG" 2>&1 || true

        info "在临时容器内安装新依赖..."
        local temp_container="sakura-ai-pip-${current_hash:0:8}"
        local image_tag="sakura-ai:pip-${current_hash:0:8}"
        docker rm -f "$temp_container" >/dev/null 2>&1 || true

        if docker run --name "$temp_container" \
            -v "$(pwd)/requirements.txt:/app/requirements.txt:ro" \
            sakura-ai:latest \
            sh -c "pip install -r /app/requirements.txt" >> "$BUILD_LOG" 2>&1; then

            info "将依赖写入镜像 $image_tag ..."
            docker commit \
                --change 'CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]' \
                "$temp_container" "$image_tag" >> "$BUILD_LOG" 2>&1
            docker tag "$image_tag" sakura-ai:latest >> "$BUILD_LOG" 2>&1
            docker rm -f "$temp_container" >/dev/null 2>&1 || true
            echo "$current_hash" > "$HASH_FILE"
            ok "依赖安装完成，镜像已更新"

            set_phase "start"
            info "启动服务（已更新依赖镜像）..."
            $COMPOSE up -d >> "$BUILD_LOG" 2>&1
        else
            warn "临时容器 pip install 失败，自动回退到重建镜像..."
            docker rm -f "$temp_container" >/dev/null 2>&1 || true
            if $COMPOSE up -d --build >> "$BUILD_LOG" 2>&1; then
                echo "$current_hash" > "$HASH_FILE"
                ok "镜像重建完成，依赖哈希已更新"
            else
                set_phase "pip" "fail"
                return 1
            fi
        fi
    # --- prod: 拉取 GHCR 镜像部署 ---
    elif $prod; then
        if [[ "$rebuild" == "true" ]]; then
            info "--rebuild 生产模式：重新拉取最新镜像"
        fi
        # 不写本地哈希：镜像版本由 GHCR 发布管理，本地 requirements/Dockerfile 哈希无意义
        info "停止现有容器..."
        $COMPOSE down >> "$BUILD_LOG" 2>&1 || true
        set_phase "pull"
        info "拉取最新镜像"
        if ! compose_pull_with_native_progress; then
            set_phase "pull" "fail"
            return 1
        fi
        set_phase "start"
        info "启动服务..."
        if $COMPOSE up -d >> "$BUILD_LOG" 2>&1; then
            ok "服务已启动"
        else
            set_phase "start" "fail"
            return 1
        fi
    else
        # 无需构建
        set_phase "start"
        info "停止现有容器..."
        $COMPOSE down >> "$BUILD_LOG" 2>&1 || true
        info "启动服务（无构建）..."
        $COMPOSE up -d >> "$BUILD_LOG" 2>&1
    fi

    # --- health check ---
    set_phase "health"
    info "等待服务启动..."
    local elapsed=0
    while [[ $elapsed -lt $HEALTH_TIMEOUT ]]; do
        if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
            ok "服务已就绪 (${elapsed}s)"
            break
        fi
        sleep 3
        elapsed=$((elapsed + 3))
        # Write progress to log so --attach can see it
        echo "  ... health check ${elapsed}/${HEALTH_TIMEOUT}s" >> "$BUILD_LOG"
    done

    if [[ $elapsed -ge $HEALTH_TIMEOUT ]]; then
        warn "服务启动超时 (${HEALTH_TIMEOUT}s)"
    fi

    # host updater daemon 恢复（spec §11.4）
    ensure_updater_running || warn "updater daemon 未拉起（更新功能不可用，服务不受影响）"

    # --- done ---
    set_phase "done"

    echo "" >> "$BUILD_LOG"
    echo "==============================" >> "$BUILD_LOG"
    ok "启动流程完成" | tee -a "$BUILD_LOG"
    echo "" >> "$BUILD_LOG"
    echo "服务状态:" >> "$BUILD_LOG"
    $COMPOSE ps >> "$BUILD_LOG" 2>&1 || true
    echo "" >> "$BUILD_LOG"

    clear_runner_identity
}

# ============================================================
# 主入口
# ============================================================

# ============================================================
# 交互画面渲染 / Interactive screen rendering
# ============================================================

# 画面 = 标题 + 文本行缓冲。ui_line/ui_blank 向缓冲追加文本，ui_render 清屏
# 后整体重绘；新增或删除菜单项、状态行只需增删对应的 ui_* 调用，渲染逻辑
# 本身不用改。
UI_TITLE=""
UI_LINES=()

ui_title() { UI_TITLE="$1"; }
ui_reset() { UI_LINES=(); }
ui_blank() { UI_LINES+=(""); }
ui_line()  { UI_LINES+=("$1"); }

ui_render() {
    clear 2>/dev/null || printf '\033[2J\033[H'
    echo -e "${BOLD}${UI_TITLE}${RESET}"
    echo -e "${BOLD}==========================${RESET}"
    local line
    for line in ${UI_LINES[@]+"${UI_LINES[@]}"}; do
        echo -e "$line"
    done
    ui_reset
}

# 交互动作结束后暂停，等待回车再重绘画面。
ui_pause() {
    local prompt="${1:-按回车键返回菜单...}" _
    echo ""
    read -rp "$prompt" _ || exit 0
}

# ============================================================
# 镜像频道工具 / Image channel helpers
# ============================================================

# 频道约定与 backend/services/container_registry.py 一致：
#   stable      标签 vX.Y.Z，移动别名 latest
#   development 标签 dev-<timestamp>-vX.Y.Z-<sha>，移动别名 edge
DEFAULT_IMAGE_REPOSITORY="ghcr.io/sakura520222/sakura-ai"

# 取镜像引用的 repository 部分（去掉 :tag 与 @digest）。
image_repo_of() {
    local image="${1%%@*}"
    local tail="${image##*/}"
    if [[ "$tail" == *:* ]]; then
        printf '%s\n' "${image%:*}"
    else
        printf '%s\n' "$image"
    fi
}

# 取镜像引用的 tag 部分；无 tag（仅 repository 或 digest-pinned）时输出空串。
image_tag_of() {
    local image="${1%%@*}"
    local tail="${image##*/}"
    if [[ "$tail" == *:* ]]; then
        printf '%s\n' "${image##*:}"
    fi
}

# 依据 tag 判定频道：latest / vX.Y.Z -> stable；edge / dev-* -> development。
image_channel_of() {
    local tag
    tag=$(image_tag_of "$1")
    case "$tag" in
        latest|v*.*)          printf 'stable\n' ;;
        edge|dev-*)           printf 'development\n' ;;
        *)                    printf 'unknown\n' ;;
    esac
}

# 频道对应的移动别名 tag（CI 维护其指向各自频道 head）。
channel_alias() {
    case "$1" in
        stable)      printf 'latest\n' ;;
        development) printf 'edge\n' ;;
        *)           return 1 ;;
    esac
}

image_digest_of() {
    local image="$1" repo line digest="" count=0
    repo=$(image_repo_of "$image")
    [[ -n "$repo" ]] || return 1
    while IFS= read -r line; do
        case "$line" in
            "$repo"@sha256:*)
                digest=${line#"$repo"@sha256:}
                count=$((count + 1))
                ;;
        esac
    done < <(docker image inspect --format='{{range .RepoDigests}}{{println .}}{{end}}' "$image" 2>/dev/null)
    if [[ "$count" -eq 1 && "$digest" =~ ^[0-9a-f]{64}$ ]]; then
        printf 'sha256:%s\n' "$digest"
        return 0
    fi
    return 1
}

# ============================================================
# Updater IPC / Updater daemon v1 IPC over UDS
# ============================================================
# 与 WebUI（backend/services/updater_client.py）调用的是同一组端点；这里用
# curl --unix-socket 触发完整的 job 流水线（preflight/pull/activate/health）。
UPDATE_JOB_TIMEOUT=900

updater_ipc_get() {
    local path="$1"
    curl --silent --connect-timeout 2 --max-time 10 \
        --unix-socket "$UPDATER_SOCKET_PATH" \
        -H 'Accept: application/json' \
        "http://localhost$path" 2>/dev/null
}

# 从紧凑 JSON 中提取第一个字符串字段值；失败返回非零。
updater_ipc_field() {
    local key="$1" payload="$2" pattern
    pattern="\"$key\"[[:space:]]*:[[:space:]]*\"([^\"]+)\""
    [[ "$payload" =~ $pattern ]] || return 1
    printf '%s\n' "${BASH_REMATCH[1]}"
}

updater_daemon_is_running() {
    updater_backend is-running \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1
}

updater_has_active_job() {
    local payload
    payload=$(updater_ipc_get /v1/status) || return 1
    [[ -n "$payload" ]] || return 1
    [[ "$payload" =~ \"has_active_job\"[[:space:]]*:[[:space:]]*true ]]
}

# 轮询 job 直至终态（success / failed / rolled_back）。
updater_ipc_wait_job() {
    local job_id="$1" payload state last="" elapsed=0
    info "等待 updater job 完成 ($job_id)..."
    while true; do
        payload=$(updater_ipc_get "/v1/jobs/$job_id") || payload=""
        if state=$(updater_ipc_field state "$payload"); then
            if [[ "$state" != "$last" ]]; then
                info "job 状态: $state"
                last="$state"
            fi
            case "$state" in
                success)                return 0 ;;
                failed|rolled_back)     return 1 ;;
            esac
        fi
        if [[ "$elapsed" -ge "$UPDATE_JOB_TIMEOUT" ]]; then
            warn "等待超时 (${UPDATE_JOB_TIMEOUT}s)；job 可能仍在后台执行"
            return 1
        fi
        sleep 2
        elapsed=$((elapsed + 2))
    done
}

# 失败时输出 job 日志（原始 JSON）辅助诊断。
updater_ipc_show_job_logs() {
    local job_id="$1" payload
    payload=$(updater_ipc_get "/v1/jobs/$job_id/logs") || payload=""
    if [[ -z "$payload" ]]; then
        warn "无法获取 job 日志"
        return 0
    fi
    echo ""
    info "job 日志 (原始 JSON):"
    echo "$payload"
}

# ============================================================
# 镜像更新与频道切换 / Image update and channel switch
# ============================================================

require_image_deployment() {
    local mode
    mode=$(read_deployment_mode)
    if [[ "$mode" != "image" ]]; then
        warn "当前部署模式不是生产镜像 (${mode:-未初始化})；该操作仅适用于 image 部署"
        info "可先执行「生产镜像部署」完成 image 模式初始化"
        return 1
    fi
}

require_idle_image_deployment() {
    if is_running; then
        warn "后台部署正在进行；请等待完成或先停止正在进行的构建"
        return 1
    fi
    if runner_pid_is_live; then
        fail "build.pid refers to a live process whose runner identity cannot be verified"
        return 1
    fi
    if updater_daemon_is_running && updater_has_active_job; then
        warn "updater 正在执行更新 job；请等待其完成后再操作"
        return 1
    fi
}

# 轻量 /health 摘要（短超时），仅用于菜单头展示。
menu_health_summary() {
    local payload version="" channel="" revision="" pattern
    payload=$(curl --silent --connect-timeout 1 --max-time 2 \
        -H 'Accept: application/json' "$UPDATER_HEALTH_URL" 2>/dev/null) || return 1
    [[ -n "$payload" ]] || return 1
    version=$(updater_ipc_field version "$payload") || return 1
    pattern="\"build\".*\"channel\"[[:space:]]*:[[:space:]]*\"([a-z]+)\""
    if [[ "$payload" =~ $pattern ]]; then
        channel="${BASH_REMATCH[1]}"
    fi
    pattern="\"revision\"[[:space:]]*:[[:space:]]*\"([0-9a-f]{7,40})\""
    if [[ "$channel" == "development" && "$payload" =~ $pattern ]]; then
        revision="+${BASH_REMATCH[1]:0:7}"
    fi
    printf 'v%s (%s%s)' "$version" "${channel:-unknown}" "$revision"
    return 0
}

# 轮询 /health 直至就绪；成功后打印运行版本与频道。
menu_wait_healthy() {
    local elapsed=0 payload version channel revision pattern
    info "等待服务启动..."
    while [[ "$elapsed" -lt "$HEALTH_TIMEOUT" ]]; do
        payload=$(curl --silent --connect-timeout 2 --max-time 5 \
            -H 'Accept: application/json' "$UPDATER_HEALTH_URL" 2>/dev/null) || payload=""
        if [[ -n "$payload" ]]; then
            version=$(updater_ipc_field version "$payload") || version="?"
            channel=""
            pattern="\"build\".*\"channel\"[[:space:]]*:[[:space:]]*\"([a-z]+)\""
            if [[ "$payload" =~ $pattern ]]; then
                channel="${BASH_REMATCH[1]}"
            fi
            revision=""
            pattern="\"revision\"[[:space:]]*:[[:space:]]*\"([0-9a-f]{7,40})\""
            if [[ "$channel" == "development" && "$payload" =~ $pattern ]]; then
                revision="+${BASH_REMATCH[1]:0:7}"
            fi
            ok "服务已就绪: v${version} (${channel:-unknown}${revision})"
            return 0
        fi
        sleep 3
        elapsed=$((elapsed + 3))
    done
    warn "服务启动超时 (${HEALTH_TIMEOUT}s)"
    return 1
}

# 手动 Compose 更新路径：拉取频道别名镜像 -> 记录到 deployment.env -> up -d。
# 只重建镜像发生变化的 web 容器（与 updater ImageAdapter.activate 一致），
# 不会 down 掉 MySQL/Redis。
apply_channel_image() {
    local channel="$1" repository channel_tag image compose_cmd digest
    if ! channel_tag=$(channel_alias "$channel"); then
        fail "无法识别目标频道: $channel"
        return 1
    fi
    repository=$(image_repo_of "$(read_deployment_value "SAKURA_AI_IMAGE")")
    if [[ -z "$repository" ]]; then
        repository="$DEFAULT_IMAGE_REPOSITORY"
    fi
    image="$repository:$channel_tag"

    info "拉取镜像: $image"
    docker pull "$image" || return 1

    if ! digest=$(image_digest_of "$image"); then
        fail "无法解析镜像 digest: $image"
        return 1
    fi
    image="$image@$digest"

    write_deployment_env_image "$image" || return 1
    ok "部署状态已指向: $image"

    select_compose_for_operation true || return 1
    compose_cmd=$(detect_compose)
    if [[ -z "$compose_cmd" ]]; then
        fail "Docker Compose 未安装"
        return 1
    fi
    COMPOSE="$compose_cmd"
    info "应用新镜像..."
    $COMPOSE up -d || return 1
    menu_wait_healthy
    # 与 do_start 一致：镜像部署完成后自动拉起 host updater daemon（失败不阻断）
    ensure_updater_running || warn "updater daemon 未拉起（更新功能不可用，服务不受影响）"
}

# 更新当前频道的镜像到最新（菜单 [3]）。
# stable + daemon 运行时复用 updater 的 job 流水线（preflight/pull/activate/
# health）；其余情况（development 频道、daemon 未运行）回退为直接 Compose 拉取
# 频道别名。updater 的空 body 目标固定解析为最新 stable Release（jobs.check()），
# development 频道更新要求结构化 target（WebUI 从镜像目录选择），CLI 端不重复
# 实现 GHCR 目录解析，故 development 不走 daemon 路径。
cmd_update_image() {
    local image channel payload job_id body_file http_status pattern
    require_image_deployment || return 1
    require_idle_image_deployment || return 1
    # 补全残缺部署状态（缺数据库密码/项目名/镜像时自动补写），确保后续镜像
    # 解析与 updater 安装有完整的权威状态。
    init_deployment_env || return 1

    image=$(read_deployment_value "SAKURA_AI_IMAGE")
    channel=$(image_channel_of "$image")
    info "当前镜像: ${image:-未记录}"
    info "目标频道: $channel"

    if [[ "$channel" != "stable" && "$channel" != "development" ]]; then
        fail "当前镜像无法识别频道；镜像更新仅支持 stable/development 别名"
        return 1
    fi

    if [[ "$channel" == "stable" ]] && updater_daemon_is_running; then
        info "通过 host updater daemon 更新 stable 频道..."
        body_file=$(mktemp) || return 1
        # 不用 --fail：422 preflight_failed 的响应体需要读取并分类。
        if ! http_status=$(curl --silent --show-error \
            --connect-timeout 2 --max-time 300 \
            --unix-socket "$UPDATER_SOCKET_PATH" \
            -H 'Content-Type: application/json' -H 'Accept: application/json' \
            --request POST --data '{}' \
            --output "$body_file" \
            --write-out '%{http_code}' \
            http://localhost/v1/update); then
            rm -f -- "$body_file"
            fail "无法连接 host updater daemon"
            return 1
        fi
        payload=$(cat "$body_file" 2>/dev/null) || payload=""
        rm -f -- "$body_file"

        if [[ "$http_status" =~ ^2[0-9][0-9]$ ]]; then
            if ! job_id=$(updater_ipc_field job_id "$payload"); then
                fail "updater 响应缺少 job_id: $payload"
                return 1
            fi
            if updater_ipc_wait_job "$job_id"; then
                ok "镜像更新完成"
                return 0
            fi
            fail "镜像更新未成功 (job: $job_id)"
            updater_ipc_show_job_logs "$job_id"
            return 1
        fi

        pattern="\"error\"[[:space:]]*:[[:space:]]*\"preflight_failed\""
        if [[ "$http_status" == "422" && "$payload" =~ $pattern ]]; then
            # already_current 未通过 = 目标 digest 与运行 digest 相同：已是最新。
            pattern="\"name\":\"already_current\"[[:space:]]*,[[:space:]]*\"passed\":false"
            if [[ "$payload" =~ $pattern ]]; then
                ok "stable 频道已是最新版本，无需更新"
                return 0
            fi
            pattern="\"name\":\"channel_switch_confirmed\"[[:space:]]*,[[:space:]]*\"passed\":false"
            if [[ "$payload" =~ $pattern ]]; then
                fail "updater 检测到运行中容器不在 stable 频道，未带频道切换确认，拒绝更新"
                info "请使用菜单 [4] 切换频道，或在 WebUI 版本管理器中操作"
                return 1
            fi
            fail "updater 预检未通过 (422):"
            echo "$payload"
            return 1
        fi

        fail "updater 更新提交失败 (HTTP $http_status): $payload"
        return 1
    fi

    if updater_daemon_is_running; then
        info "development 频道刷新直接使用 Compose 别名镜像（updater 空 body 时目标固定为 stable）"
    else
        warn "host updater daemon 未运行；回退为手动 Compose 更新"
    fi
    apply_channel_image "$channel"
}

# 切换 stable/development 频道（菜单 [4]）：
# 拉取目标频道别名 -> 更新 deployment.env -> up -d -> 等待健康检查。
cmd_switch_channel() {
    local image channel repository choice target confirm
    require_image_deployment || return 1
    require_idle_image_deployment || return 1
    # 与 cmd_update_image 一致：残缺部署状态先自动补全。
    init_deployment_env || return 1

    image=$(read_deployment_value "SAKURA_AI_IMAGE")
    channel=$(image_channel_of "$image")
    repository=$(image_repo_of "$image")
    if [[ -z "$repository" ]]; then
        repository="$DEFAULT_IMAGE_REPOSITORY"
    fi

    echo ""
    info "当前镜像: ${image:-未记录}"
    info "当前频道: $channel"
    echo ""
    echo -e "  ${BOLD}[1]${RESET} stable      正式频道 ($repository:latest)"
    echo -e "  ${BOLD}[2]${RESET} development 开发频道 ($repository:edge)"
    echo -e "  ${BOLD}[0]${RESET} 取消"
    echo ""
    read -rp "  切换到: " choice
    case "$choice" in
        1) target=stable ;;
        2) target=development ;;
        *) info "已取消"; return 0 ;;
    esac

    if [[ "$target" == "$channel" ]]; then
        info "已在 $target 频道；将执行频道内更新"
    else
        warn "切换频道会替换运行中的 web 镜像并重启服务"
        read -rp "  确认切换到 $target? (y/N): " confirm
        if [[ ! "$confirm" =~ ^[yY]$ ]]; then
            info "已取消"
            return 0
        fi
    fi
    apply_channel_image "$target"
}

# ============================================================
# 主菜单 / Main menu
# ============================================================

# 状态头与菜单项都是 UI_LINES 里的普通文本行；调整画面只需增删 ui_* 调用。
render_main_menu() {
    local mode mode_label image channel health phase daemon
    mode=$(read_deployment_mode)
    case "$mode" in
        image)  mode_label="image (生产镜像)" ;;
        source) mode_label="source (源码构建)" ;;
        *)      mode_label="未初始化" ;;
    esac
    image=$(read_deployment_value "SAKURA_AI_IMAGE")
    channel=$(image_channel_of "$image")
    health=$(menu_health_summary) || health="不可达"
    phase=$(get_phase)
    if is_running; then
        phase="部署进行中 ($phase)"
    else
        phase="空闲 (上次: $phase)"
    fi
    if updater_daemon_is_running; then
        daemon="运行中"
    else
        daemon="未运行"
    fi

    ui_title "Sakura AI 管理菜单"
    ui_blank
    ui_line "  ${DIM}部署模式: ${mode_label}${RESET}"
    if [[ "$mode" == "image" ]]; then
        ui_line "  ${DIM}当前镜像: ${image:-未记录}${RESET}"
        ui_line "  ${DIM}镜像频道: ${channel}${RESET}"
    fi
    ui_line "  ${DIM}运行版本: ${health}${RESET}"
    ui_line "  ${DIM}后台任务: ${phase}${RESET}"
    ui_line "  ${DIM}Updater : ${daemon}${RESET}"
    ui_blank
    ui_line "  ${BOLD}[1]${RESET} 启动服务 (自动检测构建)"
    ui_line "  ${BOLD}[2]${RESET} 强制重建镜像并启动"
    ui_line "  ${BOLD}[3]${RESET} 更新镜像 (当前频道)"
    ui_line "  ${BOLD}[4]${RESET} 切换镜像频道 (正式/开发)"
    ui_line "  ${BOLD}[5]${RESET} 查看构建/运行状态"
    ui_line "  ${BOLD}[6]${RESET} 附加到构建日志"
    ui_line "  ${BOLD}[7]${RESET} 停止正在进行的构建"
    ui_line "  ${BOLD}[8]${RESET} 查看服务容器状态"
    ui_line "  ${BOLD}[9]${RESET} 停止服务"
    ui_line "  ${BOLD}[10]${RESET} 生产镜像部署"
    ui_line "  ${BOLD}[11]${RESET} Updater daemon 管理"
    ui_line "  ${BOLD}[12]${RESET} 卸载 Sakura AI"
    ui_line "  ${BOLD}[0]${RESET} 退出"
    ui_blank
}

# 在子 shell 中执行菜单动作：动作内部的 exit 与失败都不会终止菜单循环。
menu_run() {
    echo ""
    ( "$@" ) || true
    ui_pause
}

menu_loop() {
    local choice
    while true; do
        render_main_menu
        ui_render
        read -rp "  请选择操作: " choice || exit 0
        case "$choice" in
            1)  menu_run do_start false ;;
            2)  menu_run do_start true ;;
            3)  menu_run cmd_update_image ;;
            4)  menu_run cmd_switch_channel ;;
            5)  menu_run cmd_status ;;
            6)  menu_run cmd_attach ;;
            7)  menu_run cmd_stop ;;
            8)  menu_run do_ps ;;
            9)  menu_run do_down ;;
            10) menu_run do_start false true ;;
            11) updater_menu_loop ;;
            12) menu_run cmd_uninstall ;;
            0)  info "已退出" ; exit 0 ;;
            *)  warn "无效选项: $choice" ; sleep 1 ;;
        esac
    done
}

# Updater daemon 管理子菜单（host updater CLI 的交互入口）
# 复用 cmd_updater：包括 install/reinstall/uninstall 与 daemon 生命周期操作。
render_updater_menu() {
    ui_title "Updater daemon 管理"
    ui_blank
    ui_line "  ${BOLD}[1]${RESET} 安装 updater (需 root)"
    ui_line "  ${BOLD}[2]${RESET} 启动 updater daemon"
    ui_line "  ${BOLD}[3]${RESET} 停止 updater daemon"
    ui_line "  ${BOLD}[4]${RESET} 查看 updater daemon 状态"
    ui_line "  ${BOLD}[5]${RESET} 重新安装并启动 updater"
    ui_line "  ${BOLD}[6]${RESET} 卸载 updater"
    ui_line "  ${BOLD}[0]${RESET} 返回主菜单"
    ui_blank
}

updater_menu_loop() {
    local choice
    while true; do
        render_updater_menu
        ui_render
        read -rp "  请选择操作: " choice || exit 0
        case "$choice" in
            1) menu_run cmd_updater install ;;
            2) menu_run cmd_updater start  ;;
            3) menu_run cmd_updater stop   ;;
            4) menu_run cmd_updater status ;;
            5) menu_run cmd_updater reinstall ;;
            6) menu_run cmd_updater uninstall ;;
            0) return 0 ;;
            *) warn "无效选项: $choice" ; sleep 1 ;;
        esac
    done
}

do_ps() {
    local prod=${1:-false}
    select_compose_for_operation "$prod"
    # Show container status
    local compose_cmd
    compose_cmd=$(detect_compose)
    if [[ -z "$compose_cmd" ]]; then
        fail "Docker Compose 未安装"
        return 1
    fi
    echo ""
    $compose_cmd ps
}

confirm_sakura_uninstall() {
    local purge="$1" assume_yes="$2" expected answer
    if [[ "$assume_yes" == "true" ]]; then
        return 0
    fi
    if [[ ! -t 0 ]]; then
        fail "uninstall confirmation requires an interactive terminal; pass --yes for automation" >&2
        return 1
    fi
    echo ""
    warn "即将停止并删除 Sakura AI 容器、网络和 Host Updater。"
    if [[ "$purge" == "true" ]]; then
        warn "--purge 还会永久删除 Compose 数据卷（包括 MySQL/Redis）和 .deploy 状态。"
        expected="PURGE SAKURA-AI"
    else
        info "Docker 数据卷和 .deploy/deployment.env 将保留，可供以后重新部署。"
        expected="UNINSTALL"
    fi
    read -r -p "输入 '$expected' 继续: " answer
    if [[ "$answer" != "$expected" ]]; then
        fail "确认内容不匹配，已取消卸载" >&2
        return 1
    fi
}

stop_deployment_for_uninstall() {
    local pid elapsed=0
    if ! updater_deployment_is_running; then
        if runner_pid_is_live; then
            fail "refusing to signal live PID from build.pid without verified runner identity" >&2
            return 1
        fi
        clear_runner_identity
        return 0
    fi
    pid=$(runner_read_pid) || return 1
    warn "正在停止后台部署进程 (PID: $pid)..."
    runner_identity_matches || return 1
    kill -TERM -- -"$pid" 2>/dev/null || true
    runner_identity_matches && kill -TERM "$pid" 2>/dev/null || true
    while runner_identity_matches && [[ "$elapsed" -lt 10 ]]; do
        sleep 1
        elapsed=$((elapsed + 1))
    done
    if runner_identity_matches; then
        kill -KILL -- -"$pid" 2>/dev/null || true
        runner_identity_matches && kill -KILL "$pid" 2>/dev/null || true
    fi
    clear_runner_identity
}

sakura_compose_uninstall() {
    local purge="$1"
    local -a compose_cmd=(docker compose)
    command -v docker >/dev/null 2>&1 || {
        fail "Docker 未安装，无法卸载 Compose 服务" >&2
        return 1
    }
    docker compose version >/dev/null 2>&1 || {
        fail "Docker Compose V2 未安装" >&2
        return 1
    }
    select_compose_from_deployment_mode || return $?
    if [[ -f "$UPDATER_DEPLOYMENT_ENV_FILE" ]]; then
        compose_cmd+=(--env-file "$UPDATER_DEPLOYMENT_ENV_FILE")
    fi
    if [[ -n "$COMPOSE_PROJECT" ]]; then
        compose_cmd+=(--project-name "$COMPOSE_PROJECT")
    fi
    compose_cmd+=(-f "$COMPOSE_FILE" down --remove-orphans)
    if [[ "$purge" == "true" ]]; then
        compose_cmd+=(--volumes)
    fi
    "${compose_cmd[@]}"
}

purge_sakura_deployment_state() {
    local target="$UPDATER_PROJECT_ROOT/$DEPLOY_DIR"
    local expected="$UPDATER_PROJECT_ROOT/.deploy"
    if [[ "$target" != "$expected" || "$target" == "/" || "$target" == "$UPDATER_PROJECT_ROOT" ]]; then
        fail "refusing unsafe deployment state target: $target" >&2
        return 1
    fi
    if updater_path_is_symlink "$target"; then
        fail "refusing symlinked deployment state target: $target" >&2
        return 1
    fi
    rm -rf -- "$target"
}

cmd_uninstall() {
    local purge=false assume_yes=false arg
    for arg in "$@"; do
        case "$arg" in
            --purge) purge=true ;;
            --yes) assume_yes=true ;;
            *)
                fail "未知卸载参数: $arg" >&2
                echo "用法: ./start.sh uninstall [--purge] [--yes]" >&2
                return 1
                ;;
        esac
    done
    updater_require_root || return $?
    confirm_sakura_uninstall "$purge" "$assume_yes" || return $?
    stop_deployment_for_uninstall || return $?

    info "正在删除 Sakura AI Compose 服务..."
    sakura_compose_uninstall "$purge" || return $?
    cmd_updater_uninstall || return $?
    if [[ "$purge" == "true" ]]; then
        purge_sakura_deployment_state || return $?
        ok "Sakura AI 已完全卸载；Compose 数据卷和部署状态已删除"
    else
        ok "Sakura AI 已卸载；Docker 数据卷和部署状态已保留"
    fi
    info "项目源码/脚本目录未删除，可手动检查后移除: $UPDATER_PROJECT_ROOT"
}

do_down() {
    local prod=${1:-false}
    select_compose_for_operation "$prod"
    local compose_cmd
    compose_cmd=$(detect_compose)
    if [[ -z "$compose_cmd" ]]; then
        fail "Docker Compose 未安装"
        return 1
    fi
    echo ""
    info "停止服务..."
    $compose_cmd down
    ok "服务已停止"
}

# Actual start logic (called from menu or CLI args)
do_start() {
    local rebuild=${1:-false}
    local prod=${2:-false}

    echo ""
    echo -e "${BOLD}Sakura AI 启动脚本${RESET}"
    echo -e "${BOLD}==========================${RESET}"

    # Check Docker
    if ! command -v docker &>/dev/null; then
        fail "Docker 未安装"
        exit 1
    fi

    # 显式 --prod 或持久化 image 部署都使用生产 compose。这样最小化镜像部署
    # 后再次运行交互菜单的“启动服务”也不会误入缺少源码文件的开发构建路径。
    if should_use_production_mode "$prod"; then
        if [[ "$prod" != "true" ]]; then
            info "检测到持久化 image 部署，继续使用生产模式"
        fi
        prod=true
        info "生产模式：使用生产 compose ($PROD_COMPOSE_FILE)"
    fi

    # 先初始化部署状态（detect_compose 依赖 deployment.env 是否存在来决定 --env-file）
    mkdir -p "$DEPLOY_DIR"
    init_deployment_env
    select_compose_for_operation "$prod"

    # Detect compose
    COMPOSE=$(detect_compose)
    if [[ -z "$COMPOSE" ]]; then
        fail "Docker Compose 未安装"
        exit 1
    fi

    ok "环境检查完成"

    # Create directories
    mkdir -p logs "$DEPLOY_DIR" workplace Skills

    # If a build is already running, attach to it
    if is_running; then
        local pid
        pid=$(runner_read_pid)
        warn "构建进程已在运行 (PID: $pid)"
        echo ""
        info "附加到日志 (Ctrl+C 退出查看，不会中断构建)..."
        echo ""
        trap 'trap - INT; return 0' INT
        tail -f "$BUILD_LOG" || true
        trap - INT
        exit 0
    fi
    if runner_pid_is_live; then
        fail "build.pid refers to a live process whose runner identity cannot be verified"
        fail "refusing to start a second deployment runner; inspect PID $(runner_read_pid) manually"
        exit 1
    fi

    # If previous run completed successfully, do a clean start
    local phase
    phase=$(get_phase)
    if [[ "$phase" == "done" ]]; then
        rm -f "$PHASE_FILE" "$PHASE_RESULT"
    fi

    echo ""
    info "构建将在后台运行，SSH 断线不影响进度"
    info "日志文件: $BUILD_LOG"
    echo ""

    # Rotate log
    : > "$BUILD_LOG"

    # Write a self-contained runner script.
    # It sources start.sh with _START_SH_SOURCED=1 so functions are loaded
    # but main() is not executed, then calls build_runner directly.
    local runner_script
    runner_script="$DEPLOY_DIR/_runner.sh"
    local abs_script_dir
    abs_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    cat > "$runner_script" <<RUNNER_EOF
#!/usr/bin/env bash
set -euo pipefail
export _START_SH_SOURCED=1
cd "${abs_script_dir}"
source "${abs_script_dir}/start.sh"
trap 'clear_runner_identity' EXIT
export prod="${prod}"
build_runner "${rebuild}" "${prod}"
RUNNER_EOF
    chmod +x "$runner_script"

    # Launch in a completely detached session:
    #   setsid → new session, detached from controlling terminal
    #   nohup  → ignore SIGHUP when SSH disconnects
    setsid nohup bash "$runner_script" >> "$BUILD_LOG" 2>&1 &
    local bg_pid=$!
    echo "$bg_pid" > "$(runner_pid_file_path)"
    if ! runner_write_identity "$bg_pid" "$runner_script"; then
        fail "后台构建进程身份记录失败；拒绝留下不可验证的 runner"
        kill -TERM -- -"$bg_pid" 2>/dev/null || true
        kill -TERM "$bg_pid" 2>/dev/null || true
        clear_runner_identity
        return 1
    fi

    disown "$bg_pid" 2>/dev/null || true

    # Brief pause to let the background process start
    sleep 0.5

    ok "后台构建已启动 (PID: $bg_pid)"
    echo ""
    echo -e "  ${DIM}./start.sh --status${RESET}  查看进度"
    echo -e "  ${DIM}./start.sh --attach${RESET}  实时日志"
    echo ""

    # Auto-attach to log — trap SIGINT so Ctrl+C only stops tail, not the build
    info "自动附加日志 (Ctrl+C 退出查看，不会中断构建)..."
    echo ""
    trap 'trap - INT; return 0' INT
    tail -f "$BUILD_LOG" || true
    trap - INT
}

main() {
    if [[ "${1:-}" == "uninstall" ]]; then
        shift
        cmd_uninstall "$@"
        exit $?
    fi

    # updater 子命令（位置参数，优先于 flag 解析）
    if [[ "${1:-}" == "updater" ]]; then
        shift
        cmd_updater "$@"
        exit $?
    fi

    # Parse args
    local rebuild=false
    local prod=false
    local cmd=""
    for arg in "$@"; do
        case "$arg" in
            --rebuild)   rebuild=true ;;
            --prod)      prod=true ;;
            --status)    cmd=status ;;
            --attach)    cmd=attach ;;
            --stop)      cmd=stop ;;
            --ps)        cmd=ps ;;
            --down)      cmd=down ;;
            --help|-h)
                echo "用法: ./start.sh [选项]"
                echo ""
                echo "选项:"
                echo "  (无参数)    交互式菜单（支持更新镜像、切换 stable/development 频道）"
                echo "  --rebuild   强制重建镜像并启动"
                echo "  --prod      生产模式：拉取 GHCR 镜像一键部署（跳过本地构建）"
                echo "  --status    查看当前构建/运行状态"
                echo "  --attach    附加到正在进行的构建日志"
                echo "  --stop      停止正在进行的构建"
                echo "  --ps        查看服务容器状态"
                echo "  --down      停止服务"
                echo "  --help      显示帮助"
                echo "  uninstall [--purge] [--yes]  卸载服务；默认保留数据，--purge 删除数据卷"
                echo "  updater [action]  管理 host updater daemon（含 reinstall/uninstall；生产操作需 root）"
                echo ""
                echo "断线续跑:"
                echo "  构建过程在后台运行，SSH 断开不会中断。"
                echo "  重连后使用 --status 查看进度，--attach 查看日志。"
                exit 0
                ;;
            *)
                echo "未知参数: $arg"
                echo "使用 --help 查看帮助"
                exit 1
                ;;
        esac
    done

    # Handle subcommands
    case "$cmd" in
        status) cmd_status; exit 0 ;;
        attach) cmd_attach; exit $? ;;
        stop)   cmd_stop; exit 0 ;;
        ps)     do_ps "$prod"; exit 0 ;;
        down)   do_down "$prod"; exit 0 ;;
    esac

    # No subcommand args -> interactive menu
    if [[ -z "$cmd" && "$rebuild" == "false" && "$prod" == "false" ]]; then
        menu_loop
    else
        do_start "$rebuild" "$prod"
    fi
}

if [[ "${_START_SH_SOURCED:-}" != "1" ]]; then
    main "$@"
fi
