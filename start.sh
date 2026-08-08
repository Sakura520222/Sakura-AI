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

# 首次启动时初始化部署状态：写入部署模式（source/image）与实际镜像引用。
# - 已存在则不覆盖（updater 或之前初始化已写入）。
# - 写实际值（非 ${...} 表达式）：deployment.env 记录"当时实际选择的镜像"。
# - durability：write temp → fsync(sync -d) → atomic mv，满足 spec §9.5。
# - digest 具体化（:latest → :vX.Y.Z@sha256:...）留给 Slice 4 updater activate。
init_deployment_env() {
    local mode="source"
    if $prod; then
        mode="image"
    fi

    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        return 0
    fi

    mkdir -p "$DEPLOY_DIR"
    local tmp
    tmp="$DEPLOY_DIR/.deployment.env.$$"
    {
        echo "# Sakura AI 部署状态（由 start.sh 初始化；updater 接管后以 atomic write 维护）"
        echo "SAKURA_DEPLOY_MODE=$mode"
        if $prod; then
            # 写实际值：解析当前 SAKURA_AI_IMAGE 环境变量，缺省用默认 latest
            local image="${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}"
            echo "SAKURA_AI_IMAGE=$image"
        fi
    } > "$tmp"

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

updater_backend() {
    local binary="${UPDATER_BINARY:-$UPDATER_STATE_DIR/sakura-ai-updater}"
    if [[ -x "$binary" ]]; then
        "$binary" backend "$@"
    elif [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]]; then
        "${SAKURA_UPDATER_PYTHON:-python3}" -m sakura_ai_updater backend "$@"
    else
        fail "updater executable not installed: $binary"
        return 127
    fi
}

ensure_updater_running() {
    if updater_backend is-running \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH" >/dev/null 2>&1; then
        return 0
    fi
    warn "updater daemon 未运行，正在拉起..."
    if ! updater_backend install \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH"; then
        fail "updater bootstrap 失败（GID 冲突或权限不足，需 root）"
        return 1
    fi
    if ! updater_backend start \
        --state-dir "$UPDATER_STATE_DIR" \
        --socket-path "$UPDATER_SOCKET_PATH"; then
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
        install|start|stop|status|is-running)
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
        0) info "已退出" ; exit 0 ;;
        *) warn "无效选项: $choice" ; exit 1 ;;
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
