# Sakura-AI Auto-Update P0 — Slice 4 实施计划

## 镜像模式端到端更新闭环（v2）

> 状态：草案 v2，待用户快速复核  
> 日期：2026-08-09  
> 分支：`feature/3.0.0-refactor`  
> 前置依赖：Slice 1 / 2 / 3a / 3b / 3c（全部已冻结）  
> 设计基准：[2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md)

---

## 0. v2 修订摘要

v1 经审查裁定 BLOCKED（D3/D4 PARTIALLY REJECTED + 7 项 correctness / frozen-contract 问题）。
v2 全部修正，逐项映射如下，便于快速复核：

| # | 审查项 | v2 处理 | 对应章节 |
|---|---|---|---|
| 1 | Manifest v1 schema：`asset_linux_*` 须在 `updater` 对象内 | 按冻结 §12.1 schema 改正 | §5 D1、§7.1、S4-1、S4-2 |
| 2 | ImageAdapter 不得在 event loop 用同步 `subprocess.run` | 全 async：`asyncio.create_subprocess_exec` + `asyncio.to_thread`，不引入 httpx 生产依赖 | §7.2、§7.4、S4-3 |
| 3 | DOWNLOADING 不得改 deployment.env；ACTIVATING 用 `--env-file` | pull/activate 拆分，pull 只 `docker pull`，activate 才写 env + `compose --env-file -f up -d` | §7.2、§7.3、S4-3 |
| 4 | Release DAG 真实可跑：`needs` 含 generate-release、版本 source、`always()` 覆盖 skipped propagation、checkout、image 存在性校验 | §7.6 重写 job 契约 | §7.6、S4-2 |
| 5 | `CancelledError` 不得清 `active_job_id`（否则破坏 reconcile invariant） | success/failed 清 gate；CancelledError 不清、re-raise、交 reconcile | §7.4、S4-4、§9 |
| 6 | 当前部署/版本/digest authoritative provider + `from_digest` 容器 inspect + `:latest` materialization | `DeploymentStateProvider`：from_digest 容器 inspect + `materialize_current_anchor`（§9.5） | §7.4、S4-4、S4-6 |
| 7 | Preflight 须含 mode/newer/min/disk 四 gate | §7.5 checks 补全，`can_update=false` vs 422 分层 | §7.5、S4-4 |
| + | `update_available` token 漏列 | 状态机补回：`checking → update_available → preflight` | §5 D4、§7.3、S4-0 |

裁定落点：
- **D1** APPROVED — `min_upgrade_from` 用 manifest 字段，P0 固定 `"0.0.0"`。
- **D2** APPROVED WITH CORRECTION — 不加 `start.sh update apply`；adapter 改 async（项 2）。
- **D3** PARTIALLY REJECTED — tag 用于 manifest/target ref；**from_digest 捕获是 P0 必须**（§9.5 首次接管 `:latest` 也要存 immutable digest）；目标镜像 digest pinning 才是 P2。
- **D4** PARTIALLY REJECTED — 小写 `snake_case` + 补回 `update_available`。
- **D5** APPROVED — `rolled_back` 保留字，`/v1/rollback` 返回 501。

---

## 1. 目标

1. **Release Manifest v1**：每次稳定版发布附带结构化清单（冻结 §12.1 schema），声明版本、镜像、更新器协议兼容性、最低升级来源、架构资产。
2. **ImageAdapter**：镜像模式部署原语（预检/拉取/激活/健康检查），全 `asyncio` 非阻塞，`argv` 子进程不经 shell，不挂 docker.sock。
3. **更新状态机**：冻结 P0 公共状态（含 `update_available`），持久化进度，崩溃/重启经 Slice 3a reconcile 恢复一致。
4. **IPC 动作端点**：`/v1/check`、`/v1/preflight`、`/v1/update`、`/v1/rollback`、`/v1/jobs/{id}`、`/v1/jobs/{id}/logs`，复用 v1 envelope 与 409 冲突契约。
5. **任务编排**：同时只允许一个破坏性 update；两层锁（flock + asyncio.Lock）+ active_job_id gate；`CancelledError` 保留 gate 交 reconcile；结构化日志。
6. **镜像更新流程**：check → preflight（mode/newer/min/disk 四 gate）→ download（不改 deployment.env）→ activate（capture from → 写 env → compose up）→ restart → health（版本门控）→ success/failed，P0 不自动回滚。
7. **后端授权入口**：WebUI super_admin + CSRF 代理路由，浏览器绝不直连 UDS，后端不执行 Docker/Git。
8. **WebUI 闭环**：target/preflight/update/job 进度，Web 重启 reconnect 而非误报失败。

## 2. 非目标（明确推迟）

| 项 | 推迟到 |
|---|---|
| SourceAdapter（源码部署更新） | P1 |
| SystemdBackend（systemd 原语） | P1 |
| 更新器自更新（updater 升级自身二进制） | P1 |
| 最新兼容更新器搜索（跨版本回溯） | P1 |
| 目标镜像 digest pinning（用 digest 替代 tag 拉取） | P2 |
| minisign/cosign 独立信任根、镜像签名验证 | P2 |
| 自动回滚（健康检查失败自动 revert） | P1 |
| P1 备份状态（`backup` 字段、回滚镜像保留策略） | P1 |
| 预发布版（prerelease）支持 | P1 |
| `start.sh update apply` CLI | P1 |
| 移动端/API consumer 的 v1 更新接口 | 按需 |
| WebUI 整体重构（仅增量扩展版本管理器） | — |
| Slice 3c PyInstaller 链改动 | — |

---

## 3. 冻结契约依赖

Slice 4 以下列已冻结契约为前置事实，**不改其结构**：

### 3.1 Slice 3a — 状态与锁

- [state.py](../../../updater/src/sakura_ai_updater/state.py) `JobState` 字段已覆盖 Slice 4 所需全部字段，**不新增字段**。
- `TERMINAL_STATES = {"success", "failed", "rolled_back"}` 不变。
- `ERROR_CODE_INTERRUPTED = "interrupted"` 不变；中断 = `state="failed" + error_code="interrupted"`。
- `UpdateStateStore` wrapper 不变；`load_state` fail-closed、`save_state` 原子写不变。
- `reconcile_interrupted_job` 的 6 条 invariant 不变。**第 2 条**（`active_job_id == null AND current_job 非 terminal → StateCorruptionError`）直接约束编排器的 `CancelledError` 处理（项 5）。

### 3.2 Slice 3a — IPC 骨架

- v1 body envelope `{protocol_version, updater_version, data}`；版本字段只在顶层。
- 成功（2xx）走 envelope；错误（4xx/5xx）不走 envelope；409 body `{error, job_id}`。
- `create_app(state_path)` 保持纯 HTTP app；Slice 4 经 `app.state` 注入编排器。

### 3.3 Slice 3b — 守护生命周期

- `DaemonBackend` PID 三重校验、UDS pre-bind 监听器移交不变。
- Slice 4 只在 `_serve_args()` **追加** `--compose-file` / `--deployment-env`（S4-6）。
- `_PID_META_FILENAME = "daemon-meta.json"` 不变（spec §11 文档漂移在 S4-0 修正描述）。

### 3.4 Slice 3c — 资产发布

- `updater-build.yml`（reusable `workflow_call`）的 PyInstaller bullseye onefile 构建、原生 arm64 runner、SHA256SUMS 单写者、3 资产终态不变。
- Slice 4 在 `release-on-pr-merge.yml` **追加** `publish-update-manifest` 作业（S4-2），不改 `publish-updater-assets` / `publish-stable-image` 既有逻辑。

### 3.5 Slice 1/2 — 版本展示

- `backend/__init__.py` 的 `__version__` 是应用版本唯一代码来源。
- `UpdateChecker` 只做 discovery + Redis 缓存 + navbar 提示，**不**承担 destructive gate。
- `GET /health` 的 `version` 字段是 ImageAdapter 版本门控唯一权威来源；`GET /api/v1/health` 的 `version` 是 API 协议版本，**不得**用于版本门控。
- `update_available`（discovery derived）与 `update_ready`（manifest gate）是两个独立概念。

### 3.6 部署事实（来自当前代码）

- [docker-compose.prod.yml:7-8](../../../docker/docker-compose.prod.yml#L7-L8)：`image: ${SAKURA_AI_IMAGE:-...:latest}`、`container_name: sakura-ai`（**固定**，非 compose 默认命名）。
- [docker-compose.prod.yml:12-13](../../../docker/docker-compose.prod.yml#L12-L13)：`env_file: ../.deploy/deployment.env` 是**容器环境变量**，不等于 compose model 插值。`${SAKURA_AI_IMAGE}` 插值须由 shell / `.env` / CLI `--env-file` 提供；`start.sh` 已显式 `--env-file .deploy/deployment.env`。
- [release-on-pr-merge.yml](../../../.github/workflows/release-on-pr-merge.yml)：非 `workflow_call`，触发为 `push: main` / `workflow_dispatch`；版本来自 `needs.generate-release.outputs.version`；`generate-release` 输出 `version`/`release_exists`/`release_action`（`created` / `updated`）；`publish-stable-image` `if: release_action == 'created'`（refresh 时 **skipped**）。
- [pyproject.toml](../../../updater/pyproject.toml)：updater 生产依赖仅 `fastapi`/`uvicorn`/`pydantic`；`httpx` 仅 dev。**Slice 4 不新增生产依赖**，网络 I/O 用标准库 + `asyncio.to_thread`，子进程用 `asyncio.create_subprocess_exec`。

---

## 4. 文档漂移修正（Task S4-0 交付）

| 编号 | 漂移 | 修正 |
|---|---|---|
| D1 | spec §8.1 状态 token 大写，且 v1 计划漏列 `update_available` | 改小写 `snake_case` + **补回 `update_available`**，与冻结 §8.1 完整枚举一致（见 §7.3） |
| D2 | spec §16.1 文件树列出不存在的文件 | 标注为「Slice 4 交付」 |
| D3 | spec §9 描述 ImageAdapter 经 `start.sh` 子命令重启 | 改为 ImageAdapter 直接 async argv 调 `docker compose`，不经 start.sh（决策 D2） |
| D4 | spec §11 称 PID meta 文件名为 `updater.pid` | 改为 `daemon-meta.json`，与 [daemon.py:36](../../../updater/src/sakura_ai_updater/backends/daemon.py#L36) 一致 |

---

## 5. 关键决策记录（含审查裁定）

### D1 — `min_upgrade_from` 来源（APPROVED）

Manifest v1 字段，发布流水线生成时写入，P0 固定 `"0.0.0"`（允许所有历史稳定版升级）。不引入额外 policy 文件。P1 引入真实兼容性策略时只改清单生成器，不改 schema/parser。

### D2 — `start.sh update apply` CLI（APPROVED WITH CORRECTION）

Slice 4 **不**在 `start.sh` 加 update 子命令。ImageAdapter 在 updater 进程内 async argv 调 `docker compose` / `docker`。路径（compose 文件、deployment.env）经 DaemonBackend 配置传入（S4-6）。**实现方式修正**：全部 async（项 2），不用同步 `subprocess.run`。

### D3 — 镜像引用与 digest（PARTIALLY REJECTED → v2 折中）

- **APPROVED**：Manifest v1 `image` 字段、`deployment.env` 的 `SAKURA_AI_IMAGE`、pull 目标统一用 tag 格式 `ghcr.io/sakura520222/sakura-ai:vX.Y.Z`。
- **REJECTED 部分**：「digest pinning 全部 P2」不成立。冻结 §9.5 要求首次接管 `:latest` 时必须保存实际 immutable digest。
- **v2 决策**：
  - **tag 用于 ref 传递**（manifest `image` / deployment.env SAKURA_AI_IMAGE / pull 目标）。
  - **`from_digest` 捕获是 P0 必须**：每次 activate 前 inspect 运行容器取 immutable image identity；**首次接管 `:latest` 也必须捕获**，使 `:latest` 后续漂移时仍有可靠恢复锚点（§9.5）。
  - 目标镜像 **digest pinning**（pull 用 digest 替代 tag）才是 P2。
- **`:latest` 首次接管 authoritative materialization**（冻结 §9.5）：destructive update job 在 preflight 成功后、downloading 前，若当前 `SAKURA_AI_IMAGE` 是 mutable `:latest`，则查 `/health.version` 得 `current_version` + `docker inspect` 运行容器得 `running_digest`，原子改写 deployment.env 为 `ghcr.io/.../sakura-ai:v{current_version}@sha256:{running_digest}`。reboot 后不再重新解析 mutable `:latest`；`from_image` 记录 materialized ref；Slice 3c 已支持 `vX.Y.Z@sha256:...` 格式解析。**仅在 destructive job 内执行**，read-only `/v1/check`/`/v1/preflight` 不改 deployment.env。

### D4 — 状态值大小写 + `update_available`（PARTIALLY REJECTED → v2 折中）

- **APPROVED**：传输值与持久化值统一小写 `snake_case`，与 [state.py](../../../updater/src/sakura_ai_updater/state.py) 一致。
- **REJECTED 部分**：v1 漏列冻结 §8.1 的 `update_available` token。
- **v2 完整 P0 状态枚举**（§7.3）：

```
idle / checking / update_available / preflight / downloading /
activating / restarting / health_checking / success / failed
```

`rolled_back` 是 P1 预留 terminal token（`TERMINAL_STATES` 已含，P0 不产生）。

### D5 — `rolled_back` 保留字（APPROVED）

保持 `rolled_back` 在 `TERMINAL_STATES`；`/v1/rollback` P0 返回 501 `not_implemented`；不实现 RollbackManager。

---

## 6. 阻塞项与处理

### B1 — 清单写入器作业缺失（→ S4-2）

[test_release_workflows.py:160-161](../../../tests/test_release_workflows.py#L160-L161) 断言两工作流都不出现 `update-manifest.json`。S4-2 新增 `publish-update-manifest` 作业 + 放宽契约测试（项 4）。

### B2 — `from_digest` 捕获 + `:latest` authoritative materialization（→ S4-4，§9.5）

activate 前 inspect **运行容器**（`container_name: sakura-ai` 固定）取 immutable image identity：

```
docker inspect --format='{{.Image}}' sakura-ai
```

明确语义：这是**容器 inspect**（返回容器启动时锁定的 image digest），不是 `docker image inspect <ref>`（ref 若是 `:latest` 会返回当前解析值，非运行时锁定值）。

**:latest authoritative materialization**（冻结 §9.5）：仅 `from_digest` 记录 digest 不够——authoritative deployment.env 仍是 mutable `:latest`，reboot 后重新解析。destructive job 在 preflight 后、downloading 前执行 `DeploymentStateProvider.materialize_current_anchor()`：若当前 ref 是 `:latest`，查 `/health.version` + 容器 digest，原子改写 deployment.env 为 `vX.Y.Z@sha256:<digest>` concrete ref；ref 已 concrete tag 则跳过。归 S4-4。

### B3 — 健康检查版本门控（→ S4-3）

`GET http://localhost:8000/health` 轮询，门控：HTTP 200 **AND** `response.version == target_version`。纯 200 不足（旧容器可能仍响应）。超时 → `health_check_timeout`；version 不符 → `health_check_version_mismatch`。

### B4 — 清单生成排序竞争（→ S4-2）

`publish-update-manifest` 必须是 release DAG 最后叶子，`needs: [generate-release, publish-updater-assets, publish-stable-image]`，且显式处理 `publish-stable-image` skipped（项 4）。

### B5 — Preflight gate 缺失（→ S4-4，项 7）

补四 gate：`deployment_mode == image`、`target_version > current_version`（防 downgrade）、`current_version >= min_upgrade_from`、`disk_space_sufficient`。前三项失败属合法 preflight 结果（`can_update=false`），`/v1/update` 再转 422 `preflight_failed`。

---

## 7. 架构设计

### 7.1 Release Manifest v1（冻结 §12.1 schema，项 1 修正）

**Schema**（`update-manifest.json`，每个稳定 Release 的 asset）：

```json
{
  "schema_version": 1,
  "version": "3.1.0",
  "channel": "stable",
  "min_upgrade_from": "0.0.0",
  "image": "ghcr.io/sakura520222/sakura-ai:v3.1.0",
  "updater": {
    "protocol_version": 1,
    "asset_linux_amd64": "sakura-ai-updater-linux-amd64",
    "asset_linux_arm64": "sakura-ai-updater-linux-arm64"
  }
}
```

**`asset_linux_*` 在 `updater` 对象内**（冻结 §12.1），非顶层。这是 v1 与 v2 草案的关键差异，parser、生成器、契约测试全部按此 schema。

字段语义：
- `schema_version` 恒为 `1`；updater 拒绝未知值（向前兼容保护）。
- `version` 严格 SemVer `X.Y.Z`，**必须**与 Release tag（去 `v`）一致；不一致 → manifest 非法。
- `channel` P0 恒 `"stable"`；非 stable → updater 跳过该 Release。
- `min_upgrade_from` 决策 D1，P0 固定 `"0.0.0"`；`current_version < min_upgrade_from` → 不允许升级。
- `image` tag 格式（D3 APPROVED 部分）。
- `updater.protocol_version`：manifest 声称需要的更新器协议版本；与 `PROTOCOL_VERSION` 不兼容 → 不允许升级。
- `updater.asset_linux_amd64` / `asset_linux_arm64`：Release 中对应架构二进制资产文件名，readiness 校验用（确认资产存在 + 与 SHA256SUMS 行对应）。

**解析器**（`updater/src/sakura_ai_updater/manifest.py`）：`parse_manifest(data, *, expected_version) -> Manifest`；任一字段缺失/类型错/SemVer 非法/version 不符/`asset_linux_*` 不在 `updater` 内 → `ManifestError`。

### 7.2 ImageAdapter（全 async，项 2/3 修正）

**职责**：镜像模式部署原语。全 `asyncio` 非阻塞——子进程用 `asyncio.create_subprocess_exec`（`shell=False`，argv 无用户控制值；target_image 来自受信 manifest 结构化值），HTTP 用标准库 + `asyncio.to_thread`。**不引入 httpx 生产依赖**。

**接口**（`updater/src/sakura_ai_updater/adapters/image.py`）：

```python
class ImageAdapter:
    def __init__(
        self,
        compose_file: str,
        deployment_env: str,
        web_container: str = "sakura-ai",   # compose container_name 固定（§3.6）
        health_url: str = "http://localhost:8000/health",
        health_timeout: float = 90.0,
        health_poll_interval: float = 2.0,
    ) -> None: ...

    async def preflight_image(self, target_image: str) -> None:
        """docker manifest inspect <target_image>（async subprocess）。
        验证目标镜像 manifest 可达。失败 → ImagePreflightError（不影响运行容器）。"""

    async def pull(self, target_image: str) -> None:
        """docker pull <target_image>（async subprocess）。
        **只拉镜像，不修改 deployment.env**（项 3：pull 失败时部署状态零影响）。"""

    async def activate(self, target_image: str) -> None:
        """原子写 deployment.env 的 SAKURA_AI_IMAGE=<target_image>
        → docker compose --env-file <deployment_env> -f <compose_file> up -d（async subprocess）。
        compose interpolation 经 CLI --env-file 提供（§3.6），不是 env_file:。"""

    async def health_check(self, target_version: str) -> None:
        """轮询 health_url 至 health_timeout：标准库 socket HTTP（asyncio.to_thread）。
        门控 HTTP 200 AND response.version == target_version（B3）。
        超时 → health_check_timeout；version 不符 → health_check_version_mismatch。"""
```

**关键修正（项 3）**：v1 的 `pull()` 先写 deployment.env 再 compose pull 是错的——pull 失败但 deployment.env 已改，破坏「DOWNLOADING 失败不改变部署」SLA。v2 拆分：

| 状态 | 操作 | deployment.env |
|---|---|---|
| `downloading` | `docker pull <target_image>` | **不改** |
| `activating` | capture from_image/from_digest → 原子写 deployment.env → `docker compose --env-file <env> -f <file> up -d` | 改 |

pull 失败 → deployment.env 未变 → 下次 reboot 仍用旧镜像。activate 失败 → deployment.env 已改但容器可能中间态 → `failed` + `from_digest` 记录（手动恢复）。

**`from_digest` 捕获**由 `DeploymentStateProvider` 提供（§7.4），不在 ImageAdapter 内，避免 adapter 持有部署读职责。

**deployment.env 原子写**（`write_deployment_env`）：复用 start.sh `init_deployment_env` 约定（temp → fsync → rename）；`SAKURA_AI_IMAGE` 行存在则替换，不存在则追加；保留其他行。

### 7.3 更新状态机（含 `update_available`，项 + 修正）

**P0 状态**（决策 D4，全小写）：

```
idle ── POST /v1/update ──► checking ──► update_available ──► preflight ──► downloading ──► activating ──► restarting ──► health_checking ──► success
                                 │             │                  │              │              │              │                  │
                                 └─────────────┴──────────────────┴──────────────┴──────────────┴──────────────┴──────────────────┘ failed
                                                                                                                                 (error_code)
```

- `idle`：无 job。
- `checking`：读最新 Release manifest。
- `update_available`：确认 target 存在且 `target_version > current_version`（冻结 §8.1 token，v1 漏列，v2 补回）。
- `preflight`：dry-run 四 gate（mode/newer/min/disk）+ 镜像 manifest 存在性（B5）。preflight 成功后执行 `materialize_current_anchor`（§9.5）：若当前 ref 是 `:latest` 则原子具体化为 `vX.Y.Z@sha256:<digest>`，随后才进入 downloading。**不新增状态**，`job.step="materialize_current_anchor"` 标记。
- `downloading`：`docker pull <target_image>`（不改 deployment.env；materialize 已在 preflight 后完成）。
- `activating`：capture from_image/from_digest → 写 deployment.env → `compose up -d`。
- `restarting`：compose `up -d` 返回后到 health 可达前。
- `health_checking`：health 轮询 + 版本门控（B3）。
- `success` / `failed`：终态。`rolled_back` 保留字（D5），P0 不产生。

**双角色**：`checking`/`update_available`/`preflight` 既是 job 步骤，也复用相同逻辑服务 `/v1/check`、`/v1/preflight` 同步端点（不建 job、不写 active_job_id）。

**错误代码**（`JobState.error_code`，小写 `snake_case`）：

| error_code | 阶段 | 语义 |
|---|---|---|
| `update_in_progress` | 提交 | 已有 active job（409） |
| `target_not_found` | checking | target_version 不在 Release 列表 |
| `target_not_newer` | update_available | `target_version <= current_version`（防 downgrade） |
| `deployment_mode_not_image` | preflight | `SAKURA_DEPLOY_MODE != image` |
| `min_upgrade_from_unsatisfied` | preflight | `current_version < min_upgrade_from` |
| `disk_space_insufficient` | preflight | 可用空间 < 阈值 |
| `preflight_failed` | preflight | 镜像 manifest 不可达 / protocol 不兼容等 |
| `manifest_not_found` | checking/preflight | Release 无清单或清单非法 |
| `pull_failed` | downloading | `docker pull` 失败 |
| `activate_failed` | activating | `compose up -d` 失败 |
| `health_check_timeout` | health_checking | 超时未就绪 |
| `health_check_version_mismatch` | health_checking | HTTP 200 但 version 不符 |
| `interrupted` | 任一 | 进程崩溃/daemon 重启（reconcile 设置） |
| `internal_error` | 任一 | 未分类异常 |

### 7.4 JobOrchestrator + DeploymentStateProvider（项 5/6 修正）

#### 7.4.1 DeploymentStateProvider（项 6 新增）

**职责**：authoritative 读取当前部署状态（deployment.env + 运行容器），**每次现读不缓存**（updater daemon 跨 Web 容器更新存活，不能在启动时快照版本）。

`updater/src/sakura_ai_updater/deployment.py`：

```python
class DeploymentStateProvider:
    def __init__(self, deployment_env: str, web_container: str = "sakura-ai",
                 health_url: str = "http://localhost:8000/health") -> None: ...

    def read_image_ref(self) -> str | None:
        """从 deployment.env 读 SAKURA_AI_IMAGE（authoritative 当前镜像 ref）。"""

    def read_deploy_mode(self) -> str | None:
        """SAKURA_DEPLOY_MODE 优先从 deployment.env 读；缺失则从进程环境变量读
        （start.sh 启动 daemon 时注入）。两者都缺 → None（preflight gate fail）。"""

    async def resolve_current_version(self) -> str | None:
        """推导当前应用版本（async，可能涉及 HTTP）：
        - image_ref 匹配 vX.Y.Z tag → 从 tag 解析（无 HTTP）
        - image_ref 是 :latest 或不可解析 → 查 health_url.version（标准库 socket + to_thread）
        - image_ref 缺失 → None
        """

    async def capture_from_image(self) -> str | None:
        """激活前 = read_image_ref()（from_image 来源）。"""

    async def capture_from_digest(self) -> str:
        """激活前从运行容器取 immutable image identity（B2/§9.5）：
        docker inspect --format='{{.Image}}' <web_container>（容器 inspect，async subprocess）。
        返回容器启动时锁定的 image digest；即使 ref 是 :latest 也 immutable。
        web_container='sakura-ai'（compose container_name 固定，§3.6）。
        """

    async def materialize_current_anchor(self) -> str:
        """§9.5 首次接管 :latest 的 authoritative materialization。
        **仅在 destructive job 内调用**（preflight 后、downloading 前）；
        read-only /v1/check、/v1/preflight 不改 deployment.env。

        若 read_image_ref() 是 mutable :latest：
          resolve /health.version → current_version
          capture_from_digest() → running digest
          原子改写 deployment.env: ghcr.io/.../sakura-ai:v{current_version}@sha256:{digest}
        若 ref 已是 concrete（vX.Y.Z 或 vX.Y.Z@sha256:...）：不处理。
        返回 materialized（或原有）concrete ref。Slice 3c 已支持此格式。
        """
```

`from_digest` 语义明确：容器 inspect（运行容器实际 image identity），非 image ref inspect（§9.5 首次接管 latest 锚点）。

#### 7.4.2 JobOrchestrator（项 5 CancelledError gate 修正）

`updater/src/sakura_ai_updater/jobs.py`：

```python
class JobOrchestrator:
    def __init__(
        self,
        state_path: str,
        adapter: ImageAdapter,
        release_client: ReleaseClient,
        deployment: DeploymentStateProvider,
    ) -> None:
        self._lock = asyncio.Lock()      # 进程内 destructive 互斥
        self._tasks: dict[str, asyncio.Task] = {}

    async def submit_update(self, target_version: str | None) -> str: ...
    async def check(self) -> dict: ...        # 同步只读
    async def preflight(self, target_version: str) -> dict: ...
    def get_job(self, job_id: str) -> JobState | None: ...
    def get_job_logs(self, job_id: str) -> list[dict]: ...
```

**`_run_update_job` 的 `CancelledError` 契约**（项 5，关键修正）：

```python
async def _run_update_job(self, job: JobState) -> None:
    try:
        # 状态机各步：update job.state + step → save_state → await adapter/deployment
        # checking → update_available → preflight → materialize_current_anchor → downloading → activating → restarting → health_checking
        # §9.5: preflight 成功后、downloading 前，若当前 ref 是 :latest 则 materialize 为 vX.Y.Z@sha256:<digest>
        job.state = "success"
        job.updated_at = _utcnow()
        save_state(...)                       # 持久化终态
        self._clear_active_job_id()           # success：清 gate（current_job terminal）
    except asyncio.CancelledError:
        # **不清 active_job_id，不设终态**
        # current_job 保持非 terminal + active_job_id 仍指向它
        # → 下次 daemon 启动 reconcile 第 5 条 invariant → failed + interrupted
        # （若清了 gate，则 active_job_id=null + current_job 非 terminal → 第 2 条 StateCorruptionError）
        save_state(...)                       # 持久化当前非 terminal 状态
        raise
    except Exception as e:
        job.state = "failed"
        job.error_code = classify_error(e)
        job.error = summarize(e)              # 不截断单条，结构化 stderr_lines
        job.updated_at = _utcnow()
        save_state(...)                       # 持久化终态
        self._clear_active_job_id()           # failed：清 gate（current_job terminal）
```

`_clear_active_job_id` 只在 current_job 已 terminal 时执行（success/failed 路径）；CancelledError 路径**不调用**。这保证 reconcile 永远走 interrupted recovery（第 5 条），不会误触 StateCorruptionError（第 2 条）。

**两层锁 + active_job_id gate**（spec §7.5）：
1. flock（Slice 3a）— 同 host 单 daemon 进程。
2. `asyncio.Lock`（Slice 4）— 同进程单 destructive task。
3. `active_job_id`（Slice 3a）— 跨崩溃 gate。

**崩溃恢复协调**：daemon 启动 Slice 3a `reconcile_interrupted_job` 先跑；非终态 active job → `failed + interrupted` + 清 gate。编排器在 reconcile **之后**初始化，启动时 active_job_id 必为 null。编排器**不自动续跑**中断 job（P0 无断点续传）；用户重提。`_tasks` 是进程内 volatile，daemon 重启丢失，但 job 终态已持久化，`/v1/jobs/{id}` 仍可读。

**结构化日志**（`updater/src/sakura_ai_updater/job_logs.py`）：每 job 内存环形缓冲；条目 `{ts, level, step, msg, error_code?, stderr_lines?}`；溢出折叠旧条目，单条不截断；`/v1/jobs/{id}/logs` 返回完整缓冲；终态 job 日志保留至 daemon 重启（P0 不持久化日志到磁盘）。

**ReleaseClient**（`updater/src/sakura_ai_updater/release_client.py`）：async，GitHub Releases API + 下载 `update-manifest.json`；标准库 `urllib` + `asyncio.to_thread`，不引入 httpx；复用 GitHub 公开 API header 约定（`Accept: application/vnd.github+json`、`X-GitHub-Api-Version: 2022-11-28`、User-Agent）；分页；网络失败保留上次结果。

#### 7.4.3 preflight 四 gate（项 7 / B5）

`preflight(target_version)` 的 `checks`：

| gate | 判定 | 失败时 |
|---|---|---|
| `manifest_found` | Release 含 `update-manifest.json` | `manifest_not_found` |
| `manifest_valid` | schema + version 一致 | `manifest_not_found` |
| `deployment_mode_image` | `DeploymentStateProvider.read_deploy_mode() == "image"` | `deployment_mode_not_image` |
| `protocol_compatible` | `manifest.updater.protocol_version == PROTOCOL_VERSION` | `preflight_failed` |
| `target_newer` | `target_version > current_version`（SemVer） | `target_not_newer` |
| `min_upgrade_from` | `current_version >= manifest.min_upgrade_from` | `min_upgrade_from_unsatisfied` |
| `image_manifest_exists` | `docker manifest inspect <target_image>` 成功 | `preflight_failed` |
| `disk_space_sufficient` | docker root 分区可用空间 >= 阈值 | `disk_space_insufficient` |
| `updater_asset_present` | manifest 声称的 `asset_linux_<arch>` 在 Release assets + SHA256SUMS 中 | `preflight_failed` |

前四 gate（mode/newer/min/disk）失败属**合法 preflight 结果**（200 envelope `can_update=false`）；manifest 缺失/非法属 422 硬错误。

**disk_space 实现**：`docker info --format '{{.DockerRootDir}}'`（async subprocess）取 docker root → `shutil.disk_usage(root).free`（`asyncio.to_thread`）→ 与阈值（配置化，默认 2 GiB，由 DaemonBackend 传入）比较。P0 不做精确镜像大小估算（YAGNI）。

### 7.5 IPC 动作端点契约（项 7 补全）

所有端点挂在 `create_app` 的 app 上，经 `app.state.orchestrator` 调用。成功走 envelope；错误不走 envelope。

#### `POST /v1/check`（同步只读）

`orchestrator.check()`：最新稳定 Release + manifest readiness。响应 200 envelope `data`：

```json
{
  "current_version": "3.0.0",
  "latest_version": "3.1.0",
  "update_available": true,
  "update_ready": true,
  "readiness": {
    "manifest_found": true, "manifest_valid": true, "image_pullable": true,
    "protocol_compatible": true, "min_upgrade_from_satisfied": true,
    "updater_asset_present": true, "sha256sums_present": true,
    "target_newer": true, "deployment_mode_image": true
  },
  "target": { "version": "3.1.0", "image": "ghcr.io/.../sakura-ai:v3.1.0", "channel": "stable" }
}
```

错误：502（GitHub 不可达且无缓存）；503（orchestrator 未就绪）。

#### `POST /v1/preflight`（同步只读 dry-run，项 7）

请求 body：`{"target_version": "3.1.0"}`。`orchestrator.preflight(target_version)`。响应 200 envelope `data`，`can_update` 为 true 或 false：
- `true`：所有 gate 通过。
- `false`：manifest 合法但某 gate 不通过（mode/newer/min/disk/protocol/image/asset），`checks` 标注失败项（正常响应，非错误）。

```json
{
  "can_update": true, "from_version": "3.0.0", "target_version": "3.1.0",
  "target_image": "ghcr.io/.../sakura-ai:v3.1.0",
  "checks": [
    {"name": "manifest_found", "passed": true},
    {"name": "manifest_valid", "passed": true},
    {"name": "deployment_mode_image", "passed": true},
    {"name": "protocol_compatible", "passed": true, "detail": "manifest=1 current=1"},
    {"name": "target_newer", "passed": true, "detail": "3.1.0 > 3.0.0"},
    {"name": "min_upgrade_from", "passed": true, "detail": "3.0.0 >= 0.0.0"},
    {"name": "image_manifest_exists", "passed": true},
    {"name": "disk_space_sufficient", "passed": true, "detail": "free=15GiB threshold=2GiB"},
    {"name": "updater_asset_present", "passed": true}
  ]
}
```

错误（无 envelope）：404 `{"error": "target_not_found"}`；422 `{"error": "manifest_invalid", ...}`（清单缺失/非法，无法判定）。

#### `POST /v1/update`（异步，创建 job）

请求 body：`{"target_version": "3.1.0"}`（可选，缺省 = check 最新可用稳定版）。先同步 preflight；通过后 `submit_update` 建 job、spawn 后台 task。响应 202 envelope `data`：

```json
{ "job_id": "upd_abc123", "state": "checking", "target_version": "3.1.0" }
```

错误（无 envelope）：
- 409 `{"error": "update_in_progress", "job_id": "upd_existing"}`。
- 422 `{"error": "preflight_failed", "checks": [...]}`（preflight 未过，不建 job）。
- 404 `{"error": "target_not_found"}`。
- 503 `{"error": "updater_not_ready"}`。

#### `POST /v1/rollback`（P0 不实现）

响应 501（无 envelope）：`{"error": "not_implemented"}`。

#### `GET /v1/jobs/{job_id}`

响应 200 envelope `data`：完整 `JobState` 序列化。错误：404 `{"error": "job_not_found"}`。

#### `GET /v1/jobs/{job_id}/logs`

响应 200 envelope `data`：`{job_id, logs: [...], truncated}`（环形缓冲溢出时 truncated=true，单条不截断）。错误：404 `{"error": "job_not_found"}`。

#### 通用

- 4xx/5xx 不走 envelope；body 恒 `{"error": "<snake_case_code>", ...}`；409 额外 `job_id`。
- `create_app(state_path, *, orchestrator=None)`：None 时动作端点返回 503 `updater_not_ready`，`/v1/status`/`/v1/health` 不受影响。

### 7.6 清单写入器工作流（项 4 重写）

基于 [release-on-pr-merge.yml](../../../.github/workflows/release-on-pr-merge.yml) 真实结构（§3.6）。

**新增 job**（非 reusable workflow_call，普通 job）：

```yaml
  publish-update-manifest:
    name: 发布更新清单
    needs:
      - generate-release
      - publish-updater-assets
      - publish-stable-image
    if: >-
      always() &&
      needs.generate-release.result == 'success' &&
      needs.publish-updater-assets.result == 'success' &&
      (
        needs.publish-stable-image.result == 'success' ||
        needs.publish-stable-image.result == 'skipped'
      )
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: 检出代码
        uses: actions/checkout@v7
        with:
          ref: main

      - name: 生成并上传 update-manifest.json
        env:
          VERSION: ${{ needs.generate-release.outputs.version }}
          RELEASE_ACTION: ${{ needs.generate-release.outputs.release_action }}
          GH_TOKEN: ${{ github.token }}
        shell: bash
        run: |
          set -euo pipefail
          # 1. 校验 VERSION SemVer
          [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid version: $VERSION" >&2; exit 1; }
          # 2. backend/__init__.py 版本一致
          actual=$(sed -nE 's/^__version__[^=]*=[^"]*"([0-9]+\.[0-9]+\.[0-9]+)".*/\1/p' backend/__init__.py | head -n1)
          [[ "$actual" == "$VERSION" ]] || { echo "version mismatch: workflow=$VERSION backend=$actual" >&2; exit 1; }
          # 3. updater PROTOCOL_VERSION
          proto=$(sed -nE 's/^PROTOCOL_VERSION[^=]*=[^0-9]*([0-9]+).*/\1/p' updater/src/sakura_ai_updater/__init__.py | head -n1)
          [[ -n "$proto" ]] || { echo "PROTOCOL_VERSION not found" >&2; exit 1; }
          # 4. 验证 vX.Y.Z 稳定镜像已推送（created 与 refresh 都必须存在）
          image="ghcr.io/sakura520222/sakura-ai:v${VERSION}"
          docker manifest inspect "$image" >/dev/null \
            || { echo "stable image not found: $image (release_action=$RELEASE_ACTION)" >&2; exit 1; }
          # 5. 构造 manifest（asset_linux_* 在 updater 对象内，冻结 §12.1）
          cat > update-manifest.json <<JSON
          {"schema_version":1,"version":"${VERSION}","channel":"stable","min_upgrade_from":"0.0.0",
           "image":"${image}",
           "updater":{"protocol_version":${proto},
                      "asset_linux_amd64":"sakura-ai-updater-linux-amd64",
                      "asset_linux_arm64":"sakura-ai-updater-linux-arm64"}}
          JSON
          # 6. jq schema 校验
          jq -e '.schema_version==1 and (.version|test("^[0-9]+\\.[0-9]+\\.[0-9]+$")) and .channel=="stable"
                 and (.min_upgrade_from|test("^[0-9]+\\.[0-9]+\\.[0-9]+$"))
                 and (.image|test("^ghcr\\.io/sakura520222/sakura-ai:v[0-9]+\\.[0-9]+\\.[0-9]+$"))
                 and (.updater.protocol_version|type=="number")
                 and .updater.asset_linux_amd64 and .updater.asset_linux_arm64' update-manifest.json >/dev/null
          # 7. 上传（单 release 所有者不变；manifest 用 upload --clobber，不 create）
          gh release view "v${VERSION}" >/dev/null
          gh release upload "v${VERSION}" update-manifest.json --clobber
```

**关键设计点**：
- `needs` 含三者：`generate-release`（取 outputs）、`publish-updater-assets`（须 success）、`publish-stable-image`（success 或 skipped 均可）。
- `if` 以 `always()` 开头：GitHub Actions 在 `needs` 中有 skipped job 时默认跳过下游，即使 `if` 表达式写了 `result == 'skipped'`；`always()` 覆盖此 propagation，使 manifest job 在 stable-image skipped（`release_action == 'updated'`）时仍执行。
- 版本 source：`needs.generate-release.outputs.version`（**非** `inputs.version`，本 workflow 非 `workflow_call`）。
- `publish-stable-image` refresh 时 skipped（`release_action == 'updated'`）；manifest job 仍跑，但内部 `docker manifest inspect` 校验 `vX.Y.Z` image 实际存在（首次 created 时推送；refresh 时复用）。created/updated 两种场景统一走同一存在性校验。
- `actions/checkout`（`ref: main`）必需：读取 `backend/__init__.py` 与 updater `PROTOCOL_VERSION`。
- manifest `image` 用 tag（D3），不嵌 digest——故不依赖 `publish-stable-image` 的 digest 输出。
- GHCR 公开镜像 `docker manifest inspect` 无需登录。

**release DAG 变更**：

```
generate-release
  ├── build-and-upload-assets (needs: generate-release)
  ├── publish-updater-assets   (needs: generate-release, build-and-upload-assets; reusable)
  ├── publish-stable-image     (needs: generate-release; if release_action=='created'; reusable)
  └── publish-update-manifest  (needs: generate-release, publish-updater-assets, publish-stable-image) ← 最后叶子
```

**契约测试放宽 + 新增**（[test_release_workflows.py](../../../tests/test_release_workflows.py)）：
- 删除「永不出现 update-manifest.json」断言（原第 160-161 行）。
- 新增：`publish-update-manifest` 作业存在。
- 新增：其 `needs` 含 `generate-release`、`publish-updater-assets`、`publish-stable-image`。
- 新增：`if` 以 `always()` 开头 + `publish-stable-image.result` 允许 success 或 skipped + `publish-updater-assets`/`generate-release` 仍要求 success。
- 新增：含 `actions/checkout` 步骤。
- 新增：`update-manifest.json` 仅出现在该作业 upload 步骤。
- 新增：manifest schema 断言（`asset_linux_*` 在 `updater` 对象内）。
- 保持：「单 release 所有者」断言（manifest 用 `gh release upload --clobber`，不 `create`/`edit`）。

### 7.7 Backend UpdaterClient 扩展

保留 `get_status()` None-folding 语义（navbar 周期性调）。action 方法抛结构化异常，route 层映射 HTTP。

**新增异常**（`backend/services/updater_client.py`）：

```python
class UpdaterUnavailableError(RuntimeError): ...   # UDS 不可达 → 503
class UpdaterProtocolError(RuntimeError): ...      # envelope 非法 / protocol 不兼容 → 502
class UpdaterActionError(RuntimeError):            # updater 4xx/5xx
    def __init__(self, status_code: int, body: dict): ...
```

**新增方法**（全 async）：

| 方法 | 端点 | 失败 |
|---|---|---|
| `check()` | `POST /v1/check` | `UpdaterUnavailableError` / `UpdaterProtocolError` / `UpdaterActionError` |
| `preflight(target_version)` | `POST /v1/preflight` | 同上 |
| `update(target_version)` | `POST /v1/update` | 同上；409 → `UpdaterActionError(409, {error, job_id})` |
| `get_job(job_id)` | `GET /v1/jobs/{id}` | 同上；404 → `UpdaterActionError(404, ...)` |
| `get_job_logs(job_id)` | `GET /v1/jobs/{id}/logs` | 同上 |

内部 `_request(method, path, json_body=None)` helper：成功校验 envelope；失败按状态码/异常抛对应类型。`get_status()` 保留独立实现（None-folding）。

### 7.8 Backend version 路由扩展

**现有契约保留**：`/version/info`（所有登录用户）、`/version/releases`（super_admin）、`/version/check`（super_admin + CSRF，**仍 Backend discovery**）、`/version/manager`（super_admin）。

**`build_version_info()` 扩展**：新增只读字段（来自现有 `get_status()` 的 `data`，不新增 UDS 请求）：`updater_state`、`has_active_job`、`active_job_id`、`updater_deployment`。`update_supported` 从硬编码 `False` 改派生（image + 连接 + protocol 兼容）。**不**改 `update_available` 语义（§3.5）。

**新增 proxy 路由**（super_admin；写加 CSRF）：

| Backend route | 方法 | 转发 | 错误映射 |
|---|---|---|---|
| `/version/readiness` | POST | `/v1/check` | unavailable→503, protocol→502, action→透传 |
| `/version/preflight` | POST | `/v1/preflight` | 同上；422 透传 |
| `/version/update` | POST | `/v1/update` | 同上；409 透传 `{error, job_id}` |
| `/version/jobs/{job_id}` | GET | `/v1/jobs/{id}` | unavailable→503, 404→404 |
| `/version/jobs/{job_id}/logs` | GET | `/v1/jobs/{id}/logs` | 同上 |

**参数校验**：`target_version` 严格 SemVer（复用 `update_checker._parse_semver`）；非法 → 422。不接受任意 command/path/body 透传。

**审计**（`backend/webui/helpers/admin_log.py`）：`/version/update` 成功后 `log_admin_action`：`admin_id`、`action="update_apply"`、`target_version`、`job_id`、`deployment_mode`。审计失败不回滚 updater job。详情不含 token/UDS body/敏感配置。

**错误映射**：

| Updater | Backend HTTP |
|---|---|
| UDS 不可达 | 503 `{"error": "updater_unavailable"}` |
| envelope 非法/protocol 不兼容 | 502 `{"error": "updater_protocol_error"}` |
| 409 | 409 原样 `{error, job_id}` |
| 4xx（404/422） | 原状态码 + body 透传 |
| 5xx | 502 `{"error": "updater_internal_error"}` |

### 7.9 WebUI 版本管理器

增量扩展 [version_manager.html](../../../backend/webui/templates/version_manager.html)：

1. 当前部署卡：updater state 徽章（`idle`/`update_available`/`downloading`/`health_checking`/...）、active job 指示。
2. target 区域：`/version/readiness` 的 `target` + `readiness`；`update_ready=true` 启用 update 按钮。
3. update 按钮：POST `/version/update`（CSRF header）→ `job_id` 写 `sessionStorage` → job 轮询。
4. job 轮询：`setInterval` `/version/jobs/{id}`，展示 state/step；终态停；`/version/jobs/{id}/logs` 展开（折叠展示，不截断）。
5. Web 重启 reconnect：网络错误 → `reconnecting` 态（不报 failed）；重连后继续轮询同 `job_id`（`sessionStorage`）；终态才退出。
6. 安全：所有请求经 Backend route，不直连 UDS/宿主 URL；target_version 显式 JSON 字段；Release notes 管线不变。

navbar（[components/navbar.html](../../../backend/webui/templates/components/navbar.html)）：微调可选（展示 `updater_state`），不阻塞。

---

## 8. 任务分解

依赖图（v2，S4-2 独立 CI 分支不汇入 updater 主链）：

```
S4-0 (spec 漂移修正，含 update_available 补回)
  │
  ▼
S4-1 (SemVer + Manifest v1，asset 在 updater 内)
  │
  ├────────────────────► S4-2 (清单写入器 workflow)   [CI 域独立分支]
  │
  ▼
S4-3 (ImageAdapter，全 async + pull/activate 拆分)
  │
  ▼
S4-4 (JobOrchestrator + DeploymentStateProvider + preflight gates + CancelledError 契约)
  │
  ▼
S4-5 (IPC 动作端点)
  │
  ▼
S4-6 (serve 注入 + daemon args + DeploymentState 构造)
  │
  ▼
S4-7 (Backend client action)
  │
  ▼
S4-8 (Backend routes + info)
  │
  ▼
S4-9 (WebUI 版本管理器)
```

每 Task：TDD（先红后绿）+ ruff + 中英双语注释 + 英文代码。

### Task S4-0 — 契约对齐与 spec 漂移修正

- 修正 [2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md)：§8.1 状态小写 **+ 补回 `update_available`**（完整枚举见本计划 §7.3）；§9 ImageAdapter 改 async argv 直调 compose（D2/D3）；§11 `daemon-meta.json`；§16.1 文件树标注 Slice 4 交付。
- 不改实现代码。

**验收**：spec 与 Slice 3a/3b/3c 代码 0 漂移；§8.1 状态枚举含 `update_available`；与本计划 §5/§7 一致。

### Task S4-1 — SemVer + Manifest v1 解析器

- `updater/src/sakura_ai_updater/semver.py`：`parse_semver`/`is_newer_version`（严格，与 backend 对齐）。
- `updater/src/sakura_ai_updater/manifest.py`：`Manifest` + `parse_manifest(data, *, expected_version)` + `ManifestError`。**`asset_linux_*` 从 `updater` 对象内读**（项 1）。
- `updater/tests/test_semver.py`、`updater/tests/test_manifest.py`。

**验收**：合法 manifest 解析；`asset_linux_*` 在顶层（而非 updater 内）→ `ManifestError`；缺字段/类型错/SemVer 非法/version 不符 → `ManifestError`；semver 与 backend 行为对齐。

### Task S4-2 — 清单写入器工作流 + 契约测试放宽（项 4）

- `release-on-pr-merge.yml` 新增 `publish-update-manifest`（§7.6 完整契约）。
- 放宽 + 新增 [test_release_workflows.py](../../../tests/test_release_workflows.py) 断言（§7.6）。

**验收**：
- workflow 静态契约：作业存在；`needs` 含三者；`if` 以 `always()` 开头且 stable-image 允许 skipped；含 checkout；清单仅该作业上传。
- manifest schema 断言：`asset_linux_*` 在 `updater` 内。
- 单 release 所有者保持。
- `update-manifest.json` 不在其他作业/source 归档。
- manifest JSON 经 jq 自校验。

### Task S4-3 — ImageAdapter（全 async，项 2/3）

- `updater/src/sakura_ai_updater/adapters/__init__.py`、`image.py`。
- 全 async：`create_subprocess_exec`（compose/pull/inspect）+ 标准库 socket/to_thread（health）。
- pull/activate 拆分（项 3）：pull 只 `docker pull`；activate 写 env + `compose --env-file -f up -d`。
- 不引入 httpx 生产依赖。
- `updater/tests/test_image_adapter.py`：monkeypatch 子进程 + 标准库 HTTP；覆盖各成功/失败路径、pull 不改 env、activate 改 env、health 版本门控（200 但 version 不符 / 超时）。

**验收**：
- 全部子进程 `shell=False`，argv 无用户控制值。
- pull 失败 → deployment.env 未改（测试断言文件内容不变）。
- activate 成功 → deployment.env `SAKURA_AI_IMAGE` 替换/追加；compose 命令含 `--env-file` 与 `-f`。
- health 门控 `response.version == target_version`（B3）。
- 所有 I/O 非阻塞（无 `subprocess.run`/同步 `urllib` 裸调；测试可用 `pytest-asyncio` 验证不阻塞 event loop）。

### Task S4-4 — JobOrchestrator + DeploymentStateProvider + preflight gates（项 5/6/7）

- `updater/src/sakura_ai_updater/deployment.py`：`DeploymentStateProvider`（§7.4.1）。
- `updater/src/sakura_ai_updater/release_client.py`：async GitHub Release + manifest 下载（标准库 + to_thread）。
- `updater/src/sakura_ai_updater/jobs.py`：`JobOrchestrator`、`UpdateInProgressError`。
- `updater/src/sakura_ai_updater/job_logs.py`：环形缓冲。
- `updater/tests/test_deployment.py`、`test_jobs.py`、`test_release_client.py`、`test_job_logs.py`。

**验收（关键契约）**：
- **CancelledError gate**（项 5）：模拟 `asyncio.CancelledError` → `active_job_id` **未清** + `current_job` 非 terminal；随后 `reconcile_interrupted_job` 走第 5 条 invariant → `failed+interrupted`（**不**触发第 2 条 StateCorruptionError）。cancellation regression test 必备。
- success/failed 路径：`active_job_id` 清除，`current_job` terminal。
- 状态转换严格匹配 §7.3 图（含 `update_available`）。
- **DeploymentStateProvider**（项 6）：`read_image_ref` 从 deployment.env；`resolve_current_version` 对 vX.Y.Z tag 从 tag 解析、对 `:latest` 查 `/health.version`；`capture_from_digest` 用 `docker inspect --format='{{.Image}}' sakura-ai`（容器 inspect）。不缓存（每次现读）。
- **`:latest` materialization**（§9.5）：`materialize_current_anchor` 在 ref=`:latest` 时改写 deployment.env 为 `vX.Y.Z@sha256:<digest>`；ref 已 concrete 则跳过。**仅在 destructive job 内调**，read-only 端点不改 env。regression：`:latest` + health.version=3.0.0 + digest=sha256:abc → env 改为 `...:v3.0.0@sha256:abc`；`:v3.0.0` → 不改。
- **preflight 四 gate**（项 7）：mode/newer/min/disk 失败 → `can_update=false`（200）；manifest 缺失/非法 → 422。
- disk_space：`docker info` 取 root + `shutil.disk_usage` + 阈值。
- asyncio.Lock 互斥（并发 submit 第二个立即拒）。
- 同时只一个 destructive update（三层锁）。

### Task S4-5 — IPC 动作端点

- 扩展 [ipc.py](../../../updater/src/sakura_ai_updater/ipc.py)：六端点（§7.5）。
- `create_app(state_path, *, orchestrator=None)`。
- `_error_response(status, code, **extra)` helper。
- `updater/tests/test_ipc_actions.py`。

**验收**：成功走 envelope；错误不走；409 `{error, job_id}`；404 一致；`/v1/rollback` 恒 501；orchestrator=None 时动作端点 503、status/health 不受影响；状态值全小写含 `update_available`。

### Task S4-6 — serve 注入 + daemon args + DeploymentState 构造（项 6）

- 扩展 [__main__.py](../../../updater/src/sakura_ai_updater/__main__.py) `serve()`：新增 `compose_file`/`deployment_env`/`health_url`/`disk_space_threshold` 参数；构造 `ImageAdapter` + `DeploymentStateProvider` + `JobOrchestrator`，注入 `create_app`。
- 扩展 [daemon.py](../../../updater/src/sakura_ai_updater/backends/daemon.py) `DaemonBackend.__init__` + `_serve_args()`：追加 `--compose-file`/`--deployment-env`。
- CLI parser 追加对应 flag。
- 测试：`test_main_serve_injection.py`、`test_daemon_serve_args.py`。

**验收**：serve 构造 adapter+deployment+orchestrator 并注入；`_serve_args()` 含两新参数，顺序稳定，既有参数不变；Windows dev import 不破坏。

### Task S4-7 — Backend UpdaterClient action 扩展

- 扩展 [updater_client.py](../../../backend/services/updater_client.py)：三异常 + 五方法 + `_request` helper（§7.7）。
- `get_status()` 不变。
- `tests/test_updater_client_actions.py`。

**验收**：`get_status()` 既有测试不回归；action 失败不折叠 None；409 body 完整透传；malformed JSON 不当成功；protocol 不兼容 → `UpdaterProtocolError`；unavailable → `UpdaterUnavailableError`。

### Task S4-8 — Backend version 路由 + 信息扩展

- 扩展 [version.py](../../../backend/webui/routes/version.py)：`build_version_info()` 新字段 + `update_supported` 派生；新增五 proxy 路由（§7.8）。
- `/version/check` 语义不变。
- 审计 + CSRF + 参数校验。
- 扩展 `tests/test_version_info.py`；新增 `tests/test_version_routes.py`。

**验收**：现有路由不回归；新路由 super_admin + 写路由 CSRF；错误映射 §7.8；`update_available`/`update_supported`/`update_ready` 独立；审计字段完整，失败不影响 job。

### Task S4-9 — WebUI 版本管理器

- 扩展 [version_manager.html](../../../backend/webui/templates/version_manager.html)：徽章/target/update/轮询/reconnect/logs（§7.9）。
- 可选 navbar 微调。
- `tests/test_version_manager_template.py`。

**验收**：update 期间 Web 重启 → reconnect；请求经 Backend route；Release notes 管线不变；无 emoji。

---

## 9. 测试策略

### 9.1 单元测试

| 模块 | 文件 |
|---|---|
| SemVer | `updater/tests/test_semver.py` |
| Manifest v1（asset 在 updater 内） | `updater/tests/test_manifest.py` |
| ImageAdapter（async） | `updater/tests/test_image_adapter.py` |
| DeploymentStateProvider | `updater/tests/test_deployment.py` |
| ReleaseClient | `updater/tests/test_release_client.py` |
| JobOrchestrator + job_logs | `updater/tests/test_jobs.py`、`test_job_logs.py` |
| IPC 动作端点 | `updater/tests/test_ipc_actions.py` |
| serve 注入 + daemon args | `updater/tests/test_main_serve_injection.py`、`test_daemon_serve_args.py` |
| UpdaterClient action | `tests/test_updater_client_actions.py` |
| version 路由 + 信息 | `tests/test_version_routes.py`、`tests/test_version_info.py`（扩展） |
| 版本管理器模板 | `tests/test_version_manager_template.py` |
| 发布工作流契约 | `tests/test_release_workflows.py`（放宽 + 新断言） |

### 9.2 关键 regression 测试

- **CancelledError gate**（项 5）：`test_jobs.py` 必须有显式用例：注入 `CancelledError` → 验证 state 文件 `active_job_id` 非 null + `current_job` 非 terminal → 调 `reconcile_interrupted_job` → 得 `failed+interrupted`（而非 `StateCorruptionError`）。
- **pull 不改 deployment.env**（项 3）：`test_image_adapter.py` 断言 pull 失败前后 deployment.env 文件字节一致。
- **`:latest` materialization**（§9.5/项 6）：`test_deployment.py` 验证 ref=`:latest` + health.version=3.0.0 + digest=sha256:abc → deployment.env 改为 `...:v3.0.0@sha256:abc`；ref 已 `:v3.0.0` → 不改。
- **preflight 四 gate**（项 7）：`test_jobs.py` 每 gate 一个失败用例（mode/newer/min/disk），验证 `can_update=false` 且对应 `checks` 项失败。
- **manifest schema**（项 1）：`test_manifest.py` 断言 `asset_linux_*` 在顶层时抛 `ManifestError`。
- **release DAG**（项 4）：`test_release_workflows.py` 断言 `publish-update-manifest` 的 `needs`/`if`/`checkout`/image verify。

### 9.3 集成边界

- UDS 集成：复用 `updater/tests/test_ipc.py` pre-bound UDS 模式，加动作端点集成（orchestrator + 真 FastAPI，adapter mock）。
- compose 集成：ImageAdapter 的 `docker`/`compose` 调用 CI 用 mock（不依赖真 Docker）；本地 e2e 手动（§9.4）。
- Backend↔updater：route 测试用 fake UDS server（httpx transport mock）校验错误映射。

### 9.4 E2E（P0 手动）

1. 本地构建目标镜像 tag `:v3.0.1-test`。
2. dev 模式 updater（`SAKURA_UPDATER_DEV=1`）+ 假 release（本地 manifest server）。
3. WebUI 触发 update → 验证 `checking → update_available → preflight → downloading → activating → restarting → health_checking → success`。
4. 模拟 health 版本不符（故意打错 tag）→ `failed + health_check_version_mismatch` + `from_digest` 记录。
5. 模拟 daemon 重启（update 中 kill updater）→ `reconcile` → `failed + interrupted`，`active_job_id` 清除，可重提。
6. 模拟 downgrade（target < current）→ preflight `target_not_newer`，`can_update=false`，`/v1/update` 422。

E2E 自动化列为 P1 候选，不阻塞 Slice 4 验收。

### 9.5 不变性测试

- compose 安全边界（`tests/test_compose_updater_mount.py`）：仍不挂 docker.sock。
- release 单所有者、DAG、资产完整性：放宽后保持绿。
- updater 包 import 不依赖 backend。
- updater 生产依赖不新增（`pyproject.toml` 仍只 fastapi/uvicorn/pydantic）。

---

## 10. 验收标准

1. **功能闭环**：镜像模式 super_admin 经 WebUI 完成 check → preflight（四 gate）→ update → 健康校验 → 成功，版本升级。
2. **失败诊断**：各阶段失败产生正确 `error_code` + 结构化日志；preflight/pull 失败时运行容器与 deployment.env 不受影响。
3. **pull/activate 分离**（项 3）：pull 失败不改 deployment.env；activate 才写。
4. **CancelledError 安全**（项 5）：update 中 daemon 被杀 → 重启 reconcile → `failed+interrupted`（非 StateCorruptionError），可重提。
5. **并发安全**：同时只一个 destructive update；第二个 409 + 现有 job_id。
6. **preflight gate**（项 7）：mode/newer/min/disk 失败正确分类；downgrade 被阻。
7. **Web 重启 reconnect**：update 期间 Web 重建 → reconnect → 继续轮询同 job → 终态正确。
8. **from_digest + `:latest` materialization**（项 6/B2/§9.5）：activate 前捕获运行容器 immutable digest；destructive job preflight 后若 ref 是 `:latest` 则原子具体化为 `vX.Y.Z@sha256:<digest>`，reboot 不再漂移。
9. **async 非阻塞**（项 2）：update 进行中 `/v1/status`/`/v1/jobs/{id}` 响应及时（event loop 不被 docker 子进程阻塞）。
10. **安全不变量**：Web 容器不挂 docker.sock；`shell=False`；用户值不进 argv；浏览器不直连 UDS。
11. **清单发布**（项 4）：流水线产 `update-manifest.json`（asset 在 updater 内）；DAG 正确；created/updated 两场景镜像存在性都校验。
12. **回归**：`python -m pytest -q`（根递归，含 updater/tests，执行前 `pip install -e './updater[dev]'`）全绿；`ruff check .` 绿（Windows ruff 权限跳过已知，CI 兜底）。
13. **文档**：spec 漂移修正（S4-0，含 `update_available`）；两个 README 如涉及则更新。
14. **依赖不膨胀**：updater 生产依赖不变。
15. **非目标守住**：source/systemd/自更新/digest pinning/签名/自动回滚/prerelease 均未进入。

---

## 11. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| event loop 被 docker 子进程阻塞（项 2） | 中 | 高 | 全 `create_subprocess_exec` + `to_thread`；`pytest-asyncio` 验证非阻塞 |
| `CancelledError` 误清 gate 致 StateCorruptionError（项 5） | 中 | 高 | success/failed 才清；CancelledError re-raise；regression test |
| pull 改 env 致失败后 reboot 用新版本（项 3） | 中 | 高 | pull/activate 拆分；pull 不改 env；测试断言文件不变 |
| compose interpolation 用错（env_file vs --env-file）（项 3） | 中 | 中 | activate 明确 `--env-file <env> -f <file>`；测试断言 argv |
| `publish-stable-image` skipped 致 manifest 被 skip（项 4） | 中 | 中 | `if` 显式处理 skipped + 内部 image 存在性校验 |
| `:latest` 首次接管无 digest 锚点（§9.5/D3） | 低 | 高 | `capture_from_digest` 容器 inspect 始终捕获 |
| downgrade 误执行（项 7） | 中 | 中 | `target_newer` gate；失败 → `can_update=false` → 422 |
| daemon 重启后 `_tasks` 丢失 | 中 | 中 | 设计如此：reconcile 标 failed+interrupted，用户重提（P0 无断点续传） |
| updater 包误引 httpx 生产依赖（项 2） | 低 | 低 | pyproject 不动；标准库 + to_thread；CI 可加 import 检查 |
| manifest schema 演进 | 低 | 低 | `schema_version` 严格校验；未知 → 拒 |

---

## 12. 实现顺序

见 §8 依赖图。S4-2 是 CI 域独立分支（S4-1 之后），不汇入 updater 主链。S4-4 起 updater 域串行；S4-7 起 backend 域串行；S4-9 最后。

每 Task 完成后：TDD 绿 + ruff 绿 + 不回归。Task 间不自主提交（项目规范）。

---

## 附录 A — 新增文件清单

```
updater/src/sakura_ai_updater/
  semver.py                       (S4-1)
  manifest.py                     (S4-1)
  adapters/__init__.py            (S4-3)
  adapters/image.py               (S4-3)
  deployment.py                   (S4-4, DeploymentStateProvider)
  release_client.py               (S4-4)
  jobs.py                         (S4-4)
  job_logs.py                     (S4-4)
updater/tests/
  test_semver.py                  (S4-1)
  test_manifest.py                (S4-1)
  test_image_adapter.py           (S4-3)
  test_deployment.py              (S4-4)
  test_release_client.py          (S4-4)
  test_jobs.py                    (S4-4)
  test_job_logs.py                (S4-4)
  test_ipc_actions.py             (S4-5)
  test_main_serve_injection.py    (S4-6)
  test_daemon_serve_args.py       (S4-6)
tests/
  test_updater_client_actions.py  (S4-7)
  test_version_routes.py          (S4-8)
  test_version_manager_template.py (S4-9)
```

## 附录 B — 修改文件清单

```
docs/superpowers/specs/2026-08-07-auto-update-design.md  (S4-0: 漂移修正 + update_available)
updater/src/sakura_ai_updater/ipc.py                      (S4-5: 动作端点)
updater/src/sakura_ai_updater/__main__.py                 (S4-6: serve 注入 + CLI)
updater/src/sakura_ai_updater/backends/daemon.py          (S4-6: serve args)
backend/services/updater_client.py                        (S4-7: action 扩展)
backend/webui/routes/version.py                           (S4-8: 路由 + info)
backend/webui/templates/version_manager.html              (S4-9: UI 闭环)
backend/webui/templates/components/navbar.html            (S4-9: 可选微调)
.github/workflows/release-on-pr-merge.yml                 (S4-2: manifest 作业)
tests/test_release_workflows.py                           (S4-2: 契约放宽 + 新断言)
tests/test_version_info.py                                (S4-8: 扩展)
```

---

## 协作约束（本切片全程适用）

- 执行者/子代理不得自主提交。
- S4-0 之前不修改实现代码；S4-0 只改 spec 文档。
- 任一 Task 与冻结 Slice 3a/3b/3c 契约冲突时，列阻塞项上报，不改冻结契约。
- 中文交流，中英双语注释，英文代码，Conventional Commits。
- 不用 emoji；不截断文本（折叠/展开）；无硬编码限制（配置化）。
- updater 不引入新生产依赖（项 2）。
