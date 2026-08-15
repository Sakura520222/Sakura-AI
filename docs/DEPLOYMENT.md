# 部署指南

> Sakura AI 完整部署流程：Docker 镜像、源码部署、GitHub App、数据库、Setup Wizard 与 Host Updater 守护进程。

← [文档索引](README.md) · [README](../README.md)

---

## 部署方式概览

| 方式 | 适用场景 | 是否需要源码 | WebUI 手动更新 | 说明 |
|---|---|---|---|---|
| 官方在线服务 | 快速体验 | 否 | 由平台管理 | [https://ai.firefly520.top/](https://ai.firefly520.top/) 注册即用 |
| Linux Docker 全量部署 | 生产自建（推荐） | 否 | 支持 | 拉起 Web + MySQL + Redis，并安装 Host Updater |
| macOS / Windows Compose 部署 | 容器化自建 | 否 | 不支持 | 可检查新版本，更新时需手动拉取并重建容器 |
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

推荐路径固定为 root 管理的 `/opt/sakura-ai`。启动脚本将 `COMPOSE_PROJECT_NAME=sakura-ai` 持久化到部署状态，并为所有 Compose 操作显式传入项目名，避免配置文件所在的 `docker/` 目录把网络和持久化卷错误命名为通用的 `docker_*`。`start.sh --prod` 会生成 root-owned `0600` 的 `.deploy/deployment.env`、启动 Web/MySQL/Redis，等待 `/health` 返回实际运行版本，然后从对应 Release 下载 updater binary 与 `SHA256SUMS`，校验后初始化 GID 9472、`/run/sakura-ai` 和 updater daemon。生产 daemon 启动前还会验证 binary、Compose、`deployment.env` 及其完整父目录链均由 root 控制且不可由 group/other 写入；校验失败时拒绝启动更新能力。新版本检查会自动执行，但安装更新必须由超级管理员在 WebUI 版本管理器中手动确认。

**macOS（仅容器，不包含 Host Updater）**：

```bash
mkdir sakura-ai && cd sakura-ai
mkdir -p docker .deploy
curl -L https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml \
  -o docker/docker-compose.prod.yml
umask 077
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest\nCOMPOSE_PROJECT_NAME=sakura-ai\nSAKURA_DB_PASSWORD=%s\n' \
  "$(openssl rand -hex 32)" > .deploy/deployment.env
docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml up -d
```

**PowerShell（Windows）**：

```powershell
New-Item -ItemType Directory -Force sakura-ai | Out-Null
Set-Location sakura-ai
New-Item -ItemType Directory -Force docker, .deploy | Out-Null
Invoke-WebRequest `
  -Uri "https://raw.githubusercontent.com/Sakura520222/Sakura-AI/main/docker/docker-compose.prod.yml" `
  -OutFile "docker/docker-compose.prod.yml"
$bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(32)
$dbPassword = [Convert]::ToHexString($bytes).ToLowerInvariant()
@("SAKURA_DEPLOY_MODE=image", "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest", "COMPOSE_PROJECT_NAME=sakura-ai", "SAKURA_DB_PASSWORD=$dbPassword") |
  Set-Content -Encoding ascii .deploy/deployment.env
docker compose --env-file .deploy/deployment.env --project-name sakura-ai -f docker/docker-compose.prod.yml up -d
```

macOS、Windows 和其他仅容器部署可以自动显示新版本，但不能从 WebUI 执行更新；请手动拉取目标镜像并重新运行 Compose。Host Updater 当前仅支持 Linux `amd64`/`arm64` 宿主机。

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

### 1.5 config 卷三路合并

生产镜像的 `config_data:/app/config` 卷会持久化 Setup 生成的 `connection.json` 以及 WebUI 可编辑的 `strategies.yaml`、`labels.yaml`。镜像内置的新版基线放在独立的 `/app/config-defaults`，容器会在卷内保存上一版 packaged baseline，并在每次启动时对这两个 YAML 做三路深度合并：

- 自上次 baseline 以来未修改的值跟随新默认（包括 scalar/list）
- 管理员改过的值和自定义键始终保留
- 新默认键补入；已从默认删除且未被修改的键删除，被管理员改过的删除键仍保留
- `connection.json` 与其它运行时文件不被触碰
- 合并采用同目录原子替换并在批次失败时回滚；YAML 解析失败或无法安全处理的类型冲突 fail-closed，既有文件不会被覆盖

---

## 二、环境要求

- Linux 服务器（推荐 Ubuntu 20.04+）
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
cd docker
docker-compose up -d
```

**本地直接运行**：

```bash
pip install -r requirements.txt
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

> Token 在每次应用启动时重新生成，仅当前部署会话有效。

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

Host updater 是一个独立的 Linux 宿主守护进程。Backend 会定期检查新 Release；超级管理员在 WebUI 版本管理器中确认更新后，Host Updater 执行预检、拉取镜像、原子更新部署状态、重建容器并校验新版本健康状态。它不会无人值守安装更新。

通过本指南推荐的 `sudo ./start.sh --prod` 首次部署时，updater 会随应用自动完成安装和启动，无需再单独执行下面的管理命令。

### 管理命令

通过 `start.sh` 脚本管理 updater 守护进程：

```bash
./start.sh updater install|reinstall|uninstall|start|stop|status
```

action 默认为 `status`，即 `./start.sh updater` 等价于 `./start.sh updater status`。

- `reinstall` 是推荐的同步命令：先确认没有后台部署和活动更新任务，再停止已验证的 daemon、原子安装对应 Release 的 binary、重新启动并输出状态。这样可避免后台 `start.sh --prod` 在手工 `stop/install/start` 之间重新拉起 daemon 的竞态。
- `uninstall` 只删除已验证 updater 的 binary、daemon metadata、日志、锁和任务状态；若 socket 仍由无法验证身份的监听者占用，会 fail-closed 并要求管理员先检查监听进程，不会盲目 kill。

### WebUI 更新后同步 Host Updater

WebUI 版本管理器当前负责更新 Sakura AI 应用镜像，不会替换或重启宿主机上的 Host Updater。预检中的“Updater 文件可用”只验证目标 Release 包含当前架构对应的 updater binary 与 `SHA256SUMS`，不代表更新任务会安装该 binary。因此，WebUI 更新应用成功后，正在运行的 updater 仍是更新前的版本。

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

`install` 不会下载 `latest` updater，而是根据部署状态解析具体 Sakura AI 版本并下载同一 Release 的架构资产。它本身**不提供应用健康门禁**：当 `deployment.env` 已包含具体的 `SAKURA_AI_IMAGE=:vX.Y.Z` 时，安装器可以直接采用该版本，无需查询 `/health`；即使应用停止、启动失败或报告其他版本，也不会因此自动拒绝安装。因此，“`/health` 成功返回预期目标版本”是管理员必须手动完成的前置检查，而不是安装器保证。命令顺序必须是“先完成应用更新并人工确认健康，再安装 updater”；健康检查失败、不可用或版本不符时请勿继续。这套操作只同步管理员已经在 WebUI 确认过的 Release，不会启用定时任务或无人值守更新。

### 生产首次安装

生产环境的 updater 二进制路径为 `.deploy/updater/sakura-ai-updater`。首次执行 `install` 时，即使宿主机没有 Python 且该 binary 尚不存在，也会由 `start.sh` 完成 binary acquisition；生产路径不依赖宿主机 Python。install 和 start 操作需要 root 权限，因为需要创建固定 GID 9472 的系统组和 `/run/sakura-ai` 运行时目录：

```bash
sudo ./start.sh updater install  # 获取并校验当前版本 binary，创建组和目录
sudo ./start.sh updater start     # 启动守护进程
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

Linux 原子替换 binary 后，已经运行的 daemon 会继续使用旧 inode，不会自动切换到新版本。重复安装时推荐使用带竞态门禁的一体化命令：

```bash
sudo ./start.sh updater reinstall
```

如果已经在 daemon 运行期间执行了 `install`，则至少需要显式重启：

```bash
sudo ./start.sh updater stop
sudo ./start.sh updater start
```

### 源码开发模式

开发环境可使用显式 Python override 运行 updater；这不是生产 fallback：

```bash
SAKURA_UPDATER_DEV=1 SAKURA_UPDATER_PYTHON=/path/to/python ./start.sh updater start
```

### 安全边界

- Web 容器通过只读挂载 `/run/sakura-ai` 目录（Unix Domain Socket）与 updater 通信，**不挂载 `docker.sock`**
- 生产部署必须位于 root-owned、group/other 不可写的目录链中；推荐固定使用 `/opt/sakura-ai`。updater 启动时会对 binary、Compose 和 `deployment.env` 逐级 `lstat` 并 fail-closed，拒绝 symlink、非 root owner、共享写权限或非 `0600` 的部署状态
- updater 不依赖 systemd 或 cron 自启；宿主机重启后，在 `/opt/sakura-ai` 运行 `sudo ./start.sh updater start`，或再次执行 `sudo ./start.sh --prod`。`start` 会重新创建 tmpfs 中消失的 `/run/sakura-ai` 后再拉起 daemon；这只恢复更新服务，不会自动安装应用更新

## 十、卸载

默认卸载会停止仍在后台运行的部署 runner，确认没有活动 updater 更新任务，删除 Compose 容器/网络并卸载 Host Updater，但保留 MySQL、Redis、配置、ChromaDB、日志、工作区和 Skills 等 Docker 数据卷，同时保留 `.deploy/deployment.env`，方便以后用原密码和原数据重新部署：

```bash
cd /opt/sakura-ai
sudo ./start.sh uninstall
```

命令会要求输入 `UNINSTALL`。自动化环境必须显式传入 `--yes`，不会因为 stdin 非交互而默认确认。

完全清理必须显式使用 `--purge` 并输入 `PURGE SAKURA-AI`：

```bash
sudo ./start.sh uninstall --purge
```

`--purge` 会永久删除 Compose 数据卷（包括 MySQL/Redis 数据）以及 `.deploy` 部署状态，无法由脚本恢复。项目源码、脚本和宿主机 bind-mount 目录不会自动递归删除；脚本会输出项目路径，管理员检查后再自行处理。可审计的非交互清理需要同时提供 `--purge --yes`。

生产部署的镜像拉取在后台 runner 中使用 Docker Compose 原生 TTY 进度渲染器。`Ctrl+C` 只退出 `tail` 查看，拉取与启动继续运行；重新连接后可用 `./start.sh --attach` 继续查看，或用 `./start.sh --status` 查看当前 `pull/start/health` 阶段。若宿主 Compose 版本不支持 `--progress`，脚本会明确警告并回退到普通拉取输出。

---

*最后更新：2026-8-15 · 发现错误？[提 Issue](https://github.com/Sakura520222/Sakura-AI/issues)*
