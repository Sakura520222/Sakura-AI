#!/bin/bash

# Sakura AI Reviewer 快速启动脚本

set -e

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

# 创建日志目录
mkdir -p logs

# 停止现有容器
echo "🛑 停止现有容器..."
cd docker
docker-compose down

# 构建并启动
echo "🔨 构建并启动服务..."
docker-compose up -d --build

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
