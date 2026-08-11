#!/usr/bin/env bash
# Sakura AI 快速启动脚本
#
# 支持断线续跑：构建/安装步骤在后台 nohup 运行，SSH 断线不中断。
# 重新运行脚本时会自动跳过已完成的阶段。
#
# 用法:
#   ./start.sh                # 启动（自动检测是否需要构建）
#   ./start.sh --rebuild      # 强制重建镜像
#   ./start.sh --prod         # 生产模式：拉取 GHCR 镜像一键部署（跳过本地构建）
#   ./start.sh --status       # 查看当前构建/运行状态
#   ./start.sh --attach       # 附加到正在进行的构建日志
#   ./start.sh --stop         # 停止正在进行的构建
#   ./start.sh --ps           # 查看服务容器状态
#   ./start.sh --down         # 停止服务
#   ./start.sh updater [action]  # 管理 host updater daemon（install/start/stop/status）
#   ./start.sh --help         # 显示帮助

set -euo pipefail

# ============================================================
# 配置
# ============================================================

COMPOSE_FILE="docker/docker-compose.yml"
PROD_COMPOSE_FILE="docker/docker-compose.prod.yml"
DEPLOY_DIR=".deploy"
BUILD_LOG="$DEPLOY_DIR/build.log"
PHASE_FILE="$DEPLOY_DIR/phase"         # 当前阶段: preflight / build / pip / start / health / done
PHASE_RESULT="$DEPLOY_DIR/phase_result" # 上一阶段结果: ok / fail
PID_FILE="$DEPLOY_DIR/build.pid"
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

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

wait_for_pid() {
    local pid=$1 label=${2:-process}
    local elapsed=0
    # Print a live dot while waiting
    while kill -0 "$pid" 2>/dev/null; do
        sleep 2
        elapsed=$((elapsed + 2))
        printf "\r⏳ %s 进行中... %ds " "$label" "$elapsed"
    done
    echo ""
    wait "$pid"
    return $?
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

init_deployment_env() {
    local mode="source"
    if ${prod:-false}; then
        mode="image"
    fi

    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        local persisted_mode=""
        local persisted_password=""
        local line
        while IFS= read -r line || [[ -n "$line" ]]; do
            case "$line" in
                SAKURA_DEPLOY_MODE=*) persisted_mode="${line#SAKURA_DEPLOY_MODE=}" ;;
                SAKURA_DB_PASSWORD=*) persisted_password="${line#SAKURA_DB_PASSWORD=}" ;;
            esac
        done < "$DEPLOYMENT_ENV_FILE"

        if [[ -n "$persisted_password" ]]; then
            if [[ ! "$persisted_password" =~ ^[0-9a-f]{64}$ ]]; then
                fail "deployment.env 中的 SAKURA_DB_PASSWORD 格式无效；拒绝启动以避免数据库凭据不一致" >&2
                return 1
            fi
            chmod 600 "$DEPLOYMENT_ENV_FILE" || {
                fail "无法将 deployment.env 权限收紧为 0600；拒绝启动以保护数据库凭据" >&2
                return 1
            }
            return 0
        fi

        # source compose 不创建内置 MySQL，旧 source 状态可以继续运行。
        # image/--prod 状态必须由管理员完成一次显式密码迁移，不能在已有
        # mysql_data 上静默生成新密码，否则应用和数据库会立即失联。
        if [[ "$persisted_mode" == "image" || "${prod:-false}" == "true" ]]; then
            fail "现有 deployment.env 缺少 SAKURA_DB_PASSWORD；请先按 README 的 legacy 密码迁移步骤 ALTER USER，再写入同一 64 位十六进制密码" >&2
            return 1
        fi
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

# ============================================================
# Host Updater daemon management
# ============================================================

UPDATER_STATE_DIR="$DEPLOY_DIR/updater"
UPDATER_BINARY="$UPDATER_STATE_DIR/sakura-ai-updater"
UPDATER_SOCKET_PATH="/run/sakura-ai/updater.sock"
UPDATER_DEPLOYMENT_ENV_FILE="${UPDATER_DEPLOYMENT_ENV_FILE:-$DEPLOYMENT_ENV_FILE}"
UPDATER_BACKEND_VERSION_FILE="${UPDATER_BACKEND_VERSION_FILE:-backend/__init__.py}"
UPDATER_RELEASE_BASE_URL="https://github.com/Sakura520222/Sakura-AI/releases/download"
UPDATER_HEALTH_URL="${UPDATER_HEALTH_URL:-http://localhost:8000/health}"

# 依据持久化部署模式选择 updater 使用的 Compose 定义。
#
# deployment.env 是 updater 的权威运行时状态。这里仅逐行读取精确的
# SAKURA_DEPLOY_MODE=... 字段，绝不 source/eval runtime 文件，避免把其中的
# 值当作 shell 代码执行。历史 source/缺失状态继续使用开发 Compose；image
# 状态必须选择生产 Compose，不能因 start.sh 新进程默认值而回落到开发定义。
select_compose_from_deployment_mode() {
    local mode="" line
    if [[ -r "$UPDATER_DEPLOYMENT_ENV_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            case "$line" in
                SAKURA_DEPLOY_MODE=*) mode="${line#SAKURA_DEPLOY_MODE=}" ;;
            esac
        done < "$UPDATER_DEPLOYMENT_ENV_FILE"
    fi

    case "$mode" in
        image)
            COMPOSE_FILE="$PROD_COMPOSE_FILE"
            ;;
        source)
            COMPOSE_FILE="docker/docker-compose.yml"
            ;;
        "")
            COMPOSE_FILE="docker/docker-compose.yml"
            warn "SAKURA_DEPLOY_MODE missing; using development compose"
            ;;
        *)
            COMPOSE_FILE="docker/docker-compose.yml"
            warn "unknown SAKURA_DEPLOY_MODE='$mode'; using development compose"
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
    local url="$1" output="$2" headers="${3:-}" http_status
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
    if ! http_status=$("${args[@]}" "$url"); then
        return 1
    fi
    http_status=${http_status//$'\r'/}
    [[ "$http_status" =~ ^2[0-9][0-9]$ ]]
}

# Read the already-running image version without requiring a source checkout.
# 读取已运行镜像的实际版本，使最小 Curl + Compose 部署无需源码版本文件。
updater_health_payload() {
    curl --fail --silent --show-error \
        --connect-timeout 2 --max-time 5 \
        --header 'Accept: application/json' \
        "$UPDATER_HEALTH_URL"
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
    local version asset binary_url sums_url
    local binary_tmp="" sums_tmp="" binary_headers_tmp="" sums_headers_tmp=""
    local expected_hash actual_hash

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
    if ! updater_curl "$binary_url" "$binary_tmp" "$binary_headers_tmp"; then
        fail "updater binary download failed; old binary unchanged" >&2
        updater_abort_acquisition "$lock_fd" "$binary_tmp" "$sums_tmp" "$binary_headers_tmp" "$sums_headers_tmp"
        return 1
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
    fi
}

ensure_updater_running() {
    if updater_backend is-running \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1; then
        return 0
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
        if cmd_updater_install; then
            :
        else
            install_rc=$?
            fail "updater bootstrap failed; see previous error" >&2
            return "$install_rc"
        fi
    fi

    select_compose_from_deployment_mode
    if ! updater_backend start \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" \
        --compose-file "$COMPOSE_FILE" \
        --deployment-env "$UPDATER_DEPLOYMENT_ENV_FILE"; then
        fail "updater 启动失败"
        fail "  若无 binary，设 SAKURA_UPDATER_DEV=1 用源码模式"
        return 1
    fi
    ok "updater daemon 已运行"
}

cmd_updater() {
    local action="${1:-status}"
    shift || true
    case "$action" in
        install)
            cmd_updater_install "$@"
            ;;
        start)
            select_compose_from_deployment_mode
            updater_backend start \
                --state-dir "$UPDATER_STATE_DIR" \
                --socket-path "$UPDATER_SOCKET_PATH" \
                --compose-file "$COMPOSE_FILE" \
                --deployment-env "$UPDATER_DEPLOYMENT_ENV_FILE" "$@"
            ;;
        stop|status|is-running)
            updater_backend "$action" \
                --state-dir "$UPDATER_STATE_DIR" \
                --socket-path "$UPDATER_SOCKET_PATH" "$@"
            ;;
        *)
            fail "未知 updater 子命令: $action"
            echo "用法: ./start.sh updater [install|start|stop|status|is-running]" >&2
            return 1
            ;;
    esac
}

# ============================================================
# 检测 docker compose
# ============================================================

detect_compose() {
    local env_file_opt=""
    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        env_file_opt="--env-file $DEPLOYMENT_ENV_FILE"
    fi
    if docker compose version &>/dev/null; then
        echo "docker compose $env_file_opt -f $COMPOSE_FILE"
    elif command -v docker-compose &>/dev/null; then
        echo "docker-compose $env_file_opt -f $COMPOSE_FILE"
    else
        echo ""
    fi
}

# ============================================================
# 子命令: --status
# ============================================================

cmd_status() {
    # host updater daemon 恢复尝试（spec §11.4）
    ensure_updater_running || warn "host updater daemon 不可用"

    # updater daemon 状态快照
    if updater_backend is-running \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1; then
        ok "host updater daemon 运行中"
    else
        warn "host updater daemon 未运行"
    fi

    if is_running; then
        local pid
        pid=$(cat "$PID_FILE")
        local phase
        phase=$(get_phase)
        echo ""
        info "构建进程正在运行 (PID: $pid, 阶段: $phase)"
        echo ""
        echo "📋 最近日志 (最后 20 行):"
        echo "──────────────────────────"
        tail -20 "$BUILD_LOG" 2>/dev/null || echo "(无日志)"
        echo "──────────────────────────"
        echo ""
        echo "💡 使用 ./start.sh --attach 查看完整实时日志"
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
        fail "没有正在进行的构建进程"
        exit 1
    fi
    local pid
    pid=$(cat "$PID_FILE")
    warn "正在终止构建进程 (PID: $pid)..."
    kill "$pid" 2>/dev/null || true
    # Also kill the entire process group
    kill -- -"$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
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

    # 选择 compose 文件：生产模式用生产 compose（runner 进程 source 后变量被重置，
    # 需在此显式重新指定）；源码模式保持默认开发 compose
    if $prod; then
        COMPOSE_FILE="$PROD_COMPOSE_FILE"
    else
        COMPOSE_FILE="docker/docker-compose.yml"
    fi

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
        set_phase "start"
        info "停止现有容器..."
        $COMPOSE down >> "$BUILD_LOG" 2>&1 || true
        info "启动服务（拉取最新镜像）..."
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
    echo "📊 服务状态:" >> "$BUILD_LOG"
    $COMPOSE ps >> "$BUILD_LOG" 2>&1 || true
    echo "" >> "$BUILD_LOG"

    rm -f "$PID_FILE"
}

# ============================================================
# 主入口
# ============================================================

show_menu() {
    echo ""
    echo -e "${BOLD}🚀 Sakura AI 启动脚本${RESET}"
    echo -e "${BOLD}==========================${RESET}"
    echo ""
    echo -e "  ${BOLD}[1]${RESET} 启动服务 (自动检测构建)"
    echo -e "  ${BOLD}[2]${RESET} 强制重建镜像并启动"
    echo -e "  ${BOLD}[3]${RESET} 查看构建/运行状态"
    echo -e "  ${BOLD}[4]${RESET} 附加到构建日志"
    echo -e "  ${BOLD}[5]${RESET} 停止正在进行的构建"
    echo -e "  ${BOLD}[6]${RESET} 查看服务容器状态"
    echo -e "  ${BOLD}[7]${RESET} 停止服务"
    echo -e "  ${BOLD}[8]${RESET} 生产镜像部署 (--prod)"
    echo -e "  ${BOLD}[9]${RESET} Updater daemon 管理"
    echo -e "  ${BOLD}[0]${RESET} 退出"
    echo ""

    local choice
    read -rp "  请选择操作: " choice
    case "$choice" in
        1) do_start false ;;
        2) do_start true  ;;
        3) cmd_status     ;;
        4) cmd_attach     ;;
        5) cmd_stop       ;;
        6) do_ps          ;;
        7) do_down        ;;
        8) do_start false true ;;
        9) do_updater_menu ;;
        0) info "已退出" ; exit 0 ;;
        *) warn "无效选项: $choice" ; exit 1 ;;
    esac
}

# Updater daemon 管理子菜单（host updater CLI 的交互入口）
# 复用 cmd_updater：install/start/stop/status 对应底层同名子命令。
do_updater_menu() {
    echo ""
    echo -e "${BOLD}Updater daemon 管理${RESET}"
    echo -e "${BOLD}--------------------------${RESET}"
    echo ""
    echo -e "  ${BOLD}[1]${RESET} 安装 updater (需 root)"
    echo -e "  ${BOLD}[2]${RESET} 启动 updater daemon"
    echo -e "  ${BOLD}[3]${RESET} 停止 updater daemon"
    echo -e "  ${BOLD}[4]${RESET} 查看 updater daemon 状态"
    echo -e "  ${BOLD}[0]${RESET} 返回"
    echo ""

    local choice
    read -rp "  请选择操作: " choice
    case "$choice" in
        1) cmd_updater install ;;
        2) cmd_updater start  ;;
        3) cmd_updater stop   ;;
        4) cmd_updater status ;;
        0) return 0 ;;
        *) warn "无效选项: $choice" ; return 1 ;;
    esac
}

do_ps() {
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

do_down() {
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
    echo -e "${BOLD}🚀 Sakura AI 启动脚本${RESET}"
    echo -e "${BOLD}==========================${RESET}"

    # Check Docker
    if ! command -v docker &>/dev/null; then
        fail "Docker 未安装"
        exit 1
    fi

    # 生产模式使用生产 compose（跳过本地构建，直接拉取 GHCR 镜像）
    if $prod; then
        COMPOSE_FILE="$PROD_COMPOSE_FILE"
        info "生产模式：使用生产 compose ($PROD_COMPOSE_FILE)"
    fi

    # 先初始化部署状态（detect_compose 依赖 deployment.env 是否存在来决定 --env-file）
    mkdir -p "$DEPLOY_DIR"
    init_deployment_env

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
        pid=$(cat "$PID_FILE")
        warn "构建进程已在运行 (PID: $pid)"
        echo ""
        info "附加到日志 (Ctrl+C 退出查看，不会中断构建)..."
        echo ""
        trap 'trap - INT; return 0' INT
        tail -f "$BUILD_LOG" || true
        trap - INT
        exit 0
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
export prod="${prod}"
build_runner "${rebuild}" "${prod}"
RUNNER_EOF
    chmod +x "$runner_script"

    # Launch in a completely detached session:
    #   setsid → new session, detached from controlling terminal
    #   nohup  → ignore SIGHUP when SSH disconnects
    setsid nohup bash "$runner_script" >> "$BUILD_LOG" 2>&1 &
    local bg_pid=$!
    echo "$bg_pid" > "$PID_FILE"

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
                echo "  (无参数)    交互式菜单"
                echo "  --rebuild   强制重建镜像并启动"
                echo "  --prod      生产模式：拉取 GHCR 镜像一键部署（跳过本地构建）"
                echo "  --status    查看当前构建/运行状态"
                echo "  --attach    附加到正在进行的构建日志"
                echo "  --stop      停止正在进行的构建"
                echo "  --ps        查看服务容器状态"
                echo "  --down      停止服务"
                echo "  --help      显示帮助"
                echo "  updater [action]  管理 host updater daemon（生产 install/start 需 root；action 默认 status）"
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
        ps)     do_ps; exit 0 ;;
        down)   do_down; exit 0 ;;
    esac

    # No subcommand args -> interactive menu
    if [[ -z "$cmd" && "$rebuild" == "false" && "$prod" == "false" ]]; then
        show_menu
    else
        do_start "$rebuild" "$prod"
    fi
}

if [[ "${_START_SH_SOURCED:-}" != "1" ]]; then
    main "$@"
fi
