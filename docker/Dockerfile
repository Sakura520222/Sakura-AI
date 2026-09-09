# Sakura AI Dockerfile — multi-stage production build
ARG SAKURA_BUILD_CHANNEL=source
ARG SAKURA_BUILD_VERSION=unknown
ARG SAKURA_BUILD_REVISION=unknown
ARG SAKURA_BUILD_CREATED=1970-01-01T00:00:00Z

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

ARG SAKURA_BUILD_CHANNEL=source
ARG SAKURA_BUILD_VERSION=unknown
ARG SAKURA_BUILD_REVISION=unknown
ARG SAKURA_BUILD_CREATED=1970-01-01T00:00:00Z

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    SAKURA_BUILD_CHANNEL=${SAKURA_BUILD_CHANNEL} \
    SAKURA_BUILD_VERSION=${SAKURA_BUILD_VERSION} \
    SAKURA_BUILD_REVISION=${SAKURA_BUILD_REVISION} \
    SAKURA_BUILD_CREATED=${SAKURA_BUILD_CREATED}

LABEL org.opencontainers.image.source="https://github.com/Sakura520222/Sakura-AI" \
      org.opencontainers.image.revision="${SAKURA_BUILD_REVISION}" \
      org.opencontainers.image.version="${SAKURA_BUILD_VERSION}" \
      org.opencontainers.image.created="${SAKURA_BUILD_CREATED}" \
      com.sakura-ai.build.channel="${SAKURA_BUILD_CHANNEL}" \
      com.sakura-ai.component="web"

# 运行期仅保留 git（TrustedGitRunner）与 curl（HEALTHCHECK）；Agent 编译
# 工具链已迁移至独立的 sakura-ai-agent-runner 镜像（docker/Dockerfile.agent-sandbox）
RUN apt-get update && apt-get install -y \
    git \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# ── 应用代码（显式 COPY，不用 COPY . .）──
WORKDIR /app
COPY backend ./backend
COPY scripts ./scripts
COPY res ./res
COPY requirements.txt .

# 运行时目录（Agent 工作区 / Skills 由 compose 卷挂载，此处仅为默认目录）
RUN mkdir -p logs workplace Skills data/chroma config \
    && chmod 0755 /app/scripts/docker-entrypoint.sh

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 启动命令；即使第三方或未知长连接未主动退出，也限制优雅停机等待时间。
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "15"]
