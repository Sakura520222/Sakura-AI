# Sakura AI 自动更新设计

日期：2026-08-07
状态：已批准（修订后，2026-08-07）
分支：feature/3.0.0-refactor
关联设计：[Docker 镜像自动发布与一键部署设计](2026-08-06-docker-release-design.md)

## 1. 目标（Goals）

1. **超级管理员通过 WebUI 一键更新**：右上角常驻当前版本，有新版本时显更新提示，点击进入版本管理器执行更新 / 切换版本。
2. **区分部署模式**：镜像部署（`docker-compose.prod.yml`，拉取 GHCR）与源码部署（`docker-compose.yml`，挂载源码）各自走对应的更新流程。
3. **自动检查**：每次应用启动后非阻塞检查一次，之后每 60 分钟后台检查一次；网络失败保留上次已知良好结果。
4. **Release Notes 展示**：从 GitHub Releases API 拉取更新说明，Markdown 渲染并做 HTML 消毒。
5. **依赖自适应**：源码模式更新时自动安装新版本依赖（停服前完成）；镜像模式依赖打在镜像里。
6. **生命周期独立**：更新执行器独立于 Sakura AI Web 进程——Web 容器在更新中被销毁后，执行器继续完成更新并拉起新版本。

## 2. 非目标（Non-goals）

本设计**只检查、不自动安装**——后台 60 分钟检查仅用于提示，不触发自动更新（参考 Coolify 将 `check_frequency` 与 `auto_update` 分离的成熟做法）。其余按阶段排除：

- **P0 不做**：源码模式自动更新、downgrade、rollback、历史版本切换、beta channel、自动备份、自动更新安装。
- **P1 不做**：beta channel、数据库 schema 回滚、自动备份。
- **永久不做**：数据库 schema 降级（应用迁移由新版本启动时自动执行，单向不可逆，参考 Nextcloud / Open WebUI 的明确警告）。

## 3. 架构总览（Architecture）

四层架构，职责严格分层。Web 层只感知"检测、展示、授权"，不接触任何系统调用；Updater 层独占更新编排；`start.sh` 只提供宿主机原语；部署适配器屏蔽 git/docker 细节。

```
                         GitHub Releases API
                                │
                                ▼
┌───────────────────────────────────────────────────┐
│ Sakura AI WebUI / Backend（容器内）               │
│                                                   │
│ UpdateChecker                                     │
│ ├─ startup check（非阻塞，启动后 5–30s）          │
│ ├─ every 60 min scheduler                         │
│ └─ manual refresh                                 │
│                                                   │
│ VersionService                                    │
│ ├─ current_version / latest_version               │
│ ├─ update_available                               │
│ └─ releases + release_notes（Markdown + sanitize）│
│                                                   │
│ Version Manager（super_admin only）               │
│ └─ 通过 UDS 下发 update / rollback 请求           │
└──────────────────────┬────────────────────────────┘
                       │ Unix Domain Socket（受限 IPC）
                       │ /run/sakura-ai/updater.sock
                       ▼
┌───────────────────────────────────────────────────┐
│ Sakura AI Host Updater（宿主机独立进程）          │
│                                                   │
│ IPC Server（HTTP over UDS，强类型）               │
│ Update Job / 状态机 / 全局更新锁                  │
│ Durable State（atomic write）                     │
│ Release / Manifest / SemVer 校验                  │
│ Adapter Orchestrator                              │
│                                                   │
│ SourceAdapter            ImageAdapter             │
│ ├─ git fetch --tags      ├─ docker pull           │
│ ├─ checkout tag          ├─ update image ref      │
│ ├─ install deps          ├─ compose up -d         │
│ ├─ restart               ├─ health check          │
│ └─ health check          └─ rollback（P1）        │
│                                                   │
│ update-state.json   update.log                    │
└──────────────────────┬────────────────────────────┘
                       │ argv 化子进程
                       ▼
               start.sh internal <primitive>
               （deployment bootstrap / host primitives）
```

## 4. 信任边界与架构决策（Trust Boundary / ADR）

以下为已拍板、原则上不再重新争论的架构决策（Architecture Decision Records）：

**ADR-1　Updater 独占更新编排，`start.sh` 只提供宿主机原语，WebUI 只负责展示与授权。**

```
WebUI        不执行系统命令
Backend      不管理 Docker/Git
Updater      不负责 UI / 用户角色
start.sh     不实现更新状态机
```

> Updater owns update orchestration; `start.sh` owns deployment bootstrap and host primitives; WebUI owns presentation and authorization only.

**Why：** 若 `start.sh` 保留另一套状态机（如 `start.sh update-internal vX.Y.Z` 一口气吃掉整个更新），实际会出现两套 updater——rollback 归 Python 还是 shell、进度归谁、超时归谁都无法回答。Python 层只编排（orchestration），实际 git/docker 动作通过细粒度原语（`start.sh internal image-pull vX.Y.Z`）执行。

**ADR-2　业务容器永远不获得 Docker socket 访问权限。**

容器内 Web 后端不挂载 `/var/run/docker.sock`，只能通过 `/run/sakura-ai/updater.sock` 调用受限 IPC（`check / update / rollback / status`）。Docker 权限只授予宿主机上的 Updater agent。

**Why：** 安全边界差异巨大——挂 docker.sock 等于把宿主机 root 暴露给任何 Web RCE；受限 IPC 只允许强类型、经校验的更新动作。

**ADR-3　Updater 进程独立于 Sakura AI Web 生命周期。**

Updater 作为宿主机独立服务运行（systemd 或自管理 daemon）。更新进入 RESTARTING 阶段时，Web 容器被销毁完全不影响 Updater 继续完成更新与拉起新版本。

**Why：** 避免"WebUI 后端执行 `docker compose down` → 杀掉自己 → 执行更新的进程也死了 → 新容器无人启动"的经典死锁（CasaOS 文档专门警告此场景）。

**ADR-4　systemd 是 preferred backend，不是 deployment requirement。**

不把部署要求从"Linux + Docker"收窄成"Linux + Docker + systemd"。NAS 等无 systemd 环境走自管理 daemon backend，由 `start.sh start` 的 `ensure_updater_running()` 兜底拉起。

**Why：** 兼容性。绝不偷写 `@reboot` cron（cron 同样不是所有环境都有）。非 systemd 模式无法凭空获得宿主机 reboot 后的自启能力——文档须明示此限制。

**ADR-5　镜像部署不得依赖宿主机预装 Python。**

Updater 虽以 Python 编写，但通过 PyInstaller 构建为 Linux amd64/arm64 独立二进制，作为 GitHub Release Asset 发布；`start.sh updater install` 下载、SHA256 校验、安装到 `.deploy/updater/`。

**Why：** 纯 Docker 用户的宿主机可能只有 `bash / curl / docker / docker compose`。强制 `apt install python3` 会把轻量部署搞复杂，违反一键部署初衷。

## 5. 部署模式检测（Deployment Detection）

### 5.1 检测信号

容器内 Web 后端需要知道自己以何种模式部署。检测优先级：

1. **环境变量 `SAKURA_DEPLOY_MODE`**（权威信号，源自 §9.5 的 `.deploy/deployment.env`，由 `start.sh` 经 compose `env_file` 注入容器）：
   - `prod-compose` 模式 → `SAKURA_DEPLOY_MODE=image`
   - `dev-compose` 模式 → `SAKURA_DEPLOY_MODE=source`
2. **运行时启发式**（环境变量缺失时的回退）：检测 `/app/backend` 是否为挂载的源码目录（`.git` 存在 / `pyproject.toml` 可写），或镜像内置标记文件 `/app/.deploy-mode`。

`.deploy/deployment.env` 是权威来源（见 §9.5），两个 compose 文件经 `env_file` 读取后注入容器。

### 5.2 源码模式在 P0 的行为

P0 不实现 SourceAdapter，但**必须识别**源码部署。Version Manager 对源码部署返回：

```json
{
  "deployment_type": "source",
  "update_supported": false,
  "update_unsupported_reason": "source_updater_not_available"
}
```

源码部署在 P0 仍可：显示当前版本、检查新版本、查看 Release Notes、显示更新提示。但更新按钮明确禁用并提示"当前源码部署暂不支持 WebUI 一键更新"。P1 接入 SourceAdapter 后 `update_supported` 变为 `true`，UI 无需改动。

## 6. 更新检查器（Update Checker）

### 6.0 Release 数据职责边界

存在两个"接触 Release 数据"的组件，必须冻结唯一真相源：

- **Backend UpdateChecker**（容器内）：只负责 **discovery / UI 展示**——扫描 Release、缓存、驱动 navbar badge。其数据可能过期或缓存被污染，**不是 destructive operation 的 gate**。
- **Updater PREFLIGHT**（宿主机）：执行 destructive operation 前**必须重新获取并验证**指定 target Release + manifest。**Updater 的验证结果才是 authoritative gate。**

即使 UI 缓存过期或被污染，也无法绕过 host-side validation。

`POST /v1/check`（Updater 端点）是 updater/CLI 使用的 host-side check，**不是 WebUI 60min scheduler 的数据源**——后者由 Backend UpdateChecker 直接调 GitHub API。

### 6.1 触发时机

| 触发 | 行为 |
| --- | --- |
| 应用启动 | 启动后 5–30 秒异步检查（**非阻塞**，不卡启动；GitHub 超时不影响应用启动） |
| 定时 | 每 60 分钟一次，`asyncio` 调度循环 |
| 手动 | 版本管理器"重新检查"按钮，立即触发并刷新 |

### 6.2 数据源

GitHub REST Releases API（公开仓库无需认证，附 `Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-11-28`）。Release 对象字段映射：

| GitHub Release 字段 | 内部字段 |
| --- | --- |
| `tag_name` | `tag`（去 `v` 前缀得 `version`） |
| `name` | `name` |
| `body` | `release_notes`（Markdown，渲染前 sanitize） |
| `published_at` | `published_at` |
| `prerelease` | `prerelease`（P0 过滤掉 prerelease） |
| `html_url` | `url` |
| `assets` | 用于定位 `update-manifest.json` 与 updater 二进制 |

### 6.3 缓存与容错

- 缓存写入 Redis（`update:releases:cache`，**不设短 TTL**，TTL >= 24h 或不过期），保留 `last_known_latest`（上次成功获取的最新版本）。缓存新鲜度由 `fetched_at` / `last_checked` 字段判断，而非 TTL。**调度周期 != 缓存生命周期**——缓存必须能跨多次 GitHub 故障继续存在。
- **网络失败时保留缓存**：检查失败不清空已有数据，仅写入 `check_error` 字段。Version Manager 展示 `last_checked` 与 `check_error`，不让 GitHub 不可达把版本管理器搞坏。
- 缓存结构：

  ```json
  {
    "current_version": "3.0.0",
    "latest_version": "3.1.0",
    "update_available": true,
    "last_checked": "2026-08-07T10:30:00Z",
    "check_error": null,
    "releases": [ /* ReleaseInfo[]，截断保留最近 N 条 */ ]
  }
  ```

### 6.4 版本比较

严格 SemVer 比较（使用 `semver` 库或 strict regex `^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$`，**不使用 `packaging.version.Version`**——后者实现 PEP 440 而非 SemVer，会接受非 SemVer 字符串）。接受 `X.Y.Z` 与 `X.Y.Z-prerelease`，P0 过滤掉 prerelease。`current_version` 来自容器内 [backend/__init__.py](backend/__init__.py) 的 `__version__`；`latest_version` 取最新非 prerelease Release。

## 7. IPC 协议（IPC Protocol）

### 7.1 传输与路径

HTTP over Unix Domain Socket（FastAPI/Starlette + `uvicorn`，`uds=` 参数）。路径分持久与易失两类：

```
持久（跨 reboot 保留）：
.deploy/updater/
├── sakura-ai-updater          # 二进制
├── updater.pid                # daemon 模式 PID
├── updater.log                # 日志
├── update-state.json          # 持久化状态
└── updater.lock               # OS-level flock

易失 runtime（/run reboot 后消失，由 daemon 启动重建）：
/run/sakura-ai/
└── updater.sock
```

- **socket 权限**：`updater.sock` 的 `owner=root, group=sakura-ai, mode=0660`。
- **host group 创建**：`start.sh updater install` 在宿主机创建 `sakura-ai` 组（**固定数字 GID，如 `9472`**，避免跨机器漂移），updater daemon 以该组启动以获得 Docker 访问权（加入 `docker` 组或以 root 跑）。
- **容器 GID 映射**：两个 compose 文件的 `web` 服务均新增 `group_add: ["9472"]`（**数字 GID，不依赖容器内组名解析**），并挂载 `- /run/sakura-ai:/run/sakura-ai`。容器进程凭 GID 匹配 socket 的 `0660` group 位，避免真实机器上 Permission denied。
- **不暴露**：`/var/run/docker.sock`。

### 7.2 协议版本协商

所有响应使用统一 body envelope，携带协议与 updater 版本：

```json
{ "protocol_version": 1, "updater_version": "1.0.0", "data": { } }
```

（HTTP header 不能直接是 JSON 对象；版本信息放 body envelope 比 `X-Sakura-Updater-Protocol` 头更易解析。）

Web 后端在首次连接时校验 `protocol_version` 兼容性，不兼容则禁用更新按钮并提示原因。此机制 P0 设计、P2 完善（min_updater_version 约束）。

### 7.3 端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/v1/status` | 当前 updater 状态 + 部署信息 + 是否有进行中的 job |
| POST | `/v1/check` | 触发一次 Release 检查（不影响 60min 调度） |
| POST | `/v1/preflight` | 对指定版本执行 dry-run 预检，不执行实际变更 |
| POST | `/v1/update` | 提交更新任务，立即返回 `job_id` |
| POST | `/v1/rollback` | 提交回滚任务（P1） |
| GET | `/v1/jobs/{job_id}` | 查询 job 状态与进度 |
| GET | `/v1/jobs/{job_id}/logs` | 查询 job 结构化日志 |

请求体强类型（Pydantic），`action` 为 `Literal["check", "update", "rollback", "status"]`，禁止 `{ "command": "..." }` 形式的万能执行器。

### 7.4 Job 模型

`POST /v1/update` / `/v1/rollback` 立即返回 `job_id`（`upd_019...`），Web 后端轮询 `GET /v1/jobs/{job_id}`。HTTP 请求**绝不挂着等更新完成**——RESTARTING 阶段 Web 后端会消失，长连接必然断开。

### 7.5 全局更新锁（P0 correctness）

两层锁，确保跨进程、跨崩溃的互斥：

**进程唯一性（OS-level）**：daemon 启动时 `flock(.deploy/updater/updater.lock)`（`LOCK_EX | LOCK_NB`），获取失败则退出——防止 DaemonBackend 因 race 被拉起两份、两个 Python 进程各有自己的 `asyncio.Lock`。

**任务唯一性**：进程内 `asyncio.Lock` + 持久化 state 中的 `active_job_id`。任何时刻最多 1 个 destructive update job，第二个请求返回 `409 Conflict`：

```json
{ "error": "update_in_progress", "job_id": "upd_xxx" }
```

覆盖所有入口：浏览器 A、浏览器 B、`start.sh update apply` 同时触发均被串行化。

### 7.6 崩溃恢复（Interrupted job reconcile）

daemon 启动时读取持久化 state（fail-closed，见 §8.4），按以下 invariant 逐一 reconcile：

```
active_job_id == null AND (current_job == null OR current_job.state terminal)
  → 无 active job，OK（current_job 可能是历史 terminal job，保留）

active_job_id == null AND current_job 非 null AND current_job.state 非 terminal
  → state corruption，fail-closed（无 gate 却声称执行中，不可能状态）

active_job_id != null AND current_job == null
  → state corruption，fail-closed（抛异常，拒绝启动）

active_job_id != null AND active_job_id != current_job.job_id
  → state corruption，fail-closed（抛异常，拒绝启动）

active_job_id != null AND current_job.state 非 terminal
  → 上次更新执行中被中断（进程崩溃 / 宿主机 reboot / kill）。
    P0 最安全的行为不是盲目续跑（事务边界可能已破坏，如镜像 pull 到一半、
    compose up 未完成），而是：
      current_job.state = "failed"
      current_job.error_code = "interrupted"
      current_job.error = "updater process restarted mid-update"
      清理 active_job_id
      changed=True（需持久化）

active_job_id != null AND current_job.state terminal
  → stale gate：上次 job 已写终态（SUCCESS/FAILED）但崩溃发生在清理
    active_job_id 之前。保留 job 终态记录，清理 active_job_id，changed=True。
```

`INTERRUPTED` 不是顶层 state，而是 `state="failed"` + `error_code="interrupted"`（FAILED 子态）。状态机只处理正式 P0 state，失败原因由 `error_code` 单独诊断。

真正自动断点续执行留待 P1+。验收标准"daemon 意外退出后自动拉起"只解决**进程恢复**，**更新事务恢复**由 reconcile + 管理员介入处理。

## 8. 状态机（State Machine）

### 8.1 P0 状态

```
IDLE
CHECKING
UPDATE_AVAILABLE
PREFLIGHT
DOWNLOADING
ACTIVATING
RESTARTING
HEALTH_CHECKING
SUCCESS
FAILED
```

### 8.2 P1 追加

```
BACKING_UP
ROLLING_BACK
ROLLED_BACK
```

### 8.3 不设 MIGRATING 状态

应用没有 updater-controlled migration step——schema 迁移由新版本容器启动时 `migrate_schema_async()` 自动执行（[database.py](backend/models/database.py)）。Updater 既不控制也无法感知迁移的起止，凭空声称 `MIGRATING` 是不真实的。

实际流程：

```
ACTIVATING → RESTARTING → [新容器启动：create_all + _auto_migrate] → HEALTH_CHECKING
```

若未来应用在 `/health/ready` 暴露 `{ "phase": "database_migration" }`，Version Manager 可在 `HEALTH_CHECKING` 阶段显示"正在执行数据库迁移…"，但 Updater 状态机本身不增加 `MIGRATING`。

### 8.4 状态持久化

`update-state.json`，atomic write（write temp → fsync → atomic rename），防断电半截 JSON。采用 **wrapper 结构**：顶层持 `active_job_id`（destructive task gate，见 §7.5）+ `current_job`（当前/最近 job 的完整状态）：

```json
{
  "schema_version": 1,
  "active_job_id": "upd_019...",
  "current_job": {
    "job_id": "upd_019...",
    "operation": "update",
    "deployment": "image",
    "from_version": "3.0.0",
    "from_image": "ghcr.io/sakura520222/sakura-ai:v3.0.0",
    "from_digest": "sha256:...",
    "target_version": "3.1.0",
    "target_image": "ghcr.io/sakura520222/sakura-ai:v3.1.0",
    "state": "downloading",
    "step": "docker_pull",
    "started_at": "...",
    "updated_at": "...",
    "retry_count": 0,
    "rollback_allowed": false,
    "error_code": null,
    "error": null
  }
}
```

`error_code` 是结构化失败原因（与 `state` 正交）：`state="failed"` + `error_code="interrupted"` 表示崩溃中断（§7.6），`error_code="health_check"` 表示健康检查失败。状态机只处理正式 P0 state（IDLE/.../SUCCESS/FAILED），失败原因由 `error_code` 单独诊断，**不新增 `INTERRUPTED` 顶层 state**。

**读取 fail-closed**（关键安全语义）：daemon 启动读取 state 时——

- 文件不存在 → 返回初始空 store（`active_job_id=null`），正常。
- JSON 损坏 / `schema_version` 不支持 / permission denied → **抛异常，daemon 拒绝启动并拒绝提供 destructive 能力**。

绝不把损坏 state 当空 store（fail-open 会让 daemon 误以为 idle 而 Slice 4 允许新的 destructive update）；绝不把未来 `schema_version=2` 当 v1 静默读完再保存（会抹掉未识别字段）。`active_job_id` 与 `current_job.job_id` 不一致属 state corruption，reconcile 阶段 fail-closed（见 §7.6）。

P0 即便不实现回滚，也必须记录 `from_image` / `from_digest`——用于故障诊断（"更新失败，原版本 3.0.0 / 原镜像 sha256:... / 失败阶段 HEALTH_CHECKING"），并为 P1 RollbackManager 铺路。

## 9. 镜像适配器（Image Adapter）— P0

部署目标由 [docker-compose.prod.yml](docker/docker-compose.prod.yml) 的 `SAKURA_AI_IMAGE` 环境变量控制（默认 `ghcr.io/sakura520222/sakura-ai:latest`）。

### 9.1 接口

```python
class ImageDeploymentAdapter:
    async def preflight(self, target_version: str) -> PreflightResult
    async def prepare(self, target_version: str) -> None       # docker pull
    async def activate(self, target_version: str) -> None      # 更新 image ref + compose up -d
    async def restart(self) -> None                            # 已包含在 activate
    async def health_check(self) -> HealthResult
```

### 9.2 失败 SLA（关键原则）

更新失败的恢复保证按阶段划分：

- **PREFLIGHT / DOWNLOADING 失败**：当前运行版本**完全不受影响**（prepare 阶段不触碰运行容器——抄 Coolify 原则，`docker pull` 失败绝不碰正在运行的容器）。
- **ACTIVATING 之后失败**（如 compose up 失败、health check 超时）：记录 `FAILED` + `from_version` / `from_image` / `from_digest` / 失败阶段。**P0 不承诺自动恢复服务**——旧容器已被替换，若新容器起不来则服务不可用，管理员依据诊断信息手动恢复（P1 RollbackManager 自动化）。

### 9.3 流程

```
PREFLIGHT
  ├─ 解析目标 Release v3.1.0
  ├─ 校验 ghcr.io/.../sakura-ai:v3.1.0 manifest 存在
  ├─ manifest.v1 校验通过（见 §12）
  ├─ 磁盘空间充足
  └─ 当前无其他 update job（全局锁）

DOWNLOADING
  └─ docker pull ghcr.io/sakura520222/sakura-ai:v3.1.0

ACTIVATING
  ├─ 记录 from_image / from_digest
  ├─ 写 SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0
  └─ docker compose -f docker-compose.prod.yml up -d（force-recreate web）

RESTARTING
  └─ 等待旧容器退出、新容器就绪（新容器启动时自动 migrate）

HEALTH_CHECKING
  ├─ 轮询 GET /health（复用 start.sh 的 HEALTH_TIMEOUT 机制）
  └─ **版本验证 gate**（仅 /health 200 不足证明更新成功）：
     readiness == healthy
     AND reported_app_version == target_version
     ——/health 返回的 version 来自新容器 __version__，必须 == target；
     否则标记 FAILED（如 compose 拉起旧镜像 / target 镜像 __version__ 与 tag 不一致）

SUCCESS / FAILED
```

### 9.4 所有外部命令 argv 化

```python
await asyncio.create_subprocess_exec(
    "docker", "pull", f"ghcr.io/sakura520222/sakura-ai:v{target_version}",
)
```

**禁止** `create_subprocess_shell(f"docker pull ...:{version}")`——`version` 来自 WebUI，必须杜绝 shell 注入。

### 9.5 镜像引用持久化（authoritative deployment state）

`SAKURA_AI_IMAGE` 不能只作为某次 `docker compose` 进程的环境变量——否则机器 reboot 或下一次 `start.sh start` 会落回默认 `:latest`，破坏"指定版本安装"与回滚锚点。

冻结一个 authoritative deployment state 文件：

```
.deploy/deployment.env

SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0
SAKURA_DEPLOY_MODE=image
```

- 所有 `start.sh` / updater / compose 均读取此文件（compose 经 `env_file`，start.sh 经 `source`），单一来源。
- 写入必须 atomic（write temp → fsync → rename）。
- **首次接管**一个当前使用 `:latest` 的部署时，必须查询并保存**实际 digest**（`docker inspect --format='{{.Image}}'`），将 `SAKURA_AI_IMAGE` 具体化为 `ghcr.io/.../sakura-ai:vX.Y.Z@sha256:...`，**不把 mutable `latest` 当可靠回滚锚点**。

## 10. 源码适配器（Source Adapter）— P1

### 10.1 接口

```python
class SourceDeploymentAdapter:
    async def preflight(self, target_version: str) -> PreflightResult
    async def prepare(self, target_version: str) -> None       # git fetch + 装依赖（停服前）
    async def activate(self, target_version: str) -> None      # checkout tag / 切换 current
    async def restart(self) -> None
    async def health_check(self) -> HealthResult
    async def rollback(self, target_version: str) -> None
```

### 10.2 流程（worktree staging 默认）

**P1 默认采用 git worktree / releases 目录 staging，不原地 detached checkout。** 原因：源码 compose 挂载的是当前源码目录（[docker-compose.yml](docker/docker-compose.yml) 的 `../backend:/app/backend`），in-place checkout 等于直接改 live deployment；而且目标版本代码在 checkout 前不在工作树，无法满足 §10.4 的"依赖停服前完成"。

```
PREFLIGHT
  ├─ 工作区是否干净（本地修改检测）
  ├─ git remote 可达
  └─ 磁盘空间（容纳新 release worktree）

PREPARING（当前版本正常运行，不动 live deployment）
  ├─ git fetch --tags origin
  ├─ 确认 vX.Y.Z tag 存在
  ├─ git worktree add releases/3.1.0 v3.1.0
  └─ 针对 releases/3.1.0 安装/验证依赖（pip install / uv sync）← 关键：停服前完成

ACTIVATING
  ├─ 切换 current 软链 -> releases/3.1.0
  ├─ 更新 .deploy/deployment.env（source root 指向新 release）
  └─ docker compose -f docker-compose.yml up -d（force-recreate web）

RESTARTING → HEALTH_CHECKING（含版本验证 gate）→ SUCCESS / FAILED
```

### 10.3 版本切换策略

目录布局（staging）：

```
/opt/sakura-ai/（或部署根目录）
├── releases/
│   ├── 3.0.0/              # git worktree
│   ├── 3.1.0/
│   └── 3.1.1/
├── current -> releases/3.1.1
└── data/                    # 持久化数据卷
```

- 不裸 `git pull origin main`（抄 Uptime Kuma：`git fetch --tags` + 明确 tag）。
- 切换版本 = 改 `current` 软链 + recreate，不 reset 工作目录。
- 旧 release worktree 保留 N 个（回滚锚点 + 节省再次切换的 fetch/build），超出滚动清理。
- in-place detached checkout 仅作为退化路径（无 worktree 支持时），**不作 P1 默认**。

### 10.4 依赖在停服前安装

**依赖必须在新版本启动前尽可能安装完成。** 否则"停掉服务 → pip 下载失败 → 服务彻底挂着"。当前 dev-compose 挂载了 `requirements.txt` 并在容器内安装，SourceAdapter 复用 [start.sh](start.sh) 现有的 pip-install-in-temp-container 逻辑（通过 `start.sh internal source-prepare` 原语）。

## 11. 服务后端（Service Backend）

### 11.1 抽象

```python
class UpdaterServiceBackend(Protocol):
    def detect(self) -> bool: ...           # 此后端在当前环境是否可用
    def install(self) -> None: ...          # 安装为系统服务
    def uninstall(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...

class SystemdBackend(UpdaterServiceBackend): ...
class DaemonBackend(UpdaterServiceBackend): ...
# 未来：OpenRCBackend / RunitBackend
```

### 11.2 systemd 严格检测

不能只 `command -v systemctl`（Docker/LXC/NAS 环境常有 `systemctl` 文件但 systemd 不是 PID 1）。检测必须同时满足：

1. PID 1 是 systemd（`ps -p 1 -o comm=` == `systemd`）
2. `systemctl is-system-running` 可调用（非 `offline` / `unknown`）

两者皆满足才选 `SystemdBackend`，否则 `DaemonBackend`。

### 11.3 systemd unit（P1 实现，P0 仅 DaemonBackend）

```ini
[Unit]
Description=Sakura AI Host Updater
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
ExecStart=/opt/sakura-ai/.deploy/updater/sakura-ai-updater --serve
Restart=on-failure
RestartSec=3
Group=docker

[Install]
WantedBy=multi-user.target
```

### 11.4 DaemonBackend 的 reboot 限制

`nohup` + PID 文件只能解决 SSH 断开 / shell 退出 / Sakura AI 容器重启，**不能解决宿主机 reboot**。兜底策略：

- `start.sh start` / `start.sh restart` / `start.sh status` / `start.sh update check` 均调用 `ensure_updater_running()`，发现 daemon 死亡即拉起。
- 文档明示：非 systemd 模式下，用户若绕过 `start.sh`、只靠 Docker `restart: unless-stopped` 在 reboot 后恢复 Sakura AI，则 updater daemon 不一定恢复。
- **禁止偷写 `@reboot` cron**（cron 不是所有环境都有）。

## 12. Release Manifest

### 12.1 Manifest v1（P0 minimal）

作为 Release Asset 随每个 Release 发布（`update-manifest.json`）：

```json
{
  "schema_version": 1,
  "version": "3.1.0",
  "channel": "stable",
  "min_upgrade_from": "3.0.0",
  "image": "ghcr.io/sakura520222/sakura-ai:v3.1.0",
  "updater": {
    "protocol_version": 1,
    "asset_linux_amd64": "sakura-ai-updater-linux-amd64",
    "asset_linux_arm64": "sakura-ai-updater-linux-arm64"
  }
}
```

### 12.2 P0 兼容性门禁与 update_ready

**release_visible != update_ready**——Release 刚公开时，镜像 / updater asset / manifest 可能尚未全部上传（CI 构建存在窗口期）。只有 `update_ready=true` 的 Release 才允许 `[立即更新]`：

```
update_ready = Release 非 draft
AND manifest asset 存在且校验通过
AND 目标 image manifest 存在（镜像模式：ghcr 上 :vX.Y.Z 可拉取）
AND 目标平台 updater asset 存在
AND SHA256SUMS 存在

# 然后才是版本兼容门禁：
AND SemVer 合法
AND manifest.version == release.tag（去 v 前缀）
AND current_version >= min_upgrade_from
AND updater.protocol_version 兼容
AND（镜像模式）manifest.image 存在 /（源码模式）SourceAdapter 可用
```

门禁失败时按钮禁用并显示具体原因（如"当前版本 2.9.0 低于此 Release 要求的 3.0.0，请先升级到 3.0.0"；或"Release v3.1.0 的 updater asset 尚未上传完成，请稍后"）。

### 12.3 Manifest v2（P2 扩展）

```json
{
  "schema_version": 2,
  "database_schema": 18,
  "rollback_safe": false,
  "requires_backup": true,
  "max_upgrade_from": null,
  "breaking": false,
  "min_updater_version": "1.2.0"
}
```

支撑 P2 的数据库 schema 兼容矩阵、强制备份、高级 downgrade gate。

## 13. 版本管理器（Version Manager）

### 13.1 数据模型（抄 Home Assistant）

```
current_version      已安装版本
latest_version       最新可用版本
update_available     是否有更新
target_version       要安装的版本（独立于 latest，支持指定版本更新）
```

四概念独立，优于简单的 `current != latest → update latest`。

### 13.2 当前部署信息卡

```
当前部署
────────────────────────
当前版本       v3.0.0
部署方式       Docker Image / Source
Channel        Stable
Build          a62e418（git short SHA，镜像模式可选）
镜像           ghcr.io/.../sakura-ai:v3.0.0
最近检查       2 分钟前
update_supported  true / false
```

### 13.3 Release 列表

每个 Release 卡片：版本号 + 发布日期 + Release Notes（Markdown 渲染、DOMPurify 消毒）+ 操作按钮。

**P0**：只有通过 §12.2 门禁的新版本显示 `[更新到此版本]`，当前版本标记 `[当前]`，历史版本无切换按钮。

**P1**：受 compatibility gate 控制的 `[切换到此版本]`：

```
v3.2.0  最新        [更新]
v3.1.0              [更新到此版本]
v3.0.0  当前
v2.13.0             [不兼容，禁止切换]
```

### 13.4 更新进度展示与 reconnect

RESTARTING 期间 Web 后端消失，**浏览器无法继续轮询**（后端没了请求必失败），更不能直接访问宿主机 UDS。实际流程：

```
浏览器将 job_id 存入 sessionStorage
↓
RESTARTING 导致轮询请求 connection error
↓
前端进入 reconnecting 状态，指数退避重试
↓
新 Web 后端 ready（/health 可达）
↓
前端经 Web 后端 proxy 继续查询同一 job_id（Web 后端再走 UDS 问 updater）
↓
显示最终 SUCCESS / FAILED
```

- 新 Web 后端恢复前：显示"更新进行中，正在重启服务…"
- 新 Web 后端恢复后：从 sessionStorage 取回 job_id 自动接回，显示最终结果
- job_id 之所以跨重启可接回：updater 持久化了 state，新 Web 后端启动后从 updater 读到完整 job 历史

### 13.5 navbar 版本区域

在 [navbar.html](backend/webui/templates/components/navbar.html) 现有 super_admin 按钮组旁常驻：

```
无更新：     v3.0.0
有更新：     v3.0.0  ● v3.1.0 可用
更新中：     v3.0.0  更新中 · HEALTH_CHECKING
失败：       v3.0.0  更新失败
```

P0 显示当前状态名（如 `更新中 · DOWNLOADING`），**不显示无数学依据的百分比**；百分比留到 P1 进度阶段细化。

点击整个版本区域进入版本管理器（不额外塞巨大"更新"按钮）。

## 14. 恢复与回滚（Recovery / Rollback）— P1

### 14.1 回滚约束（永久）

数据库 schema 迁移单向不可逆。例：

```
3.0 DB schema = 10
升级 3.1 → 启动自动 migrate → DB schema = 11 → health check 失败
此时 docker compose 回退 3.0 镜像 ≠ 回滚，而是"旧程序 + 新 DB schema"，故障更严重。
```

因此**镜像回滚必须受 manifest 约束**，且 P1 的 RollbackManager 优先支持"代码回滚"而非"数据库回滚"。

### 14.2 P1 回滚流程

```
BACKING_UP
  └─ 备份 from_image digest / from_version（P0 已记录）
ROLLING_BACK
  ├─ 校验 rollback_safe（manifest v2，P2）/ P1 用简单规则
  ├─ docker pull from_image（确保旧镜像本地存在）
  └─ 写回 SAKURA_AI_IMAGE + compose up -d
HEALTH_CHECKING
ROLLED_BACK / FAILED
```

### 14.3 P0 仅保留诊断信息

P0 不实现回滚动作，但 `update-state.json` 完整记录 `from_image` / `from_digest` / `from_version`，管理员可据此手动恢复，P1 RollbackManager 直接复用。

## 15. 安全（Security）

| 维度 | 措施 |
| --- | --- |
| Docker socket | 业务容器不挂；Docker 权限只给宿主机 Updater |
| UDS 权限 | `root:sakura-ai 0660` |
| IPC 请求 | 强类型（Pydantic `Literal`），无万能 `command` |
| 版本号注入 | 三重校验：Release 存在 + manifest.version == tag + SemVer 合法；禁止 path traversal |
| 外部命令 | 全部 `create_subprocess_exec` argv 化，禁止拼 shell string |
| WebUI 授权 | 更新/回滚端点 `require_super_admin` + `require_csrf_header`（复用 [deps.py](backend/webui/deps.py#L463) / [system_config.py](backend/webui/routes/system_config.py#L244) 模式） |
| Release Notes | Markdown 渲染后 DOMPurify 消毒，防 XSS |
| Manifest | SHA256 校验 updater 二进制完整性 |
| 审计 | 更新/回滚走 `log_admin_action`（复用现有 super_admin 操作审计） |

## 16. 打包与发布（Packaging）

### 16.1 PyInstaller 构建

updater 源码与主仓库同仓（`updater/` 目录），CI 构建：

```
updater/
├── pyproject.toml
├── src/
│   └── sakura_ai_updater/
│       ├── __main__.py        # --serve / --one-shot
│       ├── ipc.py             # HTTP over UDS
│       ├── state.py           # 状态机 + atomic write
│       ├── jobs.py            # job 队列 + 全局锁
│       ├── manifest.py        # Release manifest 解析校验
│       ├── semver.py
│       ├── adapters/
│       │   ├── image.py
│       │   └── source.py
│       └── backends/
│           ├── systemd.py
│           └── daemon.py
└── build/                     # PyInstaller onefile spec 与构建脚本
```

每个发布产物必须是 PyInstaller **onefile** executable，而不是 onedir。onefile 启动时会把自身解包到临时目录；daemon 通过受控的 `TMPDIR` 指向 `.deploy/updater/tmp`（目录 `0700`），避免宿主 `/tmp` 使用 `noexec` 时无法启动。受控临时目录只改变 onefile 解包位置，不改变 updater state、PID meta、UDS 或 lock 路径。这个低频启动时的解包成本换取单文件安装、替换和回滚边界清晰。

### 16.2 构建矩阵

| 产物 | 构建环境 | 说明 |
| --- | --- | --- |
| `sakura-ai-updater-linux-amd64` | native `ubuntu-24.04` runner，使用 `python:3.12-slim-bullseye` 构建容器 | 以 glibc 2.31 为兼容性基线 |
| `sakura-ai-updater-linux-arm64` | native `ubuntu-24.04-arm` runner，使用 `python:3.12-slim-bullseye` 构建容器 | ARM 服务器；不使用 QEMU 或 cross-PyInstaller |

构建容器固定为 `python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db`。`build.sh` 在构建容器内执行 PyInstaller onefile 构建、构建容器 smoke，并调用 outer onefile ELF/bootloader GLIBC checker；checker 只解析最终 onefile 的 outer ELF/bootloader `GLIBC_X.Y` needs，最大版本必须 `<= 2.31`，不读取 embedded ELF。该 ceiling 只是污染检测，不能替代真实的 old-glibc 构建。

构建容器成功后，每个架构还必须在干净、固定的 `debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792` runtime 中执行 authoritative lifecycle smoke。runtime 以只读 mount 注入 final onefile 与 smoke helper，先复制为 root-owned `0700` `/usr/local/libexec/sakura-ai-updater`，创建 `0700` state/tmp 目录并导出 `TMPDIR=/run/sakura-ai-smoke/tmp`，再用统一 path 参数依次验证 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、UDS `/v1/health`、`backend stop`，以及停止后 `backend is-running` 返回 1。不能用 build container 的运行结果替代该 fresh-runtime 证据；任何 runtime harness 失败都阻断资产上传。

**老 glibc 基线是硬要求**——用 Ubuntu 最新版打包会导致 Debian 11 / 老 NAS 上 `GLIBC_X.XX not found`。

### 16.3 Release Assets

Slice 3c 的 Release assets **仅包含**：

```
sakura-ai-updater-linux-amd64
sakura-ai-updater-linux-arm64
SHA256SUMS              （恰好包含上述两者的 sha256）
```

`update-manifest.json` 及其 manifest parsing、compatibility gate、`min_upgrade_from` authoritative policy 属于 Slice 4；3c 不生成、上传或硬编码这些内容。最终 P0 manifest v1 的 schema 仍按 §12.1 执行，但其产生时机延后到 Slice 4。

### 16.4 安装流程

`start.sh updater install` **默认安装与当前部署版本对应 Release 附带的 updater**——**不从"最新 Release"下载**，避免协议启动悖论（运行 v3.0.0 的应用若下载未来 v4.0.0 的 protocol v2 updater，当前 Web backend 不懂）。首次 acquisition 属于 `start.sh` host bootstrap：宿主机无 Python 且 binary 尚不存在时，shell 直接通过 HTTPS 下载当前版本对应架构的 binary 与 `SHA256SUMS`，严格校验后再调用已安装 binary 的 `backend install`。Python downloader 与 updater self-update / 寻找最新兼容 updater 留给 P2。

版本解析是 **deployment-mode-aware** 的：

- `SAKURA_DEPLOY_MODE=image`：concrete `SAKURA_AI_IMAGE=:vX.Y.Z[@sha256:...]` 为权威版本；镜像为 `:latest` 或无具体 tag 时回退到 `backend/__init__.py` 的 `__version__`。镜像部署时 host checkout 版本与实际运行版本可能不同，镜像版本始终权威——二者不冲突，package version 不参与 conflict gate。
- `SAKURA_DEPLOY_MODE=source`：`backend/__init__.py` 的精确 `__version__ = "X.Y.Z"` 为权威版本。
- `deployment.env` 缺失或 `SAKURA_DEPLOY_MODE` 非 `image`/`source` → fail-closed，不猜测版本。

```
检测 OS / ARCH
↓
读取 SAKURA_DEPLOY_MODE（image/source），缺失或无效 → fail-closed
↓
image 模式：concrete image tag 权威；:latest / 无 tag → 回退 package __version__
source 模式：package __version__ 权威
↓
最终版本用 ^[0-9]+\.[0-9]+\.[0-9]+$ 校验；不支持 prerelease/build metadata
↓
从该 Release 通过 HTTPS 下载对应 updater binary + SHA256SUMS
↓
严格解析 SHA256SUMS，校验目标 asset 恰好一条且 hash 匹配
↓
chmod 0700、临时文件 fsync、临时文件安全检查
↓
同一 state 目录内 `mv -f` 原子替换（commit point）
↓
`sync "$UPDATER_STATE_DIR"` 持久化目录 metadata，再确认 final inode 安全
↓
创建 host sakura-ai 组（固定 GID 9472）并调用 binary backend install
```

state directory 安全迁移：首次创建为 root-owned `0700`。如果目录已存在（如从 3b 升级，典型 root:root 0755），只要 owner 为 root 且 group/other 无写权限就自动 harden 到 `0700`；owner 非 root、group/other 可写（0770/0775/0777）或 symlink → fail-closed。

安装安全不变量：生产 binary、state directory 和 install lock 均为 root-owned；binary/state directory 使用 `0700`，安装锁防并发，同文件系统临时文件保证 rename 原子性；下载、checksum、chmod、临时 fsync、临时 safety 任一 pre-commit 失败时，旧 final binary 必须 byte-for-byte unchanged。commit 后目录 metadata fsync 或 rename 后 final safety confirmation 失败时，不得作"旧 binary 未变"承诺，必须明确说明新 inode 可能已安装，且不得继续调用 `backend install`。checksum 与 binary 使用同一 GitHub Release 信任根，3c 提供 HTTPS 传输与 SHA256 完整性校验，但不抵御 Release 发布凭据整体失陷；独立签名与更强 trust root 留给 P2。

如果 daemon 在 acquisition 前已经运行，安装成功也**不自动重启**：Linux rename 只替换目录项，运行进程继续使用旧 inode。管理员必须显式执行：

```bash
sudo ./start.sh updater stop
sudo ./start.sh updater start
```

`.deploy/updater/` 目录结构（持久），与 §7.1 一致：

```
.deploy/updater/
├── sakura-ai-updater          # 二进制
├── updater.pid                # daemon 模式 PID
├── updater.log                # 日志
├── update-state.json          # 持久化状态
└── updater.lock               # OS-level flock

/run/sakura-ai/                # 易失 runtime，daemon 启动时创建
└── updater.sock
```

### 16.5 CI 集成与 atomic publish

`updater-build.yml` 是仅接受 `workflow_call(version)` 的 reusable workflow：native amd64/arm64 matrix 分别完成 pinned Python 3.12 bullseye build container 的 onefile 构建、outer ELF/bootloader ceiling gate，以及 pinned clean bullseye runtime 的完整 lifecycle smoke。每个 matrix artifact 只有双门禁均成功后才上传；single-writer publish job fan-in 两个 artifact，固定顺序生成唯一 `SHA256SUMS`，并用一次带 `--clobber` 的 upload 上传两个 binary 与 checksum。

现有 `release-on-pr-merge.yml` 保持 Release 的唯一 create/edit owner。它先完成 source tar/zip 的定向清理与上传，再由 `generate-release` 和 `build-and-upload-assets` 均成功的 caller job 调用 reusable workflow。`updater-build.yml` 绝不 create/edit Release，只验证 Release 存在并 upload updater assets；stable image job 保持原有 `needs`/`if` 语义并可与 updater caller 并行。并发策略仍为排队执行（`cancel-in-progress: false`）。

3c 的 trust model 是同 channel、同一 GitHub Release 的 binary 与 `SHA256SUMS`：这提供传输/存储完整性校验，不提供独立签名信任根；same-channel trust 与更强 P2 signature policy 由后续阶段补足。`update-manifest.json`、`min_upgrade_from` 与 authoritative update readiness gate 延后到 Slice 4。



## 17. 交付计划（Delivery Plan）

P0 / P1 / P2 是同一份设计的分阶段交付，**不是三套独立设计**。实现按多个 PR 切。

### 17.1 P0 — 镜像模式生产可用闭环

**Update Checker**
- startup 非阻塞检查（启动后 5–30s）
- 60min scheduler（asyncio）
- manual refresh
- GitHub Releases API
- Redis 缓存 + last-known-good
- 网络失败保留缓存
- SemVer 比较

**WebUI**
- navbar 版本区域（常驻当前版本 + 更新 badge + updating state）
- 版本管理器页面（当前部署信息卡 + Release 列表 + Release Notes Markdown 渲染 + sanitize）
- super_admin 授权 + CSRF
- 更新进度轮询（job_id）

**Updater Core**
- Python daemon（PyInstaller 二进制）
- UDS IPC（HTTP over UDS，协议 v1 + body envelope）
- durable job state（atomic write）
- 全局更新锁两层（OS-level flock + 任务锁 + 409 Conflict）
- 崩溃恢复 reconcile（interrupted job 标 INTERRUPTED + 清理）
- preflight
- 状态机（P0 子集）
- 结构化日志

**Deployment**
- 部署模式检测（`SAKURA_DEPLOY_MODE`）
- ImageAdapter（pull → activate → restart → health check）
- Source 模式识别但 apply 禁用（`update_supported: false`）
- pull before touching current container

**Bootstrap**
- DaemonBackend（零依赖先跑通）
- `ensure_updater_running()` 兜底
- start.sh CLI（`updater install/start/stop/status` + `update check/apply` + 隐藏 `internal` 原语）

**发布工程（提前到 P0）**
- PyInstaller amd64 / arm64
- 老 glibc 构建基线（debian:bullseye）
- Release Asset 发布
- SHA256 校验
- `updater-build.yml` CI

**兼容性（minimal）**
- `update-manifest.json` v1
- Release/tag/version 一致性校验
- `min_upgrade_from` 门禁
- updater protocol_version 协商

**P0 明确不做**：Source 自动更新、downgrade、rollback、历史版本切换、beta channel、自动更新、SystemdBackend、BACKING_UP/ROLLING_BACK 状态。

### 17.2 P1 — Source + Recovery

- SourceAdapter（`git fetch --tags` + worktree/releases staging + 依赖预安装 + current 切换 + restart，见 §10.2）
- SystemdBackend（严格检测 PID 1 == systemd + `systemctl is-system-running`）
- BACKING_UP / ROLLING_BACK / ROLLED_BACK 状态
- 安全 rollback（受 compatibility gate 控制）
- 版本管理器首次支持 `[切换到此版本]`
- 进度阶段细化
- `start.sh rollback vX.Y.Z` CLI

### 17.3 P2 — Release Policy / Advanced Safety

- Manifest v2（`database_schema` / `requires_backup` / `rollback_safe` / `max_upgrade_from` / `breaking` / `min_updater_version`）
- stable / beta channel
- dry-run / preflight 专属 UI
- DB + config 自动备份
- 高级 compatibility matrix
- Updater self-update policy + `min_updater_version` 强制约束

## 18. 验收标准（P0）

1. 镜像模式部署后，`./start.sh start` 自动安装 updater daemon，navbar 右上角常驻 `v3.0.0`。
2. GitHub 发新 Release `v3.1.0`（含 `update-manifest.json`），60 分钟内或手动刷新后 navbar 显示更新提示。
3. super_admin 点击进入版本管理器，看到 v3.1.0 的 Release Notes，`[更新到此版本]` 按钮启用（门禁通过）。
4. 点击更新，updater 经 PREFLIGHT → DOWNLOADING（docker pull）→ ACTIVATING → RESTARTING → HEALTH_CHECKING → SUCCESS。HEALTH_CHECKING 含版本验证 gate（`reported_app_version == target_version`）。Web 后端消失期间浏览器进入 reconnecting 指数退避，新版本恢复后从 sessionStorage 取回 job_id，显示"已更新至 3.1.0"。
5. 失败 SLA 按阶段：PREFLIGHT / DOWNLOADING 失败时当前版本**完全不受影响**、仍正常服务；ACTIVATING 之后失败（如 health check 超时 / 版本验证 gate 不通过）时显示 FAILED + 原版本 / 原镜像 digest / 失败阶段诊断信息，**P0 不承诺自动恢复服务**（管理员手动恢复，P1 RollbackManager 自动化）。
6. 源码模式部署时，版本管理器显示 `update_supported: false`，更新按钮禁用并提示原因。
7. 两个浏览器 / `start.sh update apply` 并发触发更新时，第二个返回 409 Conflict。
8. 宿主机无 Python 环境时，updater 二进制仍可正常安装运行。
9. `min_upgrade_from` 门禁生效：当前版本低于要求时按钮禁用并显示原因。
10. updater daemon 意外退出后，`start.sh start` / `status` 等操作通过 `ensure_updater_running()` 自动拉起。
