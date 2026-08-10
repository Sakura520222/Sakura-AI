# Docker 镜像自动发布与一键部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR 合并至 `main` 后自动构建 Docker 镜像（GHCR 主推 + Docker Hub 非阻塞同步，edge/vX.Y.Z/latest 三层 tag），并提供生产 Compose 一键部署（Web + MySQL 8.4 + Redis，Setup Wizard 自动预填数据库连接）。

**Architecture:** 现有 Release 工作流（main push → 建 Release）新增 job 调用可复用 `docker-publish.yml` 构建稳定镜像；新增 `docker-edge.yml` 每次 main push 用不可变 SHA 构建 edge；改造 Dockerfile 为多阶段 + 显式 COPY + Node/Go/Rust 完整工具链；新增 `docker-compose.prod.yml`（内部固定凭据 + 命名卷 + service_healthy 依赖）；Setup Wizard 从环境变量预填数据库/Redis 连接串。

**Tech Stack:** GitHub Actions (workflow_call/uses), docker/build-push-action (buildx, amd64+arm64), GHCR (GITHUB_TOKEN), Docker Hub (crane copy), Dockerfile multi-stage, docker compose, FastAPI/Jinja2 (Setup 预填), MySQL 8.4, Redis 7.

**关键前置知识（工程师必读）:**
- 版本单一来源：`backend/__init__.py` 中 `__version__ = "3.0.0"`（不带 v）。Git tag 与 Docker 稳定标签统一加 v 前缀。
- Release 工作流 `generate-release` job 已输出 `version`（如 `3.0.0`）与 `release_action`（`created` / `updated`）。
- 由工作流内置 GITHUB_TOKEN 产生的事件不会再次触发其他工作流，故不监听 `release.published`。
- Setup Wizard 的数据库步骤强制要求 `DATABASE_URL` 非空且连接测试通过（`canProceed`），`redis_url` 默认 `127.0.0.1`；需预填 compose 内固定连接串。
- 可复用工作流只能 job 级 `uses` 调用，不能放在 steps 中。

---

## 文件结构

| 文件 | 责任 | 操作 |
| --- | --- | --- |
| `.github/workflows/docker-publish.yml` | 可复用发布工作流：构建多架构镜像 → 推 GHCR → crane 同步 Docker Hub | 新建 |
| `.github/workflows/docker-edge.yml` | main push → 调 publish（channel=edge, source_ref=sha） | 新建 |
| `.github/workflows/docker-reconcile.yml` | 定时兜底：最新稳定 Release 缺镜像时补建 | 新建 |
| `.github/workflows/release-on-pr-merge.yml` | 增加 stable 镜像 job；concurrency 改排队 | 修改 |
| `docker/Dockerfile` | 多阶段构建 + 显式 COPY + Node/Go/Rust 完整保留 | 修改 |
| `docker/Dockerfile.dockerignore` | Dockerfile 专用 ignore（构建上下文=仓库根） | 新建 |
| `docker/docker-compose.prod.yml` | 生产一键部署（内部固定凭据） | 新建 |
| `docker/mysql-init/init.sql` | 收缩为仅字符集/排序规则（可选保留旧建表供参考） | 修改 |
| `backend/webui/routes/setup.py` | `setup_page` 传入 `prefill_values` | 修改 |
| `backend/webui/templates/setup_wizard.html` | 表单初始值取模板变量预填 | 修改 |
| `README.md` / `README_EN.md` | Docker 部署章节（双平台命令、固定版本、密码说明） | 修改 |

---

### Task 1: Dockerfile 多阶段改造

**Files:**
- Modify: `docker/Dockerfile`（整文件重写）

- [ ] **Step 1: 重写 Dockerfile 为多阶段构建**

```dockerfile
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
```

- [ ] **Step 2: 校验 Dockerfile 语法**

Run: `docker build --no-cache --target runtime -f docker/Dockerfile -t sakura-ai-test . 2>&1 | tail -20`
（构建上下文为**仓库根目录** `.`；Dockerfile 内 `COPY backend` 等源路径相对于上下文，而非 Dockerfile 所在目录。`docker/Dockerfile.dockerignore` 仍生效——Docker 支持与 Dockerfile 同目录的 `<Dockerfile名称>.dockerignore`。）
Expected: 无 `Error parsing` / `unknown instruction` 类错误；`COPY backend` 等成功。

- [ ] **Step 3: Commit**

```bash
git add docker/Dockerfile
git commit -m "refactor(docker): multi-stage production Dockerfile with explicit COPY"
```

---

### Task 2: 新增 Dockerfile.dockerignore

**Files:**
- Create: `docker/Dockerfile.dockerignore`

- [ ] **Step 1: 创建 `docker/Dockerfile.dockerignore`**

```
# Sakura AI Dockerfile.dockerignore — 构建上下文为仓库根目录
node_modules/
frontend/
.venv/
.git/
.gitattributes
.gitignore
logs/
workplace/
Skills/
data/
tests/
docs/
docker/
*.log
*.md
# 敏感文件：本地连接配置/密钥不得进入构建上下文
config/connection.json
.env
.env.*
*.pem
*.key
deploy.py
deploy_key
.deploy_state.json
.claude/
.codegraph/
.code-review-graph/
.pytest_cache/
.ruff_cache/
```

- [ ] **Step 2: Commit**

```bash
git add docker/Dockerfile.dockerignore
git commit -m "build(docker): add Dockerfile.dockerignore to slim build context"
```

---

### Task 3: 新建 docker-publish.yml（可复用发布工作流）

**Files:**
- Create: `.github/workflows/docker-publish.yml`

- [ ] **Step 1: 创建 reusable workflow**

```yaml
name: Docker 镜像发布（可复用）

on:
  workflow_call:
    inputs:
      source_ref:          # 不可变构建来源：commit SHA 或 refs/tags/vX.Y.Z
        required: true
        type: string
      channel:             # edge / stable
        required: true
        type: string
      version:             # 稳定版版本号（edge 可不传）
        required: false
        type: string
      sync_dockerhub:      # 是否同步 Docker Hub（非阻塞）
        required: false
        type: boolean
        default: true
    secrets:
      DOCKERHUB_USERNAME:
        required: false
      DOCKERHUB_TOKEN:
        required: false

permissions:
  contents: read
  packages: write

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: sakura520222/sakura-ai   # Docker 仓库名必须小写，不能直接用 ${{ github.repository }}

jobs:
  build-and-publish:
    name: 构建并推送 ${{ inputs.channel }}
    runs-on: ubuntu-latest
    outputs:
      digest: ${{ steps.build.outputs.digest }}   # 供 Docker Hub 按 digest 同步
    steps:
      - name: 检出代码（固定到 source_ref）
        uses: actions/checkout@v6
        with:
          ref: ${{ inputs.source_ref }}
          fetch-depth: 0

      - name: 设置 QEMU（模拟编译多架构）
        uses: docker/setup-qemu-action@v4

      - name: 设置 Docker Buildx
        uses: docker/setup-buildx-action@v4

      - name: 登录 GHCR
        uses: docker/login-action@v4
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      # 计算标签：stable → vX.Y.Z + latest；edge → edge
      - name: 计算镜像标签
        id: tags
        run: |
          if [ "${{ inputs.channel }}" = "stable" ]; then
            echo "tags=${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:v${{ inputs.version }},${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:latest" >> "$GITHUB_OUTPUT"
          else
            echo "tags=${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}:edge" >> "$GITHUB_OUTPUT"
          fi

      - name: 构建并推送 GHCR（amd64 + arm64）
        uses: docker/build-push-action@v7
        id: build
        with:
          context: .
          file: docker/Dockerfile
          platforms: linux/amd64,linux/arm64
          push: true
          tags: ${{ steps.tags.outputs.tags }}
          provenance: false
          cache-from: type=gha,scope=sakura-ai-${{ inputs.channel }}
          cache-to: type=gha,mode=max,scope=sakura-ai-${{ inputs.channel }}

  sync-dockerhub:
    name: 同步 Docker Hub（非阻塞）
    if: inputs.sync_dockerhub
    needs: build-and-publish
    runs-on: ubuntu-latest
    continue-on-error: true
    env:
      DOCKERHUB_USERNAME: ${{ secrets.DOCKERHUB_USERNAME }}
      DOCKERHUB_TOKEN: ${{ secrets.DOCKERHUB_TOKEN }}
    steps:
      - name: 检查 Docker Hub 凭据
        id: credentials
        run: |
          if [ -n "$DOCKERHUB_USERNAME" ] && [ -n "$DOCKERHUB_TOKEN" ]; then
            echo "available=true" >> "$GITHUB_OUTPUT"
            echo "Docker Hub 凭据已配置，将同步"
          else
            echo "available=false" >> "$GITHUB_OUTPUT"
            echo "Docker Hub 凭据未配置，跳过同步（不影响 GHCR 主链路）"
          fi

      - name: 安装 crane
        if: steps.credentials.outputs.available == 'true'
        uses: imjasonh/setup-crane@v0.4

      - name: 登录 Docker Hub
        if: steps.credentials.outputs.available == 'true'
        run: |
          echo "$DOCKERHUB_TOKEN" | crane auth login -u "$DOCKERHUB_USERNAME" --password-stdin docker.io

      # 必须按 GHCR digest 同步：edge 为可变 tag，若构建 A 后构建 B 覆盖 edge，
      # 构建 A 再按 :edge 复制会拿到构建 B 的镜像。crane copy 按 digest 引用可保证一致性。
      - name: 按 GHCR digest 复制到 Docker Hub
        if: steps.credentials.outputs.available == 'true'
        run: |
          SOURCE="ghcr.io/${IMAGE_NAME}@${{ needs.build-and-publish.outputs.digest }}"
          if [ "${{ inputs.channel }}" = "stable" ]; then
            crane copy "$SOURCE" "docker.io/${IMAGE_NAME}:v${{ inputs.version }}"
            crane tag "docker.io/${IMAGE_NAME}:v${{ inputs.version }}" latest
          else
            crane copy "$SOURCE" "docker.io/${IMAGE_NAME}:edge"
          fi
```

> 说明：secret 不能直接用于 job 级 `if`，通过 `env` 映射后在 step 中检查凭据；Docker Hub 凭据缺失时 job 正常结束（`continue-on-error`），不阻塞 GHCR 主链路。

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/docker-publish.yml
git commit -m "ci(docker): add reusable docker-publish workflow (GHCR + Docker Hub sync)"
```

---

### Task 4: 新建 docker-edge.yml

**Files:**
- Create: `.github/workflows/docker-edge.yml`

- [ ] **Step 1: 创建 edge 构建工作流**

```yaml
name: Docker edge 镜像

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: docker-edge
  cancel-in-progress: true   # edge 为最新提交预览，旧运行直接取消

permissions:
  contents: read
  packages: write

jobs:
  publish-edge:
    name: 发布 edge 镜像
    uses: ./.github/workflows/docker-publish.yml
    with:
      source_ref: ${{ github.sha }}   # 必须用不可变 SHA，不能传 main
      channel: edge
      sync_dockerhub: true
    secrets: inherit
```
- [ ] **Step 2: Commit**

```bash
git add .github/workflows/docker-edge.yml
git commit -m "ci(docker): build edge image on every main push"
```

---

### Task 5: 修改 release-on-pr-merge.yml（稳定镜像 job + concurrency 排队）

**Files:**
- Modify: `.github/workflows/release-on-pr-merge.yml`

- [ ] **Step 1: 将 concurrency 改为排队执行（第 6-9 行）**

```yaml
concurrency:
  group: release-${{ github.ref }}
  cancel-in-progress: false   # 排队执行，避免后续 main push 取消进行中的稳定镜像发布
```

- [ ] **Step 2: 在 `build-and-upload-assets` job 之后新增稳定镜像 job**

追加到文件末尾（与 `build-and-upload-assets` 同级，`jobs:` 下）：

```yaml
  publish-stable-image:
    name: 发布稳定镜像
    needs: generate-release
    if: needs.generate-release.outputs.release_action == 'created'
    permissions:
      contents: read
      packages: write
    uses: ./.github/workflows/docker-publish.yml
    with:
      source_ref: refs/tags/v${{ needs.generate-release.outputs.version }}
      channel: stable
      version: ${{ needs.generate-release.outputs.version }}
      sync_dockerhub: true
    secrets: inherit
```

- [ ] **Step 3: 校验 YAML 语法**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/release-on-pr-merge.yml',encoding='utf-8')); print('OK')"`
Expected: `OK`（注意 `${{ }}` 在 YAML 中会被 safe_load 当作普通字符串，可解析即视为合法；最终以 GitHub 校验为准）

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/release-on-pr-merge.yml
git commit -m "ci(release): publish stable Docker image on new release creation; queue release runs"
```

---

### Task 6: 新建 docker-reconcile.yml（定时补偿检查）

**Files:**
- Create: `.github/workflows/docker-reconcile.yml`

- [ ] **Step 1: 创建补偿检查工作流**

```yaml
name: Docker 镜像补偿检查

on:
  schedule:
    - cron: "0 6 * * *"   # 每天 06:00 UTC
  workflow_dispatch:

permissions:
  contents: read
  packages: write

env:
  IMAGE_NAME: sakura520222/sakura-ai   # Docker 仓库名必须小写

jobs:
  detect:
    name: 查询最新稳定 Release 与镜像状态
    runs-on: ubuntu-latest
    outputs:
      version: ${{ steps.release.outputs.version }}
      missing: ${{ steps.check.outputs.missing }}
    steps:
      - name: 检出代码（含 tag）
        uses: actions/checkout@v6
        with:
          fetch-depth: 0
          fetch-tags: true

      # 必须查 GitHub 实际最新 Release，不能读 main 的 backend/__init__.py——
      # main 可能已 bump 到 3.1.0 而最新 Release 仍是 v3.0.0（如 Release 工作流失败）。
      - name: 查询最新 Release
        id: release
        run: |
          TAG=$(gh release view --json tagName --jq '.tagName')
          VERSION="${TAG#v}"
          echo "tag=$TAG" >> "$GITHUB_OUTPUT"
          echo "version=$VERSION" >> "$GITHUB_OUTPUT"
          echo "最新 Release: $TAG (version=$VERSION)"
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}

      - name: 验证 tag 真实存在
        run: |
          git rev-parse --verify "refs/tags/${{ steps.release.outputs.tag }}" >/dev/null
          echo "tag 已验证存在"

      - name: 检查 GHCR 镜像是否存在
        id: check
        run: |
          VERSION="${{ steps.release.outputs.version }}"
          TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:${IMAGE_NAME}:pull" | python -c "import sys,json;print(json.load(sys.stdin)['token'])")
          STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
            -H "Authorization: Bearer $TOKEN" \
            "https://ghcr.io/v2/${IMAGE_NAME}/manifests/v${VERSION}")
          echo "v${VERSION} manifest 状态: ${STATUS}"
          # 200 = 镜像已存在；仅 404 视为缺失可补建；
          # 401/403（鉴权）、429（限流）、5xx（GHCR 故障）均不重建，避免误建或掩盖问题
          case "${STATUS}" in
            200) echo "missing=false" >> "$GITHUB_OUTPUT" ;;
            404) echo "missing=true" >> "$GITHUB_OUTPUT" ;;
            401|403) echo "GHCR 鉴权失败（401/403），跳过本次补偿" >&2; exit 1 ;;
            429) echo "GHCR 限流（429），跳过本次补偿" >&2; exit 1 ;;
            5*) echo "GHCR 暂时故障（${STATUS}），跳过本次补偿" >&2; exit 1 ;;
            *) echo "未知状态 ${STATUS}，跳过本次补偿" >&2; exit 1 ;;
          esac

  backfill:
    name: 补建缺失的稳定镜像
    needs: detect
    if: needs.detect.outputs.missing == 'true'
    uses: ./.github/workflows/docker-publish.yml
    with:
      source_ref: refs/tags/v${{ needs.detect.outputs.version }}
      channel: stable
      version: ${{ needs.detect.outputs.version }}
    secrets: inherit
```

> 说明：`detect` 输出 `version` / `missing` 到 job outputs；`backfill` 通过 job 级 `uses` 调用 reusable workflow（可复用工作流不能放在 steps 中），仅当 `missing == 'true'` 时运行。`GH_TOKEN` 环境变量供 `gh` 使用（job 级 `if` 不能直接读 secrets，但 `gh` 运行时通过 env 注入是允许的）。

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/docker-reconcile.yml
git commit -m "ci(docker): add scheduled reconcile to backfill missing stable images"
```

---

### Task 7: 新增生产 compose（docker-compose.prod.yml）

**Files:**
- Create: `docker/docker-compose.prod.yml`

- [ ] **Step 1: 创建生产 compose**

```yaml
# Sakura AI 生产部署 — Web + MySQL 8.4 + Redis 一键启动
# 凭据固定为内部默认值（仅容器网络内可见）。如需自定义：
#   修改 web.environment.DATABASE_URL 与 mysql.environment.MYSQL_PASSWORD / MYSQL_USER / MYSQL_DATABASE（需保持同步）。
# 固定镜像版本：SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0 docker compose -f docker-compose.prod.yml up -d
services:
  web:
    image: ${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}
    container_name: sakura-ai
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: mysql+asyncmy://sakura:sakura-ai@mysql:3306/sakura_ai
      REDIS_URL: redis://redis:6379/0
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes:
      - config_data:/app/config
      - chroma_data:/app/data/chroma
      - logs_data:/app/logs
      - workplace_data:/app/workplace
      - skills_data:/app/Skills
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 40s

  mysql:
    image: mysql:8.4
    container_name: sakura-ai-mysql
    restart: unless-stopped
    environment:
      MYSQL_DATABASE: sakura_ai
      MYSQL_USER: sakura
      MYSQL_PASSWORD: sakura-ai
      MYSQL_RANDOM_ROOT_PASSWORD: "yes"   # 应用只用普通用户，root 密码随机生成并输出到容器日志
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      # 真正执行查询以验证：服务启动、普通用户存在、密码正确、库存在、用户可连接
      # （mysqladmin ping 收到 Access denied 仍返回 0，不验证凭据）
      test:
        [
          "CMD-SHELL",
          'mysql --protocol=TCP -h 127.0.0.1 -u"$${MYSQL_USER}" -p"$${MYSQL_PASSWORD}" -D "$${MYSQL_DATABASE}" -e "SELECT 1" >/dev/null 2>&1',
        ]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s

  redis:
    image: redis:7-alpine
    container_name: sakura-ai-redis
    restart: unless-stopped
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  mysql_data:
  redis_data:
  config_data:
  chroma_data:
  logs_data:
  workplace_data:
  skills_data:
```

- [ ] **Step 2: 校验 compose 语法**

Run: `docker compose -f docker/docker-compose.prod.yml config`
Expected: 输出解析后的配置，无 `Invalid` / `ERROR` 报错（镜像未构建时 `config` 仍可解析，无需实际拉取）。

- [ ] **Step 3: Commit**

```bash
git add docker/docker-compose.prod.yml
git commit -m "feat(docker): add production compose with MySQL 8.4 and Redis"
```

---

### Task 8: 收缩 mysql-init/init.sql

**Files:**
- Modify: `docker/mysql-init/init.sql`

- [ ] **Step 1: 收缩为仅字符集/排序规则**

将文件内容替换为（保留 CREATE DATABASE 与字符集说明，删除全部 CREATE TABLE —— 表结构由应用 `create_all` + `_auto_migrate` 自管）：

```sql
-- Sakura AI MySQL 初始化（供需要自定义字符集/排序规则的场景手动挂载）
-- 生产 compose 不挂载本文件：数据库与用户由 MYSQL_DATABASE / MYSQL_USER / MYSQL_PASSWORD 创建，
-- 表结构由应用启动时 Base.metadata.create_all + _auto_migrate 自动创建/迁移。

CREATE DATABASE IF NOT EXISTS `sakura_ai` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

> 若担心删除既有表定义造成信息丢失，可在同目录保留 `init.sql.full`（412 行旧版，含全部 CREATE TABLE）作为参考，README 说明其用途。

- [ ] **Step 2: Commit**

```bash
git add docker/mysql-init/init.sql
git commit -m "refactor(docker): shrink mysql-init to charset only, schema owned by app"
```

---

### Task 9: Setup Wizard 预填 — setup.py 传入 prefill_values

**Files:**
- Modify: `backend/webui/routes/setup.py`（`setup_page` 函数，约 166-173 行）

- [ ] **Step 1: 在 `setup_page` 中提取环境变量连接串并传入模板**

将 `setup_page` 的 `render_template(...)` 调用改为：

```python
    lang = resolve_language(request)
    current_step = await get_current_step()
    missing = await get_missing_fields()

    # 预填值：compose 部署时环境变量已固定 DATABASE_URL/REDIS_URL，
    # 使 Setup Wizard 数据库步骤免手动输入；纯本地开发（无环境变量）时为空/默认值，行为不变。
    settings = get_settings()
    prefill_values = {
        "database_url": (settings.database_url or "").strip(),
        "redis_url": (settings.redis_url or "").strip(),
    }

    return render_template(
        "setup_wizard.html",
        request,
        user_prefs={"language": lang},
        current_step=current_step,
        missing_fields=missing,
        prefill_values=prefill_values,
        js_i18n=_js_i18n_dict(lang),
    )
```

- [ ] **Step 2: 确认 `get_settings` 已导入**

在文件顶部 import 区（`backend/core/setup_service` 之后）加入：

```python
from backend.core.config import get_settings
```

若已有导入则跳过。确认 `backend/webui/deps.py` 的 `render_template` 会透传任意 kwargs 到模板（已存在，无需改）。

- [ ] **Step 3: 运行现有 setup 相关测试确认无回归**

Run: `pytest -q tests/ -k "setup" 2>&1 | tail -5`（若存在 setup 测试）
Expected: 通过或无相关测试被跳过。

- [ ] **Step 4: Commit**

```bash
git add backend/webui/routes/setup.py
git commit -m "feat(setup): prefill database/redis URLs from environment for compose deploy"
```

---

### Task 10: Setup Wizard 预填 — setup_wizard.html 表单初始值

**Files:**
- Modify: `backend/webui/templates/setup_wizard.html`

- [ ] **Step 1: 修改 Alpine 数据初始值（约 588-590 行）**

将：

```js
        form: {
            database_url: '',
            redis_url: 'redis://127.0.0.1:6379/0',
```

改为：

```js
        form: {
            database_url: {{ prefill_values.database_url | tojson }},
            redis_url: {{ prefill_values.redis_url | tojson }},
```

> `tojson` 已输出合法的 JS 字符串字面量（含引号），**外面不要再套单引号**——否则值会包含多余的双引号。`prefill_values.database_url` 为 `''` 时 `tojson` 渲染为 `''`（空字符串），同样合法。

- [ ] **Step 2: 若 `prefill_values` 未传（防御）**

由于 `setup_page` 是唯一渲染点，`prefill_values` 恒存在；为稳妥可在模板顶部（`<script>` 起始处）加：

```jinja
{% set prefill_values = prefill_values | default({'database_url': '', 'redis_url': ''}, true) %}
```

- [ ] **Step 3: 空 redis_url 回退默认值**

在 `form` 定义之后（`get canProceed` 之前）增加：

```js
            init() {
                if (!this.form.redis_url) this.form.redis_url = 'redis://127.0.0.1:6379/0';
            },
```

> Alpine `init()` 在组件初始化时运行，确保无环境变量时保持原默认行为。

- [ ] **Step 4: 手动冒烟验证**

Run: 启动应用（`uvicorn backend.main:app`，无环境变量），访问 `/setup/verify` 输入 Token 后进入 Wizard
Expected: 数据库连接输入框为空、Redis 默认 `redis://127.0.0.1:6379/0`（行为不变）；以 `DATABASE_URL=mysql+asyncmy://...` 启动时对应输入框自动预填该值。

- [ ] **Step 5: Commit**

```bash
git add backend/webui/templates/setup_wizard.html
git commit -m "feat(setup): prefill wizard form fields from environment values"
```

---

### Task 11: 更新 README / README_EN Docker 部署章节

**Files:**
- Modify: `README.md`（"快速开始"章节）
- Modify: `README_EN.md`（对应章节）

- [ ] **Step 1: 在 README.md 快速开始中新增"方式一：Docker 一键部署（推荐）"**

在现有 `### 2. 克隆项目` 之前插入：

```markdown
### 1. Docker 一键部署（推荐，无需拉取源码）

**方式一：全量部署（MySQL + Redis 一并拉起）**

Linux / macOS：

```bash
mkdir sakura-ai && cd sakura-ai
curl -LO https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml
docker compose -f docker-compose.prod.yml up -d
```

PowerShell（Windows）：

```powershell
New-Item -ItemType Directory -Force sakura-ai | Out-Null
Set-Location sakura-ai
Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml" `
  -OutFile "docker-compose.prod.yml"
docker compose -f docker-compose.prod.yml up -d
```

固定版本部署（无需编辑 compose 文件）：

```bash
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0 \
  docker compose -f docker-compose.prod.yml up -d
```

PowerShell：

```powershell
$env:SAKURA_AI_IMAGE = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
docker compose -f docker-compose.prod.yml up -d
Remove-Item Env:SAKURA_AI_IMAGE
```

首次启动后访问 `http://localhost:8000/setup`：数据库/Redis 连接串已自动预填，点击"测试连接"通过后即可继续 Setup Wizard（其余步骤与源码部署一致）。更多配置见下方"快速开始"。

**方式二：仅运行 Web 镜像（MySQL/Redis 自备）**

Linux / macOS：

```bash
docker run -d -p 8000:8000 \
  -e DATABASE_URL=mysql+asyncmy://user:pass@host:3306/sakura_ai \
  -e REDIS_URL=redis://host:6379/0 \
  -v $(pwd)/config:/app/config \
  ghcr.io/sakura520222/sakura-ai:latest
```

PowerShell（Windows）：

```powershell
$ConfigPath = Join-Path (Get-Location) "config"
New-Item -ItemType Directory -Force $ConfigPath | Out-Null
docker run -d `
  --name sakura-ai `
  --restart unless-stopped `
  -p 8000:8000 `
  -e "DATABASE_URL=mysql+asyncmy://user:pass@host:3306/sakura_ai" `
  -e "REDIS_URL=redis://host:6379/0" `
  -v "${ConfigPath}:/app/config" `
  ghcr.io/sakura520222/sakura-ai:latest
```

> 镜像地址：主镜像 `ghcr.io/sakura520222/sakura-ai`；Docker Hub 替代镜像 `sakura520222/sakura-ai`（内容与 GHCR 完全一致）。Tag 说明：`latest` 最新稳定版，`vX.Y.Z` 固定版本，`edge` 开发预览（不保证稳定）。生产 compose 中 MySQL/Redis 密码固定为 `sakura-ai`（仅容器内可见），如需修改请同步修改 `docker-compose.prod.yml` 中 web 服务的 `DATABASE_URL` 与 mysql 服务的 `MYSQL_PASSWORD` / `MYSQL_USER` / `MYSQL_DATABASE`。
```

> 插入后原"### 1. 环境要求"起的内容顺延编号（将"### 1."改为"### 2."……"### 7."改为"### 8."）。

- [ ] **Step 2: 同步更新 README_EN.md（英文版对应内容，措辞对齐）**

在英文版快速开始对应位置插入同样结构的英文章节，命令与 README.md 一致（Bash + PowerShell）。

- [ ] **Step 3: 校验 Markdown 语法与编号**

Run: 检查 README 快速开始标题编号连续（`### 1.` 至 `### 8.`），无重复编号。
Expected: 连续无重复。

- [ ] **Step 4: Commit**

```bash
git add README.md README_EN.md
git commit -m "docs: add Docker one-click deploy section to READMEs"
```

---

### Task 12: 开发 compose 标注为开发模式

**Files:**
- Modify: `docker/docker-compose.yml`（顶部注释）

- [ ] **Step 1: 在文件顶部加开发模式注释**

```yaml
# Sakura AI 开发模式 Compose（挂载源码、host 网络、仅 Web 服务）
# 生产部署请使用 docker-compose.prod.yml（Web + MySQL + Redis 一键启动，无需源码）
```

- [ ] **Step 2: Commit**

```bash
git add docker/docker-compose.yml
git commit -m "docs(docker): mark compose as dev mode, point to prod compose"
```

---

## 自审记录

**Spec 覆盖核对：**
- Tag 策略（edge/vX.Y.Z/latest、SHA 不可变、stable 仅 created 触发）→ Task 3/4/5/6 ✅
- concurrency 排队（cancel-in-progress: false）→ Task 5 Step 1 ✅
- 多阶段 Dockerfile + 完整 venv + Node/Go/Rust 完整工具链 + gcc/g++/make/pkg-config + 显式 COPY → Task 1 ✅
- Dockerfile.dockerignore（含 docker/、*.md 例外）→ Task 2（README 显式 COPY 未启用，无冲突）✅
- prod compose（固定凭据 sakura-ai、MYSQL_RANDOM_ROOT_PASSWORD、SAKURA_AI_IMAGE 变量、SELECT 1 健康检查、service_healthy、命名卷、不挂 init.sql）→ Task 7 ✅
- init.sql 收缩 → Task 8 ✅
- Wizard 预填（setup.py + setup_wizard.html）→ Task 9/10 ✅
- GHCR 主推 + Docker Hub crane 同步（continue-on-error/if 跳过）→ Task 3 ✅
- reconcile 定时补偿 → Task 6 ✅
- README 双平台命令 + 固定版本 + 密码说明 + Docker Hub 替代地址 → Task 11 ✅
- 开发 compose 标注 → Task 12 ✅

**占位符扫描：** 无 TBD/TODO；所有代码步骤含完整内容。
**类型一致性：** `prefill_values`（dict，含 database_url/redis_url）在 Task 9 定义、Task 10 使用，键名一致；workflow inputs `source_ref/channel/version/sync_dockerhub` 在 Task 3/4/5/6 间一致；`release_action` output 在 Task 5 使用与现有工作流定义一致。
**审阅修正记录（8 项）:** ① reconcile 拆为 detect/backfill 两 job（reusable 不能放 steps）；② `actions/checkout@v6`、`setup-qemu-action@v4`/`setup-buildx-action@v4`/`login-action@v4`/`build-push-action@v7`；③ secret 经 env 映射后 step 内检查（job 级 if 不能读 secret）；④ Docker build 命令用 `-f docker/Dockerfile .`（context=仓库根）；⑤ `tojson` 不套外层引号 + `default(...,true)`；⑥ 镜像名硬编码小写 `sakura520222/sakura-ai`（不用 `${{ github.repository }}`）；⑦ Docker Hub 按 `needs.build-and-publish.outputs.digest` 同步（`crane copy` 按 digest，去掉 `--all-tags=false`）；⑧ reconcile 用 `gh release view` 查真实最新 Release、`git rev-parse` 验 tag、状态码仅 404 视为缺失。另：Task 3 加 QEMU + Buildx gha 缓存；Task 2 ignore 加 `config/connection.json`/`.env`/`*.pem`/`*.key` 敏感文件。
