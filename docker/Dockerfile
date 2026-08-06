# Sakura AI Dockerfile — multi-stage production build
# Stage 1: builder — Python 依赖构建（含编译头文件）
FROM python:3.14-bookworm AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Python wheel 构建所需开发包（libjpeg/zlib 用于 Pillow/qrcode，libmariadb 用于 asyncmy）
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

WORKDIR /app
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# Stage 2: runtime — 只保留运行所需
FROM python:3.14-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:/usr/local/go/bin:/root/.cargo/bin:${PATH}"

# 运行期通用编译工具链（Agent Team 编译 C/C++/Rust/CGO Go 项目必需）
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    gcc \
    g++ \
    make \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# ── Agent Team 多语言运行时（2026-05 LTS/最新）──

# Node.js 24 LTS — NodeSource 脚本自动检测架构
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs

# Go 1.26 — 动态检测架构（支持 amd64/arm64）
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://go.dev/dl/go1.26.2.linux-${ARCH}.tar.gz" \
       | tar -C /usr/local -xzf -

# Rust stable — rustup 自动检测架构和平台
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable

# ── 应用代码（显式 COPY，不用 COPY . .）──
WORKDIR /app
COPY backend ./backend
COPY config ./config
COPY scripts ./scripts
COPY res ./res
COPY requirements.txt .

# 运行时目录（Agent 工作区 / Skills 由 compose 卷挂载，此处仅为默认目录）
RUN mkdir -p logs workplace Skills data/chroma

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动命令；即使第三方或未知长连接未主动退出，也限制优雅停机等待时间。
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "15"]
