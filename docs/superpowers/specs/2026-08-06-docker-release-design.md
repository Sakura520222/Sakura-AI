# Docker 镜像自动发布与一键部署设计

日期：2026-08-06
状态：已批准实施
分支：feature/3.0.0-refactor

## 1. 背景与目标

### 1.1 现状

- 仓库 `Sakura520222/Sakura-AI`，Gitflow 工作流，`main` 为生产分支。
- 已有 [release-on-pr-merge.yml](.github/workflows/release-on-pr-merge.yml)：`main` push 后读取 `backend/__init__.py` 的 `__version__`，创建/更新 GitHub Release 与源码包（.tar.gz/.zip）。
- 已有 [docker/Dockerfile](docker/Dockerfile)（开发模式）与 [docker/docker-compose.yml](docker/docker-compose.yml)（host 网络、挂载源码，仅 web 服务，MySQL/Redis 需用户自装）。
- WebUI 为 Jinja2 模板 + CDN 静态资源（[base.html](backend/webui/templates/base.html)），`frontend/`（196M，含 node_modules）是开发工程，生产镜像不需要。
- 配置走 Setup Wizard：首次启动访问 `/setup`，将数据库连接信息写入 `config/connection.json`，其余配置写入数据库 `app_config` 表（[bootstrap.py](backend/core/bootstrap.py)、[setup_service.py](backend/core/setup_service.py)）。
- 数据库表结构由应用自身管理：`Base.metadata.create_all(checkfirst=True)` + `_auto_migrate()`（[database.py](backend/models/database.py)），不存在独立 migration 框架。
- 配置优先级：环境变量 → 数据库 `app_config` → Settings 默认值（[config.py](backend/core/config.py)）。

### 1.2 目标

1. `main` 分支有新 PR 合并后，自动构建并发布 Docker 镜像，用户无需拉取源码。
2. 用户一条命令即可部署完整应用（含 MySQL、Redis 依赖），也可单独运行 web 镜像对接自备数据库。
3. 发布物（Release、源码包、镜像）之间保持一致的版本语义。

### 1.3 非目标

- 不修改 Setup Wizard 使用方式。
- 不修改现有 Release 源码包发布逻辑。
- 不引入独立数据库 migration 框架。

## 2. 镜像 Tag 策略

版本单一来源保持不变：`backend/__init__.py` 中的 `__version__ = "X.Y.Z"`（不带 `v` 前缀）；Git tag 与 Docker 稳定标签统一添加 `v` 前缀，形成 `vX.Y.Z`。

| Tag | 语义 | 触发方式 | 可变性 |
| --- | --- | --- | --- |
| `edge` | 最新 main 提交的开发预览 | 每次 `main` push | 可变，指向该次 commit SHA |
| `vX.Y.Z` | 稳定版本 | Release 工作流成功创建新 Release 后 | 不可变（从 tag 构建，不重新指向） |
| `latest` | 最新稳定版 | 与 `vX.Y.Z` 同一次构建 | 指向与 `vX.Y.Z` 完全相同的 manifest digest |

没有 bump 版本号的 `main` push：只更新 `edge`，不动 `vX.Y.Z` / `latest`。

### 2.1 触发链路（事件驱动为主，轮询仅作补偿）

不依赖 `release.published` 事件 —— 由工作流内置 `GITHUB_TOKEN` 产生的大多数事件不会再次触发其他工作流（GitHub 官方说明），且现有工作流在 PAT 缺失时回退 `GITHUB_TOKEN`，仅监听 Release 事件不可靠。

1. **`docker-edge.yml`**（新增）：`push: branches: [main]`，只推 `edge`，`concurrency.cancel-in-progress: true`。调用时 `source_ref` **必须传 `${{ github.sha }}`**（该次 push 的不可变 commit SHA），不能传 `main`（否则排队/构建期间若有新提交，checkout 可能拿到后续提交，导致镜像与触发事件不一致）：

   ```yaml
   jobs:
     publish-edge:
       uses: ./.github/workflows/docker-publish.yml
       permissions:
         contents: read
         packages: write
       with:
         source_ref: ${{ github.sha }}
         channel: edge
         sync_dockerhub: true
       secrets: inherit
   ```

   `docker-publish.yml` 内 checkout 使用 `ref: ${{ inputs.source_ref }}` 固定到该来源。
2. **`docker-publish.yml`**（新增，reusable workflow）：同时服务于 `edge` 与稳定版本。`workflow_call` inputs：

   ```yaml
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
   ```

3. **稳定镜像构建**：在现有 Release 工作流（`release-on-pr-merge.yml`）中新增 job，**仅在新 Release 创建成功时**调用 `docker-publish.yml`，从具体 tag checkout 并构建 `vX.Y.Z` + `latest`：

   ```yaml
   publish-stable-image:
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
     secrets: inherit
   ```

   （可复用工作流只能 job 级 `uses` 调用；Release 被更新而非创建时不触发镜像发布。）

   **必须同时将 Release 工作流的 concurrency 改为排队执行**（`cancel-in-progress: false`）。否则竞态：第一次 main push 创建新 Release 并开始稳定镜像构建，第二次 main push 到达会取消第一次运行（含未完成的稳定镜像 job），第二次运行又因 Release 已存在走 `updated`、不满足 `created` 条件而不会补发镜像 —— 稳定镜像将缺失，只能等定时 reconcile 兜底。改为排队后 Release 任务按顺序执行，新 Release 创建的运行能完整完成镜像发布。

   ```yaml
   concurrency:
     group: release-${{ github.ref }}
     cancel-in-progress: false   # 排队执行，不取消已开始的稳定发布
   ```

   只有 `docker-edge.yml` 使用 `cancel-in-progress: true`。

4. **补偿检查**（watchdog）：GitHub Actions 定时任务（schedule），检查最新稳定 Release 的 tag 是否缺少 GHCR 镜像，缺少时补建。仅作兜底，不是主路径。

稳定流程内部在 checkout 后执行硬校验，确保 tag 真实存在再开始构建：

```bash
git rev-parse "refs/tags/v${VERSION}^{commit}"
```

tag 不存在时稳定镜像 job 失败，但不影响此前已完成的 Release job。

## 3. 镜像架构（docker/Dockerfile）

### 3.1 多阶段构建

```dockerfile
# Stage 1: builder — 安装 Python 编译依赖并构建虚拟环境
FROM python:3.14-bookworm AS builder
# 创建 /opt/venv，pip install -r requirements.txt

# Stage 2: runtime — 只保留运行所需
FROM python:3.14-slim-bookworm AS runtime
COPY --from=builder /opt/venv /opt/venv
```

Python 部分必须复制**完整虚拟环境 `/opt/venv`**，不能只复制 `site-packages`（否则遗漏 `/usr/local/bin/uvicorn`、辅助文件与部分动态链接库）。

### 3.2 Node / Go / Rust 工具链（运行时必需，必须完整保留）

当前镜像安装三套完整工具链，是为 Agent Team 在运行期间生成、编译、执行代码。最终镜像必须保留完整运行时，不能只复制单个二进制：

- Node.js 24 LTS：需要 `node`、npm/npx、全局模块目录。
- Go 1.26：需要完整 `/usr/local/go`（GOROOT、标准库、工具）。
- Rust stable：需要 Cargo、rustup toolchain、标准库、链接器。

### 3.3 编译依赖与体积目标

Builder 阶段安装 Python 包构建所需的开发头文件与编译依赖（如 `libjpeg-dev`、`zlib1g-dev`、`libmariadb-dev`）；Runtime 阶段删除**仅用于 Python wheel 构建**的 `*-dev` 包和缓存，但保留对应运行库。

如果 Agent Team 需要在运行期间编译 C/C++、Rust 或含 CGO 的 Go 项目，Runtime 仍保留通用编译工具链：`gcc`、`g++`、`make`、`pkg-config` 及必要链接器（Rust 编译可能依赖 `build-essential`）。

- 可以移除：`libjpeg-dev`、`zlib1g-dev`、`libmariadb-dev` 等仅构建时需要的开发包（保留对应运行库）。
- 可能必须保留：`gcc`、`g++`、`make`、`pkg-config`。

因此镜像不会特别小，但功能完整。不预先承诺体积减半，目标为**显著减少 Python 构建层**，最终以 CI 实测为准（记录构建前后体积对比）。

### 3.4 .dockerignore 与显式 COPY（新增）

构建上下文是仓库根目录（`docker build ..`），Docker 只读取构建上下文根目录的 `.dockerignore` 或与 Dockerfile 同目录的 `Dockerfile.dockerignore`。采用 **`docker/Dockerfile.dockerignore`**（Dockerfile 专用，优先级更高，不影响仓库以后可能增加的其他 Dockerfile）。排除开发冗余与敏感文件，控制构建上下文：

```
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
docker/          # compose 等部署文件仅供宿主机使用，不进运行镜像
*.log
*.md
deploy.py
deploy_key
.deploy_state.json
.claude/
.codegraph/
.code-review-graph/
.pytest_cache/
.ruff_cache/
```

生产 Dockerfile 改为**显式 COPY**，不再使用 `COPY . .`（避免无关文件带入镜像）：

```dockerfile
COPY backend ./backend
COPY config ./config
COPY scripts ./scripts
COPY res ./res
COPY requirements.txt .
```

若运行时确实读取 `README.md`（如 WebUI 关于页），则显式 `COPY README.md README_EN.md ./`，而不是保留全部 Markdown 文件。注意此时 `*.md` 已排除这两个文件，需在 ignore 中加例外，否则显式 COPY 会失败：

```dockerignore
*.md
!README.md
!README_EN.md
```

### 3.5 其他

- `HEALTHCHECK` 保留，`CMD`（uvicorn）不变。
- `EXPOSE 8000` 保留。
- 基础镜像版本固定（`python:3.14-bookworm` / `python:3.14-slim-bookworm`）。

## 4. 生产 Compose（docker/docker-compose.prod.yml，新增）

用户一键部署入口。镜像引用（`image:`）而非本地 `build:`，支持 `latest` 与固定 `vX.Y.Z`。

### 4.1 服务（内部固定凭据，无需 .env）

MySQL / Redis 由 compose 内部拉起，端口不暴露到宿主机（仅容器网络内可见），因此凭据**直接固定**在 compose 文件中，用户无需配置任何环境变量：

```yaml
services:
  web:
    image: ${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}
    ports: ["8000:8000"]
    environment:
      DATABASE_URL: mysql+asyncmy://sakura:sakura-ai@mysql:3306/sakura_ai
      REDIS_URL: redis://redis:6379/0
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_healthy
    volumes: [见 4.3]
    restart: unless-stopped

  mysql:
    image: mysql:8.4          # 8.4 LTS（8.0 于 2026-04 结束生命周期）
    environment:
      MYSQL_DATABASE: sakura_ai
      MYSQL_USER: sakura
      MYSQL_PASSWORD: sakura-ai
      MYSQL_RANDOM_ROOT_PASSWORD: "yes"   # 应用只用普通用户，root 密码随机生成并输出到容器日志
    volumes:
      - mysql_data:/var/lib/mysql
    healthcheck:
      # 真正执行查询以验证：MySQL 已启动、普通用户存在、密码正确、
      # sakura_ai 库存在、用户有权连接并执行查询。
      # （不能用 mysqladmin ping —— 它收到 Access denied 时仍返回 0，不验证凭据）
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
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
```

说明：

- 密码固定为 `sakura-ai`（compose 内写死，仅容器网络内可见）。自定义方式仅在 README 中说明（修改 web 服务 `DATABASE_URL` 与 mysql 服务 `MYSQL_PASSWORD`，需保持同步）。
- MySQL root 密码不固定：应用只用普通用户 `sakura`，无需 root 连接，故用 `MYSQL_RANDOM_ROOT_PASSWORD: "yes"` 由官方镜像生成随机 root 密码（输出到容器日志）。
- `web.image` 使用环境变量覆盖，允许用户在不编辑 compose 文件的情况下固定版本：

  ```yaml
  web:
    image: ${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}
  ```

  使用固定版：`SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0 docker compose -f docker-compose.prod.yml up -d`。
- Canonical compose 只写 GHCR。Docker Hub 仅作为 README 中的替代镜像地址，不在 YAML 注释中写"或 docker.io"。
- 连接地址必须用容器名 `mysql` / `redis`，不是 `localhost`。

### 4.2 Setup Wizard 数据库步骤自动预填（关键配套改动）

Wizard 数据库步骤强制要求填写 `DATABASE_URL` 且连接测试通过（[setup_wizard.html:637](backend/webui/templates/setup_wizard.html#L637) `canProceed`），否则流程无法继续；`redis_url` 默认值是 `127.0.0.1`。为了让 compose 固定连接真正达到"一键"，需配套：

1. **[setup.py](backend/webui/routes/setup.py) `setup_page`**：渲染 `setup_wizard.html` 时从 `get_settings()` 提取 `database_url` / `redis_url`（环境变量值），作为 `prefill_values` 传入模板。
2. **[setup_wizard.html](backend/webui/templates/setup_wizard.html)**：`form.database_url` / `form.redis_url` 初始值改用 `{{ prefill_values.database_url }}` / `{{ prefill_values.redis_url }}`（无环境变量时为 `''` / `redis://127.0.0.1:6379/0`，保持现有行为不变）。

效果：compose 部署时用户进入 Wizard 即见预填的连接串，点"测试连接"→"下一步"即可，无需手动输入数据库地址。`_getStepEnvMap` / `_getCompleteEnvMap` 的重新提交逻辑不变（始终以表单实际值为准）。

> 仅当存在环境变量 `DATABASE_URL` / `REDIS_URL` 时才预填；纯本地开发（无环境变量）行为与现状完全一致。

### 4.3 MySQL 版本

默认 `mysql:8.4`（LTS）。8.0 已随 8.0.46 于 2026-04 结束生命周期，新部署不再建议。

### 4.4 持久化卷

```yaml
volumes:
  mysql_data:
  redis_data:
  config_data:      # /app/config — Setup Wizard 写入 connection.json；不持久化则重建容器后回到初始化状态
  chroma_data:      # /app/data/chroma — ChromaDB 向量库
  logs_data:        # /app/logs
  workplace_data:   # /app/workplace — Agent 工作区
  skills_data:      # /app/Skills — Agent Skills
```

### 4.5 init.sql 职责收缩（生产 compose 不挂载）

`docker/mysql-init/init.sql` 从"完整建表"收缩为**仅字符集、排序规则或特殊权限设置**，且**生产 compose 不挂载它**：

- 数据库与用户：交给 `MYSQL_DATABASE` / `MYSQL_USER` / `MYSQL_PASSWORD`。
- 表结构：交给 Sakura AI 自身的 `create_all` + `_auto_migrate`。
- MySQL 8.4 默认字符集已是 utf8mb4，无需 init.sql 干预。
- 单文件下载 compose 后 `./mysql-init` 目录不存在，挂载反而产生空目录误导。

否则 SQL 文件与 SQLAlchemy 模型形成两套 schema 单一来源，后续必然漂移。`init.sql` 收缩后仅供需要自定义字符集/排序规则/特殊权限的用户手动挂载（README 说明）。

### 4.6 开发 compose 保留

现有 [docker-compose.yml](docker/docker-compose.yml) 保留为开发模式，README 标注区别。

## 5. GHCR 与 Docker Hub 双仓库推送

GHCR 为主，Docker Hub 为镜像副本。

1. Buildx 一次构建 `linux/amd64,linux/arm64` 并推送 GHCR。
2. GHCR 成功后取得 manifest digest。
3. 独立 job 使用 `crane copy` 或 `skopeo copy --all` 将该 digest 镜像到 Docker Hub（不重复构建，保证两仓库内容完全一致）。
4. Docker Hub 同步 job 设置 `continue-on-error: true`（凭据缺失/限流不影响 GHCR 主链路）。

### 5.1 凭据

- GHCR：`GITHUB_TOKEN`（`packages: write`）即可。
- Docker Hub：`DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` secrets（组织级或仓库级）。

## 6. 镜像命名

- `ghcr.io/sakura520222/sakura-ai`（GHCR 主镜像）
- `docker.io/sakura520222/sakura-ai`（Docker Hub 副本）

镜像名不包含 `v` 前缀（tag 层体现版本）。

## 7. 错误处理与补偿

| 场景 | 处理 |
| --- | --- |
| 构建失败 | job 失败，Actions 页面可见；`concurrency` 防叠加 |
| Release tag 未生成（未 bump 版本） | 只推 `edge`，v/latest 不推，日志说明 |
| 推 Docker Hub 失败 | `continue-on-error: true`，不阻塞 GHCR |
| 双架构任一失败 | 整个 manifest 不推（不产生半成品） |
| 定时补偿检查 | 发现最新稳定 Release 缺镜像时补建 |

## 8. 用户部署方式（README 更新）

### 方式一：全量一键部署（MySQL + Redis 由 compose 拉起）

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

固定版本部署（不需要编辑 compose 文件）：

Bash：

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

首次访问 `http://localhost:8000/setup` 走 Setup Wizard：数据库/Redis 连接串已自动预填（来自 compose 内固定环境变量），点"测试连接"通过后即可继续；无需手动配置数据库。

### 方式二：只用 web 镜像（数据库已自备）

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

## 9. 实施范围（文件清单）

| 文件 | 操作 |
| --- | --- |
| `.github/workflows/docker-edge.yml` | 新增 — main push 调 `docker-publish.yml` 推 `edge` |
| `.github/workflows/docker-publish.yml` | 新增 — reusable workflow（构建 + 推 GHCR + 镜像 Docker Hub） |
| `.github/workflows/docker-reconcile.yml` | 新增 — 定时补偿检查 |
| `.github/workflows/release-on-pr-merge.yml` | 修改 — Release 创建成功后调用 `docker-publish.yml`（stable），并将 Release concurrency 改为排队执行（`cancel-in-progress: false`），不取消已开始的稳定发布 |
| `docker/Dockerfile` | 改造 — 多阶段构建，Node/Go/Rust 完整保留 |
| `docker/Dockerfile.dockerignore` | 新增 — Dockerfile 专用 ignore（构建上下文为仓库根目录，`docker/.dockerignore` 不生效） |
| `docker/docker-compose.prod.yml` | 新增 — 生产一键部署（内部固定凭据，不挂载 init.sql） |
| `docker/mysql-init/init.sql` | 收缩 — 仅字符集/排序规则，供自定义场景手动挂载 |
| `backend/webui/routes/setup.py` | 修改 — `setup_page` 传入 `prefill_values`（database_url/redis_url） |
| `backend/webui/templates/setup_wizard.html` | 修改 — 表单初始值取模板变量预填 |
| `docker/docker-compose.yml` | 修改 — 标注为开发模式 |
| `README.md` / `README_EN.md` | 更新 — Docker 部署章节（含 Linux/macOS 与 PowerShell 两种命令、`SAKURA_AI_IMAGE` 固定版本、Docker Hub 替代镜像地址、密码自定义说明） |

## 10. 验证方式

1. `docker buildx build --platform linux/amd64,linux/arm64` 本地预检（或 CI 首个 main push 验证）。
2. 镜像启动后 `/health` 返回 `{"status":"healthy"}`。
3. Setup Wizard 完整走通（MySQL/Redis compose 内自动拉起，数据库/Redis 连接串自动预填，点"测试连接"即通过）。
4. Agent Team 冒烟：容器内 `node --version`、`go version`、`cargo --version` 可用。
5. GHCR 与 Docker Hub 双仓库镜像 digest 一致（`skopeo inspect` 对比）。
6. 记录镜像体积构建前后对比。
7. 无环境变量的纯本地开发场景：Setup Wizard 表单仍为空/默认值，行为与现状一致。
