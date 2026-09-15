# 部署指南

> Sakura AI 完整部署流程：Docker 镜像、源码部署、GitHub App、数据库、Setup Wizard 与 Host Updater 守护进程。

← [文档索引](README.md) · [README](../README.md)

---

## 部署方式概览

| 方式 | 适用场景 | 是否需要源码 | WebUI 手动更新 | 说明 |
|---|---|---|---|---|
| 官方在线服务 | 快速体验 | 否 | 由平台管理 | [https://ai.firefly520.top/](https://ai.firefly520.top/) 注册即用 |
| Linux Docker 全量部署 | 生产自建（推荐） | 否 | 支持 | 拉起 Web + MySQL + Redis，安装 Host Updater，并启用 Agent OS 沙箱 |
| macOS / Windows Compose 部署 | 容器化自建 | 否 | 不支持 | 可检查新版本；不提供 Linux sandboxd，Agent shell/依赖执行会 fail-closed |
| Docker 仅 Web 镜像 | 已有 MySQL/Redis | 否 | 不支持 | 仅启动 Web 容器，连接外部存储 |
| 源码部署 / 开发 | 二次开发、自定义构建 | 是 | 不支持 | 克隆仓库本地运行 |

> 所有配置（GitHub App、AI 模型、数据库等）通过首次启动后的 Setup Wizard 在 Web 界面完成，无需手动编辑配置文件。

---

## 一、Docker 部署

### 1.1 全量部署（MySQL + Redis 一并拉起）

**Linux（包含 Host Updater，推荐）**：

```bash
sudo install -d -o root -g root -m 0755 /opt/sakura-ai/docker
sudo curl --fail --location \
  https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml \
  --output /opt/sakura-ai/docker/docker-compose.prod.yml
sudo curl --fail --location \
  https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/start.sh \
  --output /opt/sakura-ai/start.sh
sudo chmod 0644 /opt/sakura-ai/docker/docker-compose.prod.yml
sudo chmod 0755 /opt/sakura-ai/start.sh
cd /opt/sakura-ai
sudo ./start.sh --prod
```

推荐路径固定为 root 管理的 `/opt/sakura-ai`。启动脚本将 `COMPOSE_PROJECT_NAME=sakura-ai` 持久化到部署状态，并为所有 Compose 操作显式传入项目名。`start.sh --prod` 会先在 root-owned、权限为 `0600` 的 pending 状态副本中准备 `.deploy/deployment.env`；若 Web 初始值是移动的 `:latest`，脚本先从 GitHub 解析一个非 draft/prerelease 的具体稳定 Release，再拉取并固定该 Release 的 Web `:vX.Y.Z@sha256:...`，随后从同一 Release 的 `agent-sandbox-manifest.json` 固定 sandboxd/runner digest。只有完整解析验证以及 Web、sandboxd、runner 和 Compose 所需镜像的 pull/inspect 全部成功后，pending 副本才会原子替换权威 `.deploy/deployment.env`；任一步失败都会丢弃 pending 副本并保持旧状态，且在 Compose 启动前 fail-closed。脚本随后创建独立 GID 9473 和 `/run/sakura-ai-sandbox`，先启动并验证 sandboxd，再启动 Web/MySQL/Redis。随后脚本下载并启动 Host Updater（独立 GID 9472 和 `/run/sakura-ai`）。生产 daemon 启动前会验证 binary、`start.sh`、Compose、`deployment.env` 及其完整父目录链均由 root 控制且不可由 group/other 写入；任一身份、权限、协议、runtime、workspace 或 digest 不匹配都会 fail-closed。新版本检查会自动执行，但安装更新必须由超级管理员在 WebUI 版本管理器中手动确认。

### Agent OS 沙箱（Linux）

Agent 的 `shell`、`grep` 和自动依赖安装不会在 Web 进程中直接执行。Web 通过只读 UDS `/run/sakura-ai-sandbox/sandboxd.sock` 向独立 sandboxd 提交严格版本化请求；sandboxd 为每条请求创建一次性 runner 容器，并固定应用以下策略：

- `--network none`、只读 rootfs、`65532:65532`、`--cap-drop ALL`、`no-new-privileges`；
- CPU、内存、PID、`nofile` 与 combined stdout/stderr 字节上限；
- 创建 runner 前由 root-owned sandboxd 仅将当前任务 worktree handoff 给固定 `65532:65532`；无法完成 ownership 合同时拒绝执行，绝不使用 `0777`；
- 普通 linked worktree 的 task gitdir 以可写受控 mount 暴露，base common `.git` 只读暴露，并通过固定 `GIT_DIR`/`GIT_COMMON_DIR`/`GIT_WORK_TREE` 让 Git 不依赖 Web 容器的 `/app` 指针路径；不挂载相邻任务的可写 metadata；
- 只精确挂载当前任务 worktree 到 `/workspace`，不挂载相邻任务、应用配置、两个 UDS 或 Docker socket；
- 镜像、mount、network、runtime 和 Docker argv 全部由 sandboxd 固定，Backend/模型请求不能覆盖；
- timeout、取消、daemon shutdown 和异常路径都执行有界 `kill/rm`；重启只回收经 service、instance、request、workspace labels 证明属于当前实例的容器。

只有 sandboxd 专用容器挂载 `/var/run/docker.sock`。Web 与 runner 都不挂载该 socket。sandboxd 不是普通 Compose service，由 `start.sh` 独立管理：

```bash
sudo ./start.sh sandboxd status
sudo ./start.sh sandboxd restart
sudo ./start.sh sandboxd reinstall
sudo ./start.sh sandboxd stop
```

生产 workspace 使用宿主 bind 目录（默认 `/opt/sakura-ai/workplace`），Web 内仍显示为 `/app/workplace`。`SAKURA_SANDBOX_WORKSPACE_ROOT` 是宿主路径身份，必须由 `start.sh` 计算和注入，不要手工改成容器路径。sandboxd 的稳定 instance ID、双镜像 digest、release version、固定 egress 网络和 workspace identity 保存在 `.deploy/deployment.env`；不要提交该文件。

Agent 网络访问由 WebUI 超级管理员配置 `agent_team_network_policy` 控制。`offline` 和 `web_tools` 的 Agent/Dependency runner 均使用 `network none`；`web_tools` 仅授权 `search_web`/`fetch_url`，并继续服从既有 Web 开关与 SSRF 防护。`full_access` 才会把两类 runner 映射为 UDS `network_mode=egress`。sandboxd 将该能力映射到部署侧固定的 `SAKURA_SANDBOX_EGRESS_NETWORK`，默认是 Docker 内置 `bridge`，因此全新 Docker 环境无需额外创建网络即可实际出网。若管理员需要独立出口或包仓库 allowlist，可配置一个 named network，例如：

```bash
docker network create sakura-ai-egress
sudo env SAKURA_SANDBOX_EGRESS_NETWORK=sakura-ai-egress ./start.sh --prod
```

网络名只允许 `bridge` 或符合 sandboxd 校验的 named network（字母/数字、`.`、`_`、`-`，最长 63 个字符）；`host`、`container:*`、`ns:*`、路径/选项字符串及空值均拒绝。脚本会将它持久化并纳入 sandboxd 容器 label/identity drift 检查；不存在的 named network 会使 sandboxd 启动失败。请求和模型不能改变任何网络策略或 Docker 参数。旧版本的 `SAKURA_SANDBOX_DEPENDENCY_NETWORK` 仅作 deployment.env 迁移兼容，不应作为新配置入口。

源码开发只有在 `SAKURA_DEPLOY_MODE=source` 且显式选择 `agent_team_execution_backend=local` 时才允许本地执行，此模式**不提供 OS 隔离**。`image`、`production`、`unknown` 或缺失部署模式均拒绝 local。macOS/Windows 的仅容器部署没有 Linux sandboxd，若启用 Agent 执行会明确失败，而不会降级到 Web 宿主进程。

Agent 依赖环境只使用每个任务工作区内固定的 `.venv/local` 和
`.venv/sandbox` 路径。这两个目录是 Agent 内部的临时目录，不承载用户数据；切换执行后端时，
服务会在 runner admission 前删除非活动后端的目录，环境不完整时也会删除并重新创建。
清理仅针对当前 task worktree 下的这两个精确路径，并且不会跟随 symlink/reparse 到外部目标。

> **MySQL 低内存调优：** compose 为 MySQL 8.4 显式设置了 `performance-schema=OFF`、`innodb-buffer-pool-size=64M`、`innodb-redo-log-capacity=32M`、`max-connections=40` 并关闭 X Plugin，空闲内存约 200MB（默认配置约 500MB）。代价是 `sys`/`performance_schema` 监控表不可用；业务数据量增长到数十 MB 以上时可酌情调大缓冲池。应用侧连接池上限为 30（`pool_size=10` + `max_overflow=20`，见 `backend/models/database.py`），40 连接仍有约 9 个余量。既有部署v3.1.1重新下载 `docker-compose.prod.yml` 后重跑 `sudo ./start.sh --prod` 即可生效，`up -d` 只重建 mysql 容器，`mysql_data` 数据卷保留。

**macOS（仅容器，不包含 Host Updater）**：

```bash
mkdir sakura-ai && cd sakura-ai
mkdir -p docker .deploy workplace
curl -L https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml \
  -o docker/docker-compose.prod.yml
umask 077
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest\nCOMPOSE_PROJECT_NAME=sakura-ai\nSAKURA_DB_PASSWORD=%s\nSAKURA_SANDBOX_WORKSPACE_ROOT=%s\n' \
  "$(openssl rand -hex 32)" "$(pwd)/workplace" > .deploy/deployment.env
docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml up -d
```

**PowerShell（Windows）**：

```powershell
New-Item -ItemType Directory -Force sakura-ai | Out-Null
Set-Location sakura-ai
New-Item -ItemType Directory -Force docker, .deploy, workplace | Out-Null
Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml" `
  -OutFile "docker/docker-compose.prod.yml"
$bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
$dbPassword = [Convert]::ToHexString($bytes).ToLowerInvariant()
$workspaceRoot = (Resolve-Path workplace).Path
@("SAKURA_DEPLOY_MODE=image", "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest", "COMPOSE_PROJECT_NAME=sakura-ai", "SAKURA_DB_PASSWORD=$dbPassword", "SAKURA_SANDBOX_WORKSPACE_ROOT=$workspaceRoot") |
  Set-Content -Encoding ascii .deploy/deployment.env
docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml up -d
```

macOS、Windows 和其他仅容器部署可以自动显示新版本，但不能从 WebUI 执行更新，也不具备 Linux Agent OS 沙箱；请手动拉取目标镜像并重新运行 Compose。当前生产 Compose 需要 `SAKURA_SANDBOX_WORKSPACE_ROOT` 等由 `start.sh` 生成的部署身份，因此跨平台手工 Compose 仅适合 Agent 功能关闭的部署。Host Updater 当前仅支持 Linux `amd64`/`arm64` 宿主机；sandboxd 完整生命周期采用相同平台边界，且 Host Updater 要求 glibc ≥ 2.36（Debian 12+/Ubuntu 24.04+）。

首次启动后访问 `http://localhost:8000/setup`：数据库/Redis 连接串已自动预填，点击"测试连接"通过后即可继续 Setup Wizard（其余步骤与源码部署一致）。

### 1.2 仅运行 Web 镜像（MySQL/Redis 自备）

**Linux / macOS**：

```bash
docker run -d -p 8000:8000 \
  -e DATABASE_URL=mysql+asyncmy://user:pass@host:3306/sakura_ai \
  -e REDIS_URL=redis://host:6379/0 \
  -v $(pwd)/config:/app/config \
  ghcr.io/sakura520222/sakura-ai:latest
```

**PowerShell（Windows）**：

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

### 1.3 固定版本部署（无需编辑 compose 文件）

**Linux / macOS**：

```bash
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0 \
  docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml up -d
```

**PowerShell（Windows）**：

```powershell
$env:SAKURA_AI_IMAGE = "ghcr.io/sakura520222/sakura-ai:v3.0.0"
docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml up -d
Remove-Item Env:SAKURA_AI_IMAGE
```

### 1.4 镜像与 Tag 说明

- **权威目录**：WebUI 与 Host Updater 只读取 GHCR `ghcr.io/sakura520222/sakura-ai`；Docker Hub `sakura520222/sakura-ai` 仅是 best-effort 副本，同步失败不会阻塞 GHCR 发布。
- **正式通道**：`latest` 是默认正式版，且必须与唯一对应的 `vX.Y.Z` manifest digest 对齐；正式镜像始终从 Release tag 构建。
- **开发通道**：`edge` 是开发移动别名，同时每次 `develop` push 产生不可变 `dev-<UTC timestamp>-vX.Y.Z-<40 位 SHA>` tag。WebUI 的开发版目标只使用不可变 tag + digest，不会把 `edge` 写入部署状态。
- 跨通道切换可能回到较旧的正式版本，WebUI 会显示风险并要求明确确认；同通道历史镜像仅供查看，不提供任意降级或回滚。
- 生产 compose 不再内置数据库密码：首次部署必须将强随机的 64 位十六进制 `SAKURA_DB_PASSWORD` 保存到权限为 0600 的 `.deploy/deployment.env`，并始终通过 `--env-file .deploy/deployment.env` 启动；文件缺失或变量缺失时 Compose 会 fail-closed。使用仓库中的 `./start.sh --prod` 会自动完成生成、持久化和复用，**切勿提交该运行时文件**。

### 1.5 config 卷与策略配置迁移

生产镜像的 `config_data:/app/config` 卷持久化 Setup 生成的 `connection.json`。审查策略与标签定义已迁移到数据库 `app_config` 节键（`strategy.*` / `label.*`）：运行时按节与内置默认深度合并——管理员改动的叶子保留、升级新增的默认叶子自动出现——并随数据库卷持久化、纳入 WebUI 配置备份。全部非 AI 配置在全局配置页 `/config` 编辑（旧 `/config/general|strategies|labels` 页面自动重定向）；包含已移除历史配置键的旧备份在恢复时会被宽容跳过。

镜像仍保留 packaged YAML 三路合并机制（`/app/config-defaults` 基线 + 卷内隐藏 baseline），但当前管理文件列表为空；旧部署卷内残留的 `strategies.yaml` / `labels.yaml` 不会被读取或修改，可在确认迁移完成后手动清理。

---

## 二、环境要求

- Linux 服务器（使用 Host Updater 自动更新要求 glibc ≥ 2.36，即 Debian 12+/Ubuntu 24.04+；仅容器部署无此限制）
- Docker 和 Docker Compose V2（镜像部署；旧版 `docker-compose` V1 不受支持）/ Python 3.14+（源码部署）
- 公网 IP 和域名
- GitHub 账号
- DeepSeek API Key（或其他 OpenAI 兼容 API）

---

## 三、创建 GitHub App

1. 访问 [GitHub Apps 设置](https://github.com/settings/apps)，点击 **New GitHub App**
2. 填写名称、Homepage URL
3. **Repository permissions**：Pull requests `Read and write`，Contents `Read and write`，Checks `Read and write`，Actions `Read`，Issues `Read and write`（可选）
4. **Webhook URL**：`https://your-domain.com:8000/api/webhook/github`，填写 Webhook secret
5. **Webhook events**：勾选 Pull requests、Pull request reviews、Check runs、Workflow jobs、Issues（可选）、Issue comments（可选）
6. 创建后，在 App 页面底部 **Generate a private key**，下载 `.pem` 文件（Setup Wizard 中需粘贴完整私钥内容）
7. 点击左侧 **Install App**，选择要启用审查的仓库

### OAuth App（WebUI 登录）

WebUI 登录需额外创建 [OAuth App](https://github.com/settings/developers)，回调地址设为 `https://your-domain.com/auth/callback`。

### 仓库互助授权（可选）

启用仓库互助功能时，需在该 GitHub App 设置中启用 **Request user authorization (OAuth on behalf of users)**，并赋予 **Starring** 写权限；仓库互助回调地址为 `https://your-domain.com/star-aid/auth/callback`。

---

## 四、准备数据库（源码部署 / 自备存储）

在宿主机安装并启动 MySQL 和 Redis：

```bash
sudo apt update && sudo apt install mysql-server redis-server -y
sudo systemctl start mysql && sudo systemctl start redis
sudo mysql -e "CREATE DATABASE IF NOT EXISTS \`sakura_ai\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
sudo mysql -e "CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY 'your_password';"
sudo mysql -e "GRANT ALL PRIVILEGES ON *.* TO 'root'@'%';"
sudo mysql -e "FLUSH PRIVILEGES;"
```

> Docker 全量部署无需此步，compose 会自动拉起 MySQL 与 Redis。

---

## 五、源码部署 / 开发模式

```bash
git clone https://github.com/Sakura520222/Sakura-AI.git
cd Sakura-AI
```

**开发 Compose**（挂载源码，只启动 Web 服务）：

```bash
mkdir -p workplace
docker compose -f docker/docker-compose.yml up -d --build
```

开发 Compose 不要求预先导出 `SAKURA_SANDBOX_WORKSPACE_ROOT`：未设置时安全地使用
仓库根目录下的 `workplace/`。它只启动 Web，不会替代独立的 sandboxd。普通非 root
源码启动、显式 `local` 后端或已禁用 Agent 时，`start.sh` 不会无条件创建 root-owned
sandboxd；如果要在源码环境启用 OS 沙箱，请由管理员显式执行
`sudo ./start.sh sandboxd start`，并使用 `sandbox` 后端。sandboxd 无法通过 root/身份/健康
检查时，Agent 请求保持 fail-closed，不会降级到 Web 进程执行。

**本地直接运行**：

> 源码开发平台：Linux x86_64/arm64（glibc ≥ 2.28，非 musl；Alpine 不支持）或 Apple Silicon macOS 14+；其余平台因上游 onnxruntime 未发布对应 Python 3.14 wheel（且无 sdist）无法安装依赖，pip 方式同样受限。

uv 方式（推荐）：

```bash
uv sync                # 自动创建 .venv 并安装全部依赖(含 updater)
uv run python -m backend.main
```

传统 pip 方式（无 uv）：

```bash
pip install -r requirements.txt
pip install -e './updater[dev]'
python -m backend.main
```

> 首次启动将进入 Bootstrap 模式，访问 `http://localhost:8000/setup` 通过 Setup Wizard 完成配置。
>
> 也可通过 `./start.sh`（开发模式）或 `./start.sh --prod`（生产镜像模式）启动。

### 调试 Setup Wizard

只想在本地调试首次部署 / Setup Wizard 流程时，使用独立的 dev 配置文件启动：

```bash
py scripts/dev_bootstrap.py
```

该脚本使用 `.sakura/dev/connection.json`，不会覆盖正式的 `config/connection.json`，并跳过 Telegram、SSE、扫描、配额等后台任务。需要重新从第 0 步调试时：

```bash
py scripts/dev_bootstrap.py --reset
```

---

## 六、Setup Wizard 引导配置

首次启动后访问 `https://your-domain.com/setup`，Setup Wizard 将分步引导完成所有配置（支持断点续配）。

### 安全验证

首次启动时应用会生成一个随机 Token 并打印到启动日志中。访问 `/setup` 前需先在 `/setup/verify` 页面输入此 Token 完成验证：

```
============================================================
Setup Wizard 已启动 — 请使用以下 Token 完成首次部署验证：
  Token: <随机字符串>
请从日志中复制此 Token，在浏览器 /setup/verify 页面输入。
============================================================
```

> Token 在每次应用启动时重新生成，仅当前部署会话有效。`start.sh` 的前台日志跟随会在后台部署进程结束时自动返回；部署成功后，脚本会从经身份校验的 Web 容器“当前启动”日志中提取并直接显示 Setup Token。如果提前按 `Ctrl+C` 退出了日志跟随，可从主菜单的“查看容器当前日志”→“Web”查看。

### 配置步骤

验证通过后进入向导，分步完成：

1. **数据库配置**：填写 MySQL 和 Redis 连接地址，提供在线连接测试
2. **GitHub App 配置**：填写 App ID、私钥和 Webhook Secret，自动验证 App 连接
3. **AI 模型与通知**：配置 AI API（支持自动获取模型列表）和 Telegram Bot Token
4. **管理员与 OAuth**：设置管理员账户、应用域名和 GitHub OAuth 凭证

> Setup Wizard 内置 RAG 嵌入与重排序模型配置（可折叠），可跳过后续在 WebUI 中配置。

---

## 七、验证部署

```bash
curl http://your-domain.com:8000/health
# {"status":"healthy","service":"sakura-ai"}
```

WebUI：`https://your-domain.com/`

---

## 八、查看运行日志

应用使用 Loguru 输出日志：控制台（stdout）记录 `INFO` 及以上级别，落盘文件记录 `DEBUG` 及以上级别。控制台适合实时排障，文件适合详细回溯。

`start.sh` 主菜单提供两个直接入口：

- **查看容器当前日志**：可选 Web、MySQL、Redis 或 Agent sandboxd，只显示该容器本次启动后的日志，默认回放最近 200 行并持续跟随。
- **查看往期运行日志**：按时间从新到旧列出 `app_*.log`，选中后显示文件最后 1000 行。生产镜像部署会以只读方式读取 `logs_data` 命名卷，即使 Web 容器已停止也可回溯。

### 日志文件

- **源码部署**：项目根目录 `logs/app_<启动时间戳>_pid<PID>.log`
- **Docker 部署**：容器内 `/app/logs/`，对应生产 Compose 的 `logs_data` 命名卷
- 每次进程启动新建一个以「时间戳 + PID」命名的文件；单文件达到 **500 MB** 自动轮转；超过 **10 天** 的 `app_*.log` 自动清理
- URL 中的密码与 Telegram Bot Token 在写入前自动脱敏，可安全分享日志

### 实时跟踪控制台（INFO 及以上）

```bash
# 源码部署：直接运行会在当前终端实时输出
python -m backend.main

# Docker：跟踪 web 容器输出（在 /opt/sakura-ai 下）
docker compose --env-file .deploy/deployment.env --project-name sakura-ai \
  -f docker/docker-compose.prod.yml logs -f --tail=200 web
```

PowerShell：

```powershell
Set-Location C:\path\to\sakura-ai
docker compose --env-file .deploy/deployment.env --project-name sakura-ai `
  -f docker/docker-compose.prod.yml logs -f --tail=200 web
```

### 查看落盘 DEBUG 日志

控制台只显示 INFO；需要 DEBUG 细节或历史记录时读取文件。跟踪最新一次启动的文件：

```bash
# 源码部署
tail -f "$(ls -t logs/app_*.log | head -n1)"

# Docker：进入 web 容器查看
docker compose --env-file .deploy/deployment.env --project-name sakura-ai \
  -f docker/docker-compose.prod.yml exec web \
  sh -c 'tail -f "$(ls -t /app/logs/app_*.log | head -n1)"'

# 容器已停止时，直接从命名卷读取
docker run --rm -v sakura-ai_logs_data:/logs alpine \
  sh -c 'tail -f "$(ls -t /logs/app_*.log | head -n1)"'
```

### 过滤错误与提取首次部署 Token

```bash
# 统一设置 compose 调用（在 /opt/sakura-ai 下执行）
COMPOSE="docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml"

# 只看错误与异常
$COMPOSE logs --tail=1000 web | grep -iE "error|exception|critical|traceback"

# 提取 Setup Wizard 首次部署验证 Token（每次启动重新生成）
$COMPOSE logs web | grep -A6 "Setup Wizard"
```

> WebUI 的「审查日志」(`/logs`) 与「操作日志」(`/logs/actions`) 存于数据库，分别记录 PR 审查历史和管理员审计操作，与本节的应用运行日志文件不同。

---

## 九、Host Updater 守护进程

Host updater 是一个独立的 Linux 宿主守护进程。Backend 会定期检查新 Release；超级管理员在 WebUI 版本管理器中确认稳定版更新后，Host Updater 同时读取严格 v1 `update-manifest.json` 和同版本 `agent-sandbox-manifest.json`，预检并拉取 Web、sandboxd、runner 三个不可变镜像，原子更新部署状态，先重建并验证 sandboxd，再激活 Web。任一步失败时会恢复三项旧镜像状态并收敛旧 sidecar/Web；legacy 部署没有旧 sandbox pair 时会先卸载本次 sidecar，只有确认成功后才恢复旧 Web。它不会无人值守安装更新。

**运行环境要求**：自 2026-08-21 起，updater 发布二进制在 Python 3.14 Bookworm（glibc 2.36）环境中构建，宿主机需要 glibc ≥ 2.36（Debian 12+、Ubuntu 24.04+）。更早版本基于 Bullseye（glibc 2.31）构建，可运行于更老的发行版；宿主机仍为 Ubuntu 20.04/22.04 或 Debian 11 等旧系统时，请勿升级到新二进制（自动更新确认前请先确认发行版满足要求），或改为仅容器部署并手动更新镜像。

通过本指南推荐的 `sudo ./start.sh --prod` 首次部署时，updater 会随应用自动完成 binary 安装，并渲染、启用和启动 `sakura-ai-updater.service`；无需再单独执行下面的管理命令。生产 binary 要求 Linux 宿主机的 PID 1 是可用的 systemd；容器、LXC/NAS 或其他不满足该条件的环境会明确失败，不会声称已配置重启自启。

### 管理命令

通过 `start.sh` 脚本管理 updater 守护进程：

```bash
./start.sh updater install|reinstall|uninstall|start|stop|status
```

action 默认为 `status`，即 `./start.sh updater` 等价于 `./start.sh updater status`。

- `reinstall` 是推荐的同步命令：先确认没有后台部署，再通过 updater 内部锁原子关闭新任务提交并确认没有活动任务，然后在维护门禁后停止 systemd service、原子安装对应 Release 的 binary、重新启用并启动 unit，最后输出状态。安装或校验失败时会尝试用保留的安全 binary 恢复原 daemon。这样既避免检查任务与停止 daemon 之间的竞态，也避免后台 `start.sh --prod` 在手工 `stop/install/start` 之间重新拉起 daemon。
- 不支持 `/v1/lifecycle/prepare-stop` 的旧 daemon 无法提供原子任务门禁，`reinstall` 会 fail-closed。升级这类旧版本时，应先在 WebUI 确认没有活动任务，再显式执行 `sudo ./start.sh updater stop`，随后更新到支持 `service-install` 的 release；旧 binary 不认识该子命令时不会删除当前磁盘上的 binary，会明确报错并给出升级指引，不会假装已完成自启动迁移。
- `install` 在 daemon 已运行时保留 restart-required 语义：它会完成 binary 校验并报告“需要重启”，不会先 raw stop 正在处理任务的 daemon。需要切换正在运行的 inode 时使用 `reinstall`，让维护门禁和 systemd stop 保持同一安全顺序。
- `uninstall` 在存在已安装/已加载 unit 或残留 enable link 时，会先执行 `service-uninstall`（systemd stop、backend 兜底 stop、disable、删除 unit 和 daemon-reload），确认 socket 已停止后才删除 binary；同一操作重复执行幂等。若 socket 仍由无法验证身份的监听者占用，会 fail-closed 并要求管理员先检查监听进程，不会盲目 kill。

### systemd 自启动、验证与迁移

生产 binary 的 unit 固定为 `/etc/systemd/system/sakura-ai-updater.service`，由当前 updater binary 生成并使用同一份 `--binary-path`、`--socket-path`、`--state-dir`、Compose 和 `deployment.env` 参数。`service-install` 的顺序是写入 unit、`daemon-reload`、`enable`、`start`；因此宿主机重启后 systemd 会按 PIDFile 恢复 daemon。安装或部署完成后验证：

```bash
sudo systemctl is-enabled sakura-ai-updater.service
sudo systemctl is-active sakura-ai-updater.service
sudo systemctl status sakura-ai-updater.service --no-pager
```

现有手动 daemon 迁移使用维护门禁后的单条命令：

```bash
cd /opt/sakura-ai
sudo ./start.sh updater reinstall
```

如果 systemd 不可用，生产 `install`、`reinstall` 和生产启动都会明确失败；不会回退到手动 daemon 后宣称下次重启能够自启。显式 `SAKURA_UPDATER_DEV=1` 的源码/dev 模式继续使用手动 daemon，重启后需要管理员再次执行 `sudo env SAKURA_UPDATER_DEV=1 ./start.sh updater start`。卸载使用 `sudo ./start.sh updater uninstall`，会在删除 binary 前清理 service；没有 unit 的旧手动安装仍可安全重复卸载。若 unit 残留但 binary 已缺失，脚本会保守失败，需恢复匹配 release 后再清理。

### WebUI 更新后同步 Host Updater

WebUI 版本管理器的稳定版事务会更新 Web、sandboxd 与 Agent runner，但不会替换或重启宿主机上的 Host Updater 二进制。预检中的“Updater 文件可用”只验证目标 Release 包含当前架构对应的 updater binary 与 `SHA256SUMS`，不代表更新任务会安装该 binary。因此，三镜像事务成功后，正在运行的 updater 仍可能是更新前的版本。

等待 WebUI 更新任务成功，并确认应用健康接口已经返回目标版本：

```bash
cd /opt/sakura-ai
curl --fail --silent --show-error http://localhost:8000/health
```

只有人工确认上述响应中的 `version` 等于 WebUI 更新的目标版本后，才能重新安装 updater：

```bash
sudo ./start.sh updater reinstall

sudo curl --fail --silent --show-error \
  --unix-socket /run/sakura-ai/updater.sock \
  http://updater/v1/health
```

`install` 不会下载 `latest` updater，而是根据部署状态解析具体 Sakura AI 版本并下载同一 Release 的架构资产。它本身**不提供应用健康门禁**：当 `deployment.env` 已包含具体的 `SAKURA_AI_IMAGE=:vX.Y.Z` 时，安装器可以直接采用该版本，无需查询 `/health`。因此，“`/health` 成功返回预期目标版本”仍是管理员重新安装 updater binary 前必须手动完成的检查。三镜像应用更新本身则由当前 updater 事务执行 health 与 rollback 门禁。

### 生产首次安装

生产环境的 updater 二进制路径为 `.deploy/updater/sakura-ai-updater`。首次执行 `install` 时，即使宿主机没有 Python 且该 binary 尚不存在，也会由 `start.sh` 完成 binary acquisition，并交给 systemd 渲染、启用和启动 unit；生产路径不依赖宿主机 Python。install、start 和 service lifecycle 操作需要 root 权限，因为需要创建固定 GID 9472 的系统组、`/run/sakura-ai` 运行时目录和 `/etc/systemd/system` 下的 unit：

```bash
sudo ./start.sh updater install  # 获取并校验当前版本 binary，安装并启用 systemd unit
sudo ./start.sh updater start     # 确认/启动 systemd service
```

安装严格绑定当前部署的 Sakura AI 版本，不下载 `latest` updater。版本解析是 **deployment-mode-aware** 的：

- `SAKURA_DEPLOY_MODE=image`：优先使用 `deployment.env` 中 `SAKURA_AI_IMAGE=:vX.Y.Z`（可带 digest）的镜像版本作为权威来源；镜像为 `:latest` 或无具体 tag 时，读取已健康运行服务的 `/health` 版本；服务尚未就绪时才回退到 `backend/__init__.py` 的 `__version__`。镜像部署时实际运行版本始终优先于 host checkout 版本。
- `SAKURA_DEPLOY_MODE=source`：以 `backend/__init__.py` 的 `__version__ = "X.Y.Z"` 为权威来源。
- `deployment.env` 缺失或 `SAKURA_DEPLOY_MODE` 不是 `image`/`source` 时 fail-closed，不猜测版本。

`:latest` 不是具体版本。以上来源都无法确定具体版本时 fail-closed。仅支持 Linux `amd64` 与 `arm64`，其他操作系统或架构会明确失败。

**state directory 与 binary 校验**：state directory 首次创建为 root-owned `0700`。重复执行安装时，只要现有目录 owner 为 root 且 group/other 无写权限就会自动 harden 到 `0700`；owner 非 root、group/other 可写或目录是 symlink 时 fail-closed。binary 和 `SHA256SUMS` 从对应 GitHub Release 通过 HTTPS 获取，并严格校验目标 binary 的 SHA256 条目。binary 为 root-owned `0700`；install lock 防止并发 acquisition；下载临时文件位于同一 state directory，校验、临时文件 fsync 和安全检查通过后以同文件系统 atomic rename 替换最终 binary。

**安装失败保护**（两个阶段）：

- 下载、checksum、chmod、临时文件 fsync 或临时文件安全检查等 pre-commit 失败时，旧 binary 保持 byte-for-byte unchanged。
- atomic rename 之后，如果目录 metadata fsync 或 final safety confirmation 失败，则不得声称旧 binary 未变，必须提示新 inode 可能已经安装，且不会继续调用 backend install。只有 post-commit 检查成功后才完成 backend bootstrap。

Linux 原子替换 binary 后，已经运行的 daemon 会继续使用旧 inode，不会自动切换到新版本。单独 `install` 会保留 restart-required 语义，不会为了替换正在运行的 inode 而 raw stop；重复安装时推荐使用带竞态门禁的一体化命令：

```bash
sudo ./start.sh updater reinstall
```

如果已经在 daemon 运行期间执行了 `install`，请改用 `reinstall` 完成安全迁移。只有显式源码/dev 模式才需要手动 stop/start；未设置该环境变量时，命令按生产 systemd 生命周期运行：

```bash
sudo env SAKURA_UPDATER_DEV=1 ./start.sh updater stop
sudo env SAKURA_UPDATER_DEV=1 ./start.sh updater start
```

### 源码开发模式

开发环境可使用显式 Python override 运行 updater；这不是生产 fallback：

```bash
sudo env SAKURA_UPDATER_DEV=1 SAKURA_UPDATER_PYTHON=/path/to/python ./start.sh updater start
```

### 安全边界

- Web 容器通过只读挂载 `/run/sakura-ai` 目录（Unix Domain Socket）与 updater 通信，**不挂载 `docker.sock`**
- Web 容器通过另一个只读目录 `/run/sakura-ai-sandbox` 与 sandboxd 通信；两个 UDS、GID、状态目录、协议与生命周期完全独立
- 只有 sandboxd 专用容器持有 Docker socket；sandboxd 请求协议不接受任意镜像、宿主路径、network、runtime 或 Docker argv
- 生产部署必须位于 root-owned、group/other 不可写的目录链中；推荐固定使用 `/opt/sakura-ai`。updater 启动时会对 binary、Compose 和 `deployment.env` 逐级 `lstat` 并 fail-closed，拒绝 symlink、非 root owner、共享写权限或非 `0600` 的部署状态
- 生产 updater 由 `/etc/systemd/system/sakura-ai-updater.service` 自启，不依赖 cron；宿主机重启后由 systemd 恢复 daemon。若 PID 1 不是可用 systemd，生产启动会明确失败，不会声称已配置自启。显式 `SAKURA_UPDATER_DEV=1` 的源码/dev 模式保持手动 daemon 生命周期，重启后需要运行 `sudo env SAKURA_UPDATER_DEV=1 ./start.sh updater start`；这只恢复更新服务，不会自动安装应用更新

## 十、卸载

默认卸载会停止仍在后台运行的部署 runner，确认没有活动 updater 更新任务，停止并删除经身份验证的 sandboxd，删除 Compose 容器/网络并卸载 Host Updater，但保留 MySQL、Redis、配置、ChromaDB、日志、工作区和 Skills 等数据，同时保留 `.deploy/deployment.env`，方便以后用原密码和原数据重新部署：

```bash
cd /opt/sakura-ai
sudo ./start.sh uninstall
```

命令会要求输入 `UNINSTALL`。自动化环境必须显式传入 `--yes`，不会因为 stdin 非交互而默认确认。

完全清理必须显式使用 `--purge` 并输入 `UNINSTALL`：

```bash
sudo ./start.sh uninstall --purge
```

`--purge` 会永久删除 Compose 数据卷（包括 MySQL/Redis 数据）、全部 Sakura AI 镜像及部署文件，无法由脚本恢复。在 `/opt/sakura-ai` 等独立安装目录中，清理后只保留 `start.sh`，便于重新部署；源码仓库模式会保护项目源码，只清理部署状态。可审计的非交互清理需要同时提供 `--purge --yes`。

生产部署的镜像拉取在后台 runner 中使用 Docker Compose 原生 TTY 进度渲染器。`Ctrl+C` 只退出 `tail` 查看，拉取与启动继续运行；重新连接后可用 `./start.sh --attach` 继续查看，或用 `./start.sh --status` 查看当前 `pull/start/health` 阶段。若宿主 Compose 版本不支持 `--progress`，脚本会明确警告并回退到普通拉取输出。

### 三镜像 deployment contract（Host Updater 0.3.0）

`/v1/status`、`/v1/health` 和 `sakura-ai-updater --identity` 返回 `updater_version`、`protocol_version`、`build_revision`、`build_release` 和 `capabilities`。正式二进制的构建身份在打包时写入；源码运行明确标记为 `source`。协议 1 只保证通信；image-mode 更新要求以下全部能力，未知附加能力不会阻止兼容：

- `three-image-transaction-v1`
- `deployment-reconcile-v1`
- `deployment-manifest-v1`

Web 后端在 check/preflight/update 前查询正在运行的 daemon；WebUI 显示兼容状态及缺失能力。安装脚本在替换 binary 前检查候选文件的 `--identity`，因此缺少 development Release asset 时，stable fallback 必须同样兼容。不兼容时保留旧 binary 并拒绝危险更新。使用当前脚本执行 `sudo ./start.sh updater reinstall`；若选定 Release 尚未提供兼容 asset，必须先发布/提供兼容 Release，不能通过修改版本号或忽略能力检查绕过。尚未升级的旧 Web 无法得到新 Web 的保护，因此管理员应先同步脚本与 Updater，再进行应用更新。

development 的单一目标来源是 Web OCI image index 中的 `com.sakura-ai.deployment.v1` JSON annotation。schema 1 包含 channel、完整 revision、version、canonical tag 以及可信 GHCR 仓库的 `sandboxd_image`/`runner_image` digest 引用；index 的平台 descriptors 和外部 digest 标识 Web 本身，避免自引用 digest。CI 先按 digest 发布全部镜像，验证每个平台的构建标签，再发布带 manifest 的 canonical index，回读验证并确认源仍为 develop head 后，最后推进 `edge`。失败构建不会进入目录；不带完整 manifest 的旧 development tag 不再是可选择目标。stable 继续使用现有 Release 的 `update-manifest.json` 和 `agent-sandbox-manifest.json`，并验证共同 revision，不改变 Release 文件格式。

check/preflight 比较三个目标 digest、共同 revision、deployment.env 以及实际 sandboxd/Runner 运行配置。缺失或漂移的 sandbox pair 是可修复状态，不会仅因为 Web 已到目标而报告无需更新。旧的 mixed deployment 可以选择当前目标正常更新；完整一致时则返回无需更新。直接重启遇到已 pin 但 revision 不一致的旧 development 状态时，也会改为解析完整 channel head 自愈。首次 development 安装也只消费完整 manifest，旧的无 manifest pin 无法作为新安装目标，应通过兼容 Updater 迁移到已完整发布的目标。

更新先验证三份远程镜像，再全部 pull 并检查本地不可变身份，之后才原子替换 deployment.env。激活后检查 Web、sandboxd 健康和实际三镜像身份，只有全部通过才提交事务并写入 `deployment_verified=true`。任一步失败则整体恢复旧部署；回滚失败明确记录 failed 和 rollback error，保留恢复 journal。进程中断后的 daemon 启动会先恢复旧 env 并重新收敛容器，再清除中断任务门禁；恢复失败保留 journal 并停止服务，避免继续部分更新。轮询中断或仅 Web `/health` 成功都不能替代完整部署成功凭证。

---

*最后更新：2026-9-15 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
