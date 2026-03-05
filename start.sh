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

# 检查 .env 文件
if [ ! -f .env ]; then
    echo "⚠️  未找到 .env 文件"
    echo "📝 正在创建 .env 文件..."
    cp .env.example .env
    echo "✅ 已创建 .env 文件"
    echo "⚠️  请编辑 .env 文件，填入你的配置信息"
    echo "📝 编辑命令: nano .env"
    exit 0
fi

echo "✅ 检查环境配置完成"

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
echo "  docker-compose logs -f"
echo ""
echo "✅ 启动完成！"
echo ""
echo "🌐 访问地址:"
echo "  - 健康检查: http://localhost:8000/health"
echo "  - API 文档: http://localhost:8000/docs"
echo ""
echo "📝 下一步:"
echo "  1. 配置 GitHub App (参考 docs/GITHUB_APP_SETUP.md)"
echo "  2. 将 Webhook URL 设置为: https://your-domain.com/api/webhook/github"
echo "  3. 安装 GitHub App 到你的仓库"
echo ""