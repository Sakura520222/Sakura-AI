# Sakura AI Reviewer Dockerfile
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 安装系统依赖（libjpeg62-turbo-dev zlib1g-dev 用于 Pillow/qrcode）
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    git \
    curl \
    ca-certificates \
    libmariadb-dev \
    libjpeg62-turbo-dev \
    zlib1g-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install -r requirements.txt

# ── Agent Team 多语言运行时（2026-05 LTS/最新）──

# Node.js 24 LTS — NodeSource 脚本自动检测架构
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs

# Go 1.26 — 动态检测架构（支持 amd64/arm64）
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://go.dev/dl/go1.26.2.linux-${ARCH}.tar.gz" \
       | tar -C /usr/local -xzf -
ENV PATH="/usr/local/go/bin:${PATH}"

# Rust stable — rustup 自动检测架构和平台
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

# 复制应用代码
COPY . .

# 创建运行时目录
RUN mkdir -p logs workplace Skills

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动命令
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]