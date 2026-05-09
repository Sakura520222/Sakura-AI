#!/usr/bin/env bash
# Sakura AI Reviewer 快速启动脚本
set -euo pipefail

REBUILD=false
for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=true ;;
        --help|-h)
            echo "用法: ./start.sh [--rebuild] [--help]"
            echo "  --rebuild  强制重建镜像"
            echo "  --help     显示帮助"
            exit 0
            ;;
        *)
            echo "未知参数: $arg"
            exit 1
            ;;
    esac
done

# 检测 docker compose 命令 (v2 优先)
COMPOSE_FILE="docker/docker-compose.yml"
if docker compose version &>/dev/null; then
    COMPOSE="docker compose -f $COMPOSE_FILE"
elif command -v docker-compose &>/dev/null; then
    COMPOSE="docker-compose -f $COMPOSE_FILE"
else
    echo "❌ Docker Compose 未安装，请先安装 Docker Compose"
    exit 1
fi

echo "🚀 Sakura AI Reviewer 启动脚本"
echo "=========================="

# 检查 Docker
if ! command -v docker &>/dev/null; then
    echo "❌ Docker 未安装，请先安装 Docker"
    exit 1
fi

echo "✅ 环境检查完成"

# 创建必要目录
mkdir -p logs .deploy workplace

# 依赖变更检测
SAVED_HASH_FILE=".deploy/requirements.hash"
CURRENT_HASH=""
if [[ -f "requirements.txt" ]]; then
    CURRENT_HASH=$(md5sum requirements.txt | awk '{print $1}')
fi

NEED_BUILD=false
NEED_PIP_INSTALL=false

if $REBUILD; then
    echo "🔄 强制重建模式"
    NEED_BUILD=true
elif [[ ! -f "$SAVED_HASH_FILE" ]]; then
    echo "📦 首次部署，需要构建镜像"
    NEED_BUILD=true
elif [[ "$CURRENT_HASH" != "$(cat "$SAVED_HASH_FILE")" ]]; then
    echo "📦 检测到依赖变更，将使用临时容器安装新依赖"
    NEED_PIP_INSTALL=true
else
    echo "✅ 依赖未变更，跳过构建"
fi

# 停止现有容器
echo "🛑 停止现有容器..."
$COMPOSE down

# 构建并启动
if $NEED_BUILD; then
    echo "🔨 构建并启动服务..."
    $COMPOSE up -d --build
    echo "$CURRENT_HASH" > "$SAVED_HASH_FILE"
    echo "✅ 镜像构建完成，依赖哈希已更新"
elif $NEED_PIP_INSTALL; then
    echo "📦 在临时容器内安装新依赖..."
    TEMP_CONTAINER="sakura-ai-reviewer-pip-${CURRENT_HASH:0:8}"
    IMAGE_TAG="sakura-ai-reviewer:pip-${CURRENT_HASH:0:8}"
    docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true
    if ! docker run --name "$TEMP_CONTAINER" \
        -v "$(pwd)/requirements.txt:/app/requirements.txt:ro" \
        sakura-ai-reviewer:latest \
        sh -c "pip install -r /app/requirements.txt"; then
        echo "⚠️  临时容器 pip install 失败，自动回退到重建镜像..."
        docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true
        $COMPOSE up -d --build
        echo "$CURRENT_HASH" > "$SAVED_HASH_FILE"
        echo "✅ 镜像重建完成，依赖哈希已更新"
    else
        echo "💾 将依赖写入镜像 $IMAGE_TAG ..."
        docker commit \
            --change 'CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]' \
            "$TEMP_CONTAINER" "$IMAGE_TAG"
        docker tag "$IMAGE_TAG" sakura-ai-reviewer:latest
        docker rm -f "$TEMP_CONTAINER" >/dev/null 2>&1 || true
        echo "$CURRENT_HASH" > "$SAVED_HASH_FILE"
        echo "🔧 启动服务（已更新依赖镜像）..."
        $COMPOSE up -d
        echo "✅ 依赖安装完成，镜像已更新"
    fi
else
    echo "🔧 启动服务（无构建）..."
    $COMPOSE up -d
fi

# 轮询等待服务就绪
echo "⏳ 等待服务启动..."
TIMEOUT=60
ELAPSED=0
while [[ $ELAPSED -lt $TIMEOUT ]]; do
    if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
        echo "✅ 服务已就绪 (${ELAPSED}s)"
        break
    fi
    sleep 2
    ELAPSED=$((ELAPSED + 2))
done

if [[ $ELAPSED -ge $TIMEOUT ]]; then
    echo "⚠️  服务启动超时 (${TIMEOUT}s)，请检查日志: $COMPOSE logs -f"
fi

# 检查服务状态
echo ""
echo "📊 服务状态:"
$COMPOSE ps

echo ""
echo "📋 查看日志命令:"
echo "  $COMPOSE logs -f"
echo ""
echo "✅ 启动完成！"
echo ""
echo "🌐 访问地址:"
echo "  - Setup Wizard: http://localhost:8000/setup"
echo "  - 健康检查: http://localhost:8000/health"
echo "  - API 文档: http://localhost:8000/docs"
echo "  - WebUI: http://localhost:8000/webui/"
echo ""
echo "📝 下一步:"
echo "  1. 首次启动请访问 Setup Wizard 完成配置"
echo "  2. 配置 GitHub App (参考 README)"
echo "  3. 将 Webhook URL 设置为: https://your-domain.com:8000/api/webhook/github"
echo "  4. 安装 GitHub App 到你的仓库"
echo ""
