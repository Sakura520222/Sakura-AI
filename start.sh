# Sakura AI Reviewer 快速启动脚本

set -e

REBUILD=false
if [[ "$1" == "--rebuild" ]]; then
    REBUILD=true
fi

echo "🚀 Sakura AI Reviewer 启动脚本"
echo "=========================="

# 检查 Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker 未安装，请先安装 Docker"
    exit 1
fi

if ! command -v docker-compose &> /dev/null; then
    echo "❌ Docker Compose 未安装，请先安装 Docker Compose"
    exit 1
fi

echo "✅ 环境检查完成"

# 创建必要目录
mkdir -p logs .deploy

# 依赖变更检测
DEPLOY_HASH_DIR=".deploy"
CURRENT_HASH=""
SAVED_HASH_FILE="$DEPLOY_HASH_DIR/requirements.hash"

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
    echo "📦 检测到依赖变更，将在容器内安装依赖"
    NEED_PIP_INSTALL=true
else
    echo "✅ 依赖未变更，跳过构建"
fi

# 停止现有容器
echo "🛑 停止现有容器..."
cd docker
docker-compose down

# 构建并启动
if $NEED_BUILD; then
    echo "🔨 构建并启动服务..."
    docker-compose up -d --build
    # 保存当前哈希
    cd ..
    echo "$CURRENT_HASH" > "$SAVED_HASH_FILE"
    echo "✅ 依赖哈希已更新"
    cd docker
else
    echo "🔧 启动服务（无构建）..."
    docker-compose up -d
fi

# 依赖变更时在运行中的容器内安装
if $NEED_PIP_INSTALL; then
    echo "📦 在容器内安装新依赖..."
    docker-compose exec -T web pip install -r /app/requirements.txt -q
    cd ..
    echo "$CURRENT_HASH" > "$SAVED_HASH_FILE"
    echo "✅ 依赖安装完成，哈希已更新"
    cd docker
fi

# 等待服务启动
echo "⏳ 等待服务启动..."
sleep 10

# 检查服务状态
echo "📊 服务状态:"
docker-compose ps

# 显示日志
echo ""
echo "📋 查看日志命令:"
echo "  cd docker && docker-compose logs -f"
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
