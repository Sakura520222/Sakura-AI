# Agent OS 级沙箱设计

日期：2026-08-25
状态：审查修复与 WebUI 动态网络策略实现中；真实 Linux Docker 运行时质量门待执行
关联 Issue：[GitHub Issue #528](https://github.com/Sakura520222/Sakura-AI/issues/528)
实施计划：[2026-08-25-agent-os-sandbox-implementation.md](../plans/2026-08-25-agent-os-sandbox-implementation.md)

## 1. 决策摘要

Agent 生成或影响的命令不得继续由 Sakura AI Web 进程直接执行。Docker 部署采用由宿主 `start.sh` 独立管理的专用 `sakura-ai-sandboxd` 容器，通过专用 Unix Domain Socket 接收强类型执行请求，并为每次请求创建一个一次性 OCI 容器。只有 sandboxd 容器持有 Docker socket；它不属于 Web Compose service。runner 默认不允许原始命令出网、非 root、只读根文件系统、无 Linux capabilities、启用 `no-new-privileges`，只读或读写挂载当前任务工作区，并受 CPU、内存、进程数、文件描述符、输出量与总时长限制。超级管理员可在 WebUI 将 Agent 网络策略切换为“隔离”“仅受控 Web 工具”或“完全访问”；策略从下一次工具调用开始生效，不要求重启 Web 或 sandboxd。

Web 容器永远不挂载 Docker socket；sandboxd 与 Host Updater 也不得共用 socket、协议、进程、状态目录或权限组。两者可以复用实现模式，但安全职责必须独立。

本设计把执行路径分成三个明确的信任域：

| 执行域 | 典型操作 | 执行位置 | 规则 |
| --- | --- | --- | --- |
| `TRUSTED_CONTROL` | clone/fetch/worktree、提交、推送、创建 PR | Web 后端的受控服务 | 只接受应用构造的 argv；不得运行模型文本；凭据按单次调用注入 |
| `UNTRUSTED_AGENT` | ShellTool、GrepTool 的外部命令、构建、测试 | sandboxd 一次性容器 | 默认不允许原始命令出网；管理员可热切换到服务端固定 egress；最小环境；严格资源与挂载限制；不可回退裸执行 |
| `UNTRUSTED_DEPENDENCY` | 仓库依赖安装、构建脚本 | sandboxd 一次性容器 | 与 Agent 命令相同隔离；仅“完全访问”策略允许在线安装，否则只消费镜像内依赖或显式缓存 |
| `TRUSTED_WEB_TOOL` | `search_web`、`fetch_url` | Web 后端受控工具 Adapter | 不进入 runner；复用提供商配置、SSRF/域名/大小/重定向限制；由 Agent 网络策略与工具开关共同授权 |

## 2. 目标与非目标

### 2.1 目标

1. OS/容器边界约束不可信命令的文件系统、网络、进程、身份、资源与生命周期。
2. Agent 命令看不到 Sakura AI Web 进程的 API Key、数据库口令、GitHub token、Redis 地址等环境变量。
3. 只让命令访问当前任务工作区；不暴露应用源码、其他任务、运行时 socket、Docker socket和部署配置。
4. sandboxd 缺失、版本不兼容、策略无效或清理失败时 fail closed，不静默切换回本地执行。
5. 保留源码开发模式的显式本地执行能力，但必须由管理员选择，且不得成为 Docker 生产模式的隐式降级路径。
6. 尽量保持 ShellTool 现有输入输出合同、事件流与取消体验，降低 Agent 层改造范围。

### 2.2 非目标

- 不把命令黑名单描述为安全边界；黑名单只保留少量产品策略和误操作提示。
- 不在 Web 容器内运行 bubblewrap/nsjail 作为 Docker 部署的默认方案。
- 不给业务容器挂载 `/var/run/docker.sock`，不使用 `privileged: true`。
- 首版不承诺 Windows/macOS 原生 OS 沙箱；这些平台使用显式开发模式或拒绝启用。
- 不尝试把“完全访问”伪装成安全的受限网络：管理员选择该策略即明确接受不可信命令可访问公网、局域网、宿主网关和云 metadata 的风险；文件系统、身份、capability、资源与 socket 隔离仍继续强制。
- 沙箱不防御宿主机内核或 OCI runtime 的零日漏洞；高风险多租户应叠加 gVisor 或 microVM。

## 3. 威胁模型

### 3.1 被保护资产

- Web 进程环境中的 AI/GitHub/数据库/Redis/Telegram 凭据。
- Docker socket、updater/sandboxd UDS、宿主机文件系统与其他容器。
- Sakura AI 应用源码、部署状态、日志、数据库数据和其他 Agent 任务工作区。
- 宿主机 CPU、内存、PID、磁盘空间和网络可用性。

### 3.2 不可信输入

- 模型生成的 shell 命令、argv、路径、环境变量和工作目录。
- 被分析仓库中的源码、测试、构建脚本、包管理钩子和二进制文件。
- `pip install -e .`、`npm install` 等会执行仓库脚本的依赖操作。

### 3.3 必须阻断的路径

1. 命令读取宿主或 Web 容器秘密。
2. `../`、绝对路径、软链接或挂载技巧逃出任务工作区。
3. 访问 Docker/updater/sandboxd socket。
4. 在 `offline` / `web_tools` 策略下访问互联网、MySQL、Redis、云 metadata、宿主机管理端口或其他容器网络；在 `full_access` 下不得把网络授权扩大为 Docker socket、宿主网络 namespace 或新增挂载权限。
5. fork bomb、内存/CPU/输出洪泛、超时后残留进程或容器。
6. 通过容器名、mount source、runtime、网络名等字段注入宿主机 Docker 参数。
7. sandboxd 不可用时由调用方自动退回 `create_subprocess_shell`。

## 4. 架构与数据流

```text
Agent / Tool loop
      |
      | ShellExecutionRequest
      v
SandboxRunner (backend)
      |
      | HTTP over /run/sakura-ai-sandbox/sandboxd.sock
      v
sakura-ai-sandboxd (host, independent daemon)
      |
      | validated, server-owned docker argv
      v
one-shot runner container
      |
      +-- /workspace   exact task workspace, rw
      +-- /tmp         bounded tmpfs
      +-- /home/agent  bounded tmpfs
      +-- no Web env / no app mounts / no sockets
```

请求生命周期：

1. Backend 根据任务记录解析工作区标识和相对工作目录，不接受模型提供宿主机路径。
2. `SandboxRunner` 生成 request ID，并从 `app_config` 新鲜读取超级管理员选择的网络策略。模型与 Tool 参数不包含网络字段。
3. Backend 将策略解析为协议枚举 `none` / `egress`；sandboxd 校验协议、字段长度、工作区标识、相对路径、命令大小、超时、profile 和网络枚举，并把 `egress` 映射到服务端固定网络。
4. sandboxd 自己构造完整容器命令，创建带管理 label 的唯一一次性容器。
5. sandboxd 并发读取 stdout/stderr，按字节上限截断；超时或取消时 kill，随后强制 remove。
6. sandboxd 返回结构化结果；清理不能确认时返回基础设施错误而不是成功。
7. Backend 映射为现有 ToolResult/事件，不进行本地重试。

## 5. 信任边界 ADR

### ADR-1：sandboxd 独立于 Host Updater

两者虽然都使用宿主守护进程和 HTTP over UDS，但权限语义完全不同：Updater 接受少量部署动作，sandboxd 高频执行不可信负载。必须使用不同二进制入口、进程、socket、运行目录、固定 GID、协议模型、日志、锁与生命周期命令。任一 daemon 被攻破不得直接获得另一 daemon 的 API 权限。

### ADR-2：业务容器不持有容器运行时控制权

Docker/Podman CLI 和 socket 只在宿主 sandboxd 可见。Web 后端只能提交受约束的执行请求，不能指定镜像、挂载源、网络、capability、security-opt、runtime 或容器名。

### ADR-3：一次请求一个容器

不复用长生命周期 runner。一次性容器可清晰撤销工作区挂载、环境和进程树，避免不同任务间状态与秘密串扰。性能通过预拉镜像和宿主镜像缓存优化，不通过放宽隔离优化。

### ADR-4：Docker 生产模式 fail closed

当 `agent_team_execution_backend=sandbox` 时，连接失败、协议不兼容、镜像缺失、策略无效或运行时错误都直接终止该工具调用。仅 `local` 后端可以调用本机 subprocess；该值只用于源码开发，并在生产镜像部署中被配置校验拒绝。

### ADR-5：Git 凭据与不可信工作区分离

GitHub App token 不得写进 clone URL 或 `.git/config`。受信任 Git 控制面通过临时 askpass/一次性 header 等方式只给单次 Git 子进程提供凭据，结束后移除临时文件并清空变量。沙箱容器永远不接收这些凭据。

### ADR-6：依赖安装属于不可信代码执行

仓库声明的构建 backend、setup hook、npm lifecycle 等均可执行代码，所以自动依赖安装必须使用 `UNTRUSTED_DEPENDENCY` profile。若首版离线策略不能满足依赖获取，应明确跳过并报告，而不是在 Web 进程中执行。

### ADR-7：网络策略由管理员热配置，网络实现仍由 sandboxd 固定

安全默认值不能成为自部署用户无法退出的产品限制。全局配置页提供 `offline`、`web_tools`、`full_access` 三档 Agent 网络策略，只有超级管理员可以修改。Backend 在每次工具执行前读取最新持久化值；配置保存后，下一次工具调用和下一次一次性 runner 容器立即使用新策略，已经创建的容器不被中途改网。

WebUI、模型和普通执行请求都不能提交 Docker 网络名。Backend 到 sandboxd 的深模块接口只包含 `none` / `egress` 枚举；sandboxd 将 `egress` 映射为启动时固定的网络（默认 Docker `bridge`，自部署管理员可在宿主部署配置中改为专用 named network）。`host`、`container:*`、`ns:*` 和任意 Docker argv 仍不进入请求接口。选择 `full_access` 是管理员对广泛网络访问的明确授权，不会解除非 root、只读根文件系统、capability、资源、工作区和 socket 隔离。

## 6. Backend 执行器合同

### 6.1 统一请求与结果

Backend 内部采用与具体后端无关的模型：

```python
class ExecutionProfile(StrEnum):
    AGENT = "agent"
    DEPENDENCY = "dependency"

class ExecutionRequest:
    command: str | None
    argv: tuple[str, ...] | None
    workspace_key: str
    cwd: PurePosixPath
    profile: ExecutionProfile
    timeout_seconds: int

class ExecutionResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    cancelled: bool
    output_truncated: bool
    infrastructure_error: str | None
```

`command` 与 `argv` 必须且只能提供一个。shell 模式在 runner 镜像内固定用 `/bin/bash --noprofile --norc -lc`；调用者不能更换 shell。argv 模式不经过 shell。Backend 的公共 `ExecutionRequest` 不暴露网络参数；只有 `SandboxExecutionRunner` 在序列化 UDS 请求时，根据新鲜读取的管理员策略附加 `network_mode=none|egress`。

### 6.2 环境合同

禁止 `os.environ.copy()`。sandboxd 只设置服务端常量：

```text
HOME=/home/agent
PATH=/workspace/.venv/sandbox/bin:<runner image fixed path>
VIRTUAL_ENV=/workspace/.venv/sandbox
LANG=C.UTF-8
LC_ALL=C.UTF-8
CI=true
TERM=dumb
```

允许 Backend 传递的环境键首版为空。未来新增变量必须同时加入协议 allowlist、威胁模型和泄漏测试。任何未知键都拒绝整个请求。

### 6.3 工作区合同

- `workspace_key` 只接受任务 ID 派生的固定格式，不接受路径分隔符。
- `cwd` 必须是相对 POSIX 路径；拒绝空字节、绝对路径和 `..`。
- sandboxd 配置唯一权威工作区根或 Docker volume；请求不能覆盖它。
- 容器只看到 `/workspace`，且工作目录固定为 `/workspace/<cwd>`。
- 容器内不得出现 `/app`、`/run/sakura-ai`、`/run/sakura-ai-sandbox` 或宿主 Docker socket 挂载。
- sandboxd 在 handoff 前对任务树执行不跟随 symlink/reparse 的全树预检，拒绝后代链接、特殊节点、硬链接和嵌套挂载，并以 no-follow fd 重新校验 inode 后再逐节点变更；handoff 在 venv 创建前只执行一次，合法 venv symlink 不会被误判。不能完成合同时拒绝启动，绝不通过 `0777` 或未验证 ACL 放宽权限。
- linked worktree 的 `.git` 指针不直接作为容器路径使用：base checkout 的 common `.git` 以只读 `/sakura-git/common` 挂载，当前 task 的 `.git/worktrees/<name>` 元数据再由服务端按已验证的 task 名称以可写子 bind 挂载到 `/sakura-git/common/worktrees/<validated-task-name>`。这种 nested 布局保持 task metadata 中原生 `commondir=../..` 的相对语义，使其在容器内解析回 `/sakura-git/common`；同时只覆盖当前 task 的可写子树，不让 common metadata 或其他 task metadata 可写。容器注入固定 `GIT_DIR`、`GIT_COMMON_DIR` 和 `GIT_WORK_TREE`。服务端按当前 workspace 身份严格匹配 `gitdir`/`commondir` 指针；交接后 `.git`、`gitdir`、`commondir` 指针由 root-owned `0444` 保护，common metadata 必须已具备 UID 65532 的遍历/只读权限。

## 7. sandboxd UDS 协议

### 7.1 传输与权限

```text
/run/sakura-ai-sandbox/
└── sandboxd.sock     owner=root, group=sakura-ai-sandbox, mode=0660
```

使用独立固定数字 GID；不得复用 updater 的 GID 9472。生产部署仅给 Web 容器补充该 sandbox GID，并只读挂载 socket 目录。socket 目录不得挂进 runner 容器。

### 7.2 版本与端点

所有响应包含 `protocol_version` 和 `sandboxd_version`。当前 wire protocol 为 v2：
`network_mode` 是每个执行请求的必填能力声明，`egress_capability` 是健康响应的必填
能力声明；v1 请求不会静默按 `none` 解释。HTTP 端点路径暂保持 `/v1`，路径是服务
路由命名空间，不代表 wire protocol 版本。端点如下：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/health` | v2 版本、运行时、镜像 digest、可用 profile、`none` / `egress` 能力，不泄露实际 Docker 网络名 |
| `POST` | `/v1/executions` | 使用必填 `network_mode=none|egress` 同步执行一个有上限的请求并返回结构化结果 |
| `POST` | `/v1/executions/{id}/cancel` | 幂等取消；kill 并 remove 对应容器 |

不提供通用 `command` 管理端点，不允许客户端提交 Docker create/run 参数。v2 继续使用同步执行；Backend HTTP 总超时必须大于请求执行上限与固定清理宽限之和。

### 7.3 错误分类

| 类别 | 示例 | Backend 行为 |
| --- | --- | --- |
| `INVALID_REQUEST` | cwd 穿越、未知 profile、字段过长 | 不重试，记录安全事件 |
| `POLICY_DENIED` | prod 请求 local、profile 禁止网络 | 不重试，面向用户说明策略 |
| `RUNTIME_UNAVAILABLE` | Docker/Podman 不可用 | fail closed，可由健康检查提示管理员 |
| `IMAGE_UNAVAILABLE` | runner 镜像未拉取 | fail closed，不临时运行未知镜像 |
| `EXECUTION_TIMEOUT` | 超过 wall clock | kill/remove，返回 timed_out |
| `OUTPUT_LIMIT` | 输出超过上限 | 继续清理并返回截断标记；可按策略终止容器 |
| `CLEANUP_FAILED` | 无法确认容器移除 | 返回基础设施失败并告警 |

## 8. OCI 运行策略

### 8.1 服务端固定参数

推荐 Docker CLI 等价策略：

```text
--network none
--read-only
--user 65532:65532
--cap-drop ALL
--security-opt no-new-privileges:true
--pids-limit 256
--memory 2g
--memory-swap 2g
--cpus 2
--ulimit nofile=1024:1024
--tmpfs /tmp:rw,noexec,nosuid,nodev,size=256m
--tmpfs /home/agent:rw,nosuid,nodev,uid=65532,gid=65532,mode=0700,size=128m
--mount <server-owned exact workspace>:/workspace:rw
--workdir /workspace/<validated-relative-cwd>
--label ai.sakura.managed-by=sandboxd
--label ai.sakura.request-id=<validated-id>
```

容器镜像必须通过部署配置固定为不可变 digest；客户端只能提交公开 profile 和 `none` / `egress` 枚举，不能选择镜像、Docker 网络名或其他运行时参数。`egress` 总是映射到 sandboxd 启动时固定的网络。可选 `runsc` runtime 由宿主配置决定，健康端点报告实际 runtime。

### 8.2 生命周期和恢复

- 容器名由 sandboxd 生成，不包含原始用户输入。
- create/start/attach/inspect/kill/remove 使用 argv 调用，不通过 shell。
- daemon 启动时只扫描同时具有 managed-by 和实例标识 label 的遗留容器；确认归属后清理。
- timeout/cancel 先 `kill`，再带固定超时 `rm -f`；所有路径在 `finally` 中清理。
- 在 Docker `create` 尚未返回容器 ID 的失败/取消路径，若带 label 的延迟清理仍在运行，sandboxd 保留 workspace lease 并拒绝同一目录的新任务；只有清理 worker 结束后才释放 lease，避免工作区被复用或被并发清理。
- daemon 收到终止信号后停止接收新请求，取消在途容器并等待有界清理。

### 8.3 网络策略

Agent 网络策略是全局配置页“Agent”分组中的超级管理员配置：

| WebUI 值 | Agent shell / 构建 / 测试 | 自动依赖安装 | `search_web` / `fetch_url` |
| --- | --- | --- | --- |
| `offline` | `--network none` | 仅允许镜像内依赖或显式离线缓存；需要在线下载时跳过并说明 | 对 Agent 隐藏并在执行入口再次拒绝 |
| `web_tools` | `--network none` | 同 `offline` | 由 Web 后端执行；还必须分别满足 `web_search_enabled`、`fetch_url_enabled` 及域名/SSRF 限制 |
| `full_access` | sandboxd 固定 egress 网络 | 使用同一 egress 网络，可在线安装 | 可用；仍受各工具自己的开关和限制 |

默认值为 `web_tools`：开箱即可使用受控搜索/抓取，而模型生成的任意 shell 仍不出网。自部署管理员选择 `full_access` 后，下一次 Agent 或 Dependency 执行立即使用 egress；它明确允许访问公网、局域网、宿主网关和 metadata，不承诺 RFC1918 隔离。实际网络由 sandboxd 启动配置固定，默认 Docker `bridge` 以保证自部署场景无需额外宿主配置即可工作，也允许运维改成带代理/防火墙的 named network。runner 永远不加入 Sakura AI 的应用 Compose 网络，且不能使用 `host`、`container:*` 或 `ns:*`。

配置热生效遵循以下合同：

1. WebUI 保存事务提交后清除动态配置缓存并更新当前 Settings 视图。
2. `SandboxExecutionRunner.execute()` 对每次调用执行一次 fresh/read-through 配置解析，不能依赖进程启动快照或 60 秒 TTL；多 Web worker 最迟在下一次调用读取数据库值。
3. Agent 的 `search_web` / `fetch_url` wrapper 在每次调用时检查策略和工具开关，不能永久缓存“未启用”状态；配置参数也必须在本次调用前刷新。
4. sandboxd 对每个请求固定一次网络模式；在途容器不热切换，避免半次命令跨策略运行。
5. 配置变更和每次 runner 创建记录有效策略、profile、request/task ID 和镜像 digest，但不记录 API Key、完整命令或页面正文。

## 9. Runner 镜像

新增专用 runner 镜像，不复用 Web 镜像。镜像要求：

- 固定非 root 用户 UID/GID 65532，HOME 可写位置由 tmpfs 提供。
- 提供 Agent 当前承诺的 Python、Node.js、Go、Rust、Java、C/C++ 与常用构建工具。
- Rust/Cargo 等工具链安装到全局只读路径，不依赖 `/root`。
- 包管理器仅在 `full_access` 下启动在线安装；其他策略的自动依赖步骤显式跳过或只消费离线缓存，缓存路径只读挂载时不得自动改权限。联网 pip 仅运行在 `DEPENDENCY` profile。
- 镜像不包含 Sakura AI 配置、源码、凭据或 Docker CLI/socket。
- release 发布 digest，sandboxd 只接受部署状态中固定的 digest。

## 10. 配置与部署

建议新增配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `agent_team_execution_backend` | `sandbox`（image 与 source 均默认） | `sandbox` / `local`；`local` 仅允许 source 下显式选择 |
| `agent_team_network_policy` | `web_tools` | WebUI 超级管理员动态配置：`offline` / `web_tools` / `full_access`；下一次工具调用生效 |
| `agent_team_sandbox_socket` | `/run/sakura-ai-sandbox/sandboxd.sock` | 仅管理员环境配置，不允许任务覆盖 |
| `agent_team_sandbox_timeout_seconds` | `900` | 服务端再做上下限夹取 |
| `agent_team_sandbox_max_output_bytes` | `8388608` | stdout+stderr 总上限 |
| sandboxd `--egress-network` | `bridge` | 仅宿主部署配置；`full_access` 映射到该网络，WebUI/模型不能提交网络名；可改为专用 named network |

资源参数、镜像 digest、工作区宿主映射、runtime、egress 网络名和 GID 属于宿主 deployment state，不放进可由 WebUI 动态修改的 `app_config`。WebUI 只控制是否使用服务端已固定的 egress 能力。

`start.sh` 增加独立的 `sandboxd start|stop|restart|reinstall|uninstall|status` 生命周期。生产启动顺序必须是：验证部署目录和 sandboxd/runner 双 digest → 安装/启动 sandboxd → 验证 `/v1/health` 的 protocol、runtime、instance、workspace 和 digest → 启动 Web。Updater 与 sandboxd 使用不同 GID、UDS、状态目录和协议；稳定版升级通过 updater 的三镜像事务共同更新 Web、sandboxd 与 runner。

两个 Compose 文件必须继续满足：

- Web 不挂载 `docker.sock`。
- Web 只读挂载 sandbox UDS 目录并加入独立数字 GID。
- runner 不作为常驻 Compose service，也不加入应用网络。

## 11. 现有代码迁移

1. `shell_executor.py`：改为执行器抽象；本地实现从空白 allowlist 构造环境，sandbox 实现走 UDS。
2. `shell_tool.py`：保留路径/产品策略校验，但不再宣称黑名单提供隔离；调用统一执行器。
3. `grep_tool.py`：移除直接 `asyncio.create_subprocess_exec` 旁路，使用同一执行器 argv 模式。
4. `git_workspace_service.py`：clone/fetch/worktree/commit/push 进入受信任 argv runner；移除 token URL；自动依赖安装改走 dependency profile。
5. `pr_service.py`：保留受信任 GitHub/Git 控制面，不允许拼入模型命令。
6. ToolContext/专家构造：注入执行器或 execution context，禁止工具自行创建 subprocess。

仓库级 invariant test 应扫描 Agent Team 生产代码中的 `create_subprocess_*`；除集中封装的受信任 runner 和 sandboxd runtime adapter 外出现新调用即失败。

## 12. 测试与验收

### 12.1 单元与合同测试

- 环境不继承：在父进程放置 canary secret，容器输出与 `/proc/*/environ` 均不可见。
- 路径校验：拒绝绝对路径、`..`、空字节、未知 workspace key。
- 协议：版本不兼容、未知字段/profile、超限字段均 fail closed。
- Docker argv：参数完全由服务端生成；镜像/mount/runtime/network 不可由请求覆盖。
- 动态网络策略：WebUI 三档值严格校验；保存后下一次执行使用新值；旧进程缓存、模块级 negative cache 和多 worker 读取均不能让禁用/启用延迟到重启。
- 权限分离：模型 Tool schema 不出现网络字段；伪造 Docker 网络名、`host`、`container:*`、`ns:*` 和未知策略均 fail closed。
- 受控 Web 工具：`offline` 拒绝 Agent 搜索/抓取，`web_tools` 与 `full_access` 仅在各自工具开关开启时允许；`fetch_url` 的 DNS 重绑定、私网/metadata、重定向、域名和大小限制继续覆盖。
- timeout/cancel/output limit：容器被 kill/remove，结果分类稳定。
- Git 凭据：remote URL 与 `.git/config` 不含 token；临时 askpass 在成功/失败/取消后删除。
- 旁路扫描：GrepTool、自动依赖安装和模型工具无裸 subprocess。

### 12.2 Linux Docker 集成测试

在真实 Linux Docker runner 上验证：

1. 可在工作区创建/修改文件，不能读取相邻任务或宿主文件。
2. `offline` 与 `web_tools` 的 runner 网络为 none，不能访问互联网、MySQL、Redis、宿主网关和 metadata；`full_access` 可访问外部 HTTP/DNS，且网络名仍不能由请求覆盖。
3. 进程以 65532 运行，capabilities 为空，`NoNewPrivs` 生效，根文件系统只读。
4. fork bomb/PID、CPU、内存、磁盘临时空间和输出洪泛均被限制。
5. 超时、Backend 取消、sandboxd 重启后没有带管理 label 的遗留容器。
6. 原生 Linux runner 可以写入固定 UID/GID handoff 后的任务 worktree，并可通过固定 venv PATH 找到刚安装的工具；linked worktree 的 Git status/diff 不依赖 Web 的 `/app` 路径。
7. Web 容器内不存在 Docker socket，runner 内不存在两个 UDS。
8. 正常的 Python/Node/Go/Rust/Java/C++ 小项目可离线执行已安装工具链。
9. 同一长生命周期 Backend 进程内依次切换 `web_tools → full_access → offline`，下一次一次性容器分别使用 `none → egress → none`；在途容器保持创建时策略。
10. WebUI 开启、关闭再重新开启 Web 搜索/URL 抓取后无需重启，Agent 工具立即按新配置执行；`offline` 始终优先拒绝。

### 12.3 完成定义

只有同时满足以下条件才可把 Issue #528 标记完成：

- Docker image 部署默认使用 `sandbox` 且 sandboxd 不可用时任务拒绝执行。
- 所有模型影响的子进程和依赖脚本都进入沙箱。
- Web 与 runner 均无 Docker socket；秘密 canary 集成测试通过。
- 真实 Linux Docker 隔离、资源、取消和遗留清理测试通过。
- WebUI 可选择三档 Agent 网络策略；`full_access` 在默认自部署环境真实可联网，策略切换无需重启且有审计记录。
- 中英文配置/部署文档说明本地模式风险、离线限制与运维命令。
- Python/Ruff/现有 Agent Team 测试无回归。

## 13. 后续增强

1. 受控 package proxy：为 `web_tools` 与 `full_access` 之间增加仅域名/证书固定的包仓库策略，阻断私网与 metadata。
2. gVisor：高风险部署可配置 `runsc`，不改变 Backend 协议。
3. rootless Docker/Podman：作为宿主 daemon 的运行时选项验证兼容矩阵。
4. microVM：多租户或强对抗环境可实现 Firecracker backend，继续复用 execution contract。
5. 审计：按 request/task/profile/digest/limits 记录元数据，不记录命令中的潜在秘密和完整源码输出。
