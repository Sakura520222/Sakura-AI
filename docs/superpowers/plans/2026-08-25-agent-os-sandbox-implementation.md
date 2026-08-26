# Agent OS 级沙箱实施计划

状态：代码、单元测试、静态部署合同和文档已完成；真实 Linux Docker 隔离测试尚未在本地执行，仍是发布验收门。

> 本计划供实现代理逐项执行。安全合同以 [Agent OS 级沙箱设计](../specs/2026-08-25-agent-os-sandbox-design.md) 为唯一真相源；发生冲突时先修订设计并重新审查，不在代码中另造一套规则。

**目标：** 为 Docker 部署的 Agent 命令提供 fail-closed 的宿主侧一次性 OCI 沙箱，消除 Web 环境继承、裸 subprocess 旁路和 Git token 持久化。

**执行约束：** 不提交、不推送、不创建 PR、不暂存文件；保留工作区既有改动。每个切片由 Luna-Worker 实现，随后由不同 Luna-Worker 做反模式审查和代码质量审查。审查未通过时必须回到实现者修正，不进入下一切片。

## 切片 1：执行信任域与秘密边界

### 交付

- [x] 建立统一 ExecutionRequest/ExecutionResult、profile 与 runner protocol。
- [x] 把现有 ShellExecutor 收敛为 LocalExecutionRunner；从空白 allowlist 构造环境，禁止 `os.environ.copy()`。
- [x] ShellTool 通过注入的 runner 执行，保持现有 ToolResult 和超时语义。
- [x] GrepTool 改用 runner argv 模式，不再直接创建 subprocess。
- [x] GitWorkspaceService/PRService 的 Git 控制面明确使用 TrustedGitRunner，仅接受应用构造 argv。
- [x] GitHub token 从 clone URL 移除，使用单次临时凭据注入；任何 `.git/config`/remote URL 都不得含 token。
- [x] 自动依赖安装进入 `UNTRUSTED_DEPENDENCY` profile；在 sandbox backend 未实现前 fail closed/明确跳过，不得退回 Web subprocess。
- [x] 添加生产代码裸 subprocess invariant test 和秘密 canary 测试。

### 关键文件

```text
backend/services/agent_team/execution.py                  create
backend/services/agent_team/shell_executor.py             modify/compatibility facade
backend/services/agent_team/tools/base.py                  modify
backend/services/agent_team/tools/shell_tool.py            modify
backend/services/agent_team/tools/grep_tool.py             modify
backend/services/agent_team/git_workspace_service.py       modify
backend/services/agent_team/pr_service.py                  modify if required
backend/services/agent_team/fullstack_expert.py            modify
tests/test_agent_execution.py                              create
tests/test_agent_team_workspace.py                         modify
tests/test_agent_tool_framework.py                         modify
```

### 验证

```powershell
python -m pytest tests/test_agent_execution.py tests/test_agent_team_workspace.py tests/test_agent_tool_framework.py tests/test_agent_team_git_workspace.py -q
python run_ruff.py --check
```

### 退出条件

- 父进程 canary secret 不出现在 Agent/依赖 runner 环境中。
- Git token 不出现在命令、异常、日志、remote URL 或 `.git/config`。
- Agent Team 模型工具不存在 runner 之外的 subprocess 旁路。
- 现有 Agent Tool/Workspace 定向测试通过。

## 切片 2：sandboxd 协议、客户端与生命周期骨架

### 交付

- [x] 新建独立 sandboxd 包/入口，不能复用 updater socket、GID、状态或路由。
- [x] 定义 Pydantic 请求/响应、协议版本、错误分类和严格 unknown-field rejection。
- [x] 实现 `/v1/health`、`/v1/executions`、幂等 cancel。
- [x] Backend 实现 UDS SandboxExecutionRunner，校验协议并映射错误；不得本地 fallback。
- [x] 实现 request registry、并发上限、取消、shutdown drain 与有界输出收集。
- [x] 增加 `agent_team_execution_backend` 与 socket/timeout/output 配置；image 模式拒绝 local。
- [x] 添加 fake runtime adapter，使协议和生命周期单测不依赖 Docker。

### 关键文件

```text
sandboxer/pyproject.toml                                   create or equivalent independent package
sandboxer/src/sakura_ai_sandboxer/...                     create
sandboxer/tests/...                                       create
backend/services/agent_team/sandbox_client.py              create
backend/core/config_section_defaults.py                    modify
backend/core/config.py                                     modify if environment settings are used
backend/webui/translations/zh-CN.yaml                      modify if user-visible config is added
backend/webui/translations/en.yaml                         modify if user-visible config is added
tests/test_agent_sandbox_client.py                         create
```

### 验证

```powershell
python -m pytest sandboxer/tests tests/test_agent_sandbox_client.py tests/test_agent_execution.py -q
python run_ruff.py --check
```

### 退出条件

- Backend 与 daemon 协议合同、版本拒绝和错误映射有自动测试。
- socket 不可用、请求取消、daemon shutdown 均 fail closed 且不泄漏任务。
- daemon 代码中没有接收任意 Docker argv/镜像/mount/network/runtime 的字段。

## 切片 3：OCI 一次性容器后端

### 交付

- [x] 实现 Docker CLI runtime adapter，全程 argv，不调用 shell。
- [x] 服务端固定镜像 digest、network none、只读 rootfs、65532、cap-drop、NNP、资源和 tmpfs 参数。
- [x] 实现严格 workspace key/cwd 校验与服务端权威 mount 解析。
- [x] 实现 create/start/attach/inspect/kill/remove；所有异常、超时与取消路径清理。
- [x] 以双 label 标记归属；启动恢复只清理确认属于当前实例的遗留容器。
- [x] 实现输出总量限制和稳定的 timeout/cancel/cleanup error 分类。
- [x] 构建专用 runner Dockerfile，工具链不依赖 root HOME，不含应用秘密或 Docker 客户端。
- [x] 添加 Docker argv 快照测试和 Linux Docker 集成测试（用例已添加；本地 Windows 环境未执行 Linux 用例）。

### 关键文件

```text
sandboxer/src/sakura_ai_sandboxer/runtime/...              create
sandboxer/tests/test_docker_runtime.py                     create
sandboxer/tests/integration/test_docker_isolation.py       create
docker/Dockerfile.agent-sandbox                            create
docker/scripts/...                                         create if required
```

### 验证

```powershell
python -m pytest sandboxer/tests -q
python run_ruff.py --check
```

Linux Docker 质量门：

```bash
python -m pytest sandboxer/tests/integration/test_docker_isolation.py -q
```

### 退出条件

- 单测证明客户端无法覆盖 server-owned 安全参数。
- Linux Docker 测试证明秘密、路径、网络、身份、capability、资源、超时和遗留清理边界。
- 未运行 Linux Docker 集成测试时只能报告“代码完成、运行时未验收”，不能宣称 Issue 完成。

## 切片 4：Agent 全路径切换

### 交付

- [x] Docker image 部署默认选择 SandboxExecutionRunner；sandboxd 不健康时拒绝启动 Agent 工具任务。
- [x] ShellTool、GrepTool、依赖安装全部使用对应 sandbox profile。
- [x] TrustedGitRunner 只处理控制面 Git 操作，不接受模型 command/string。
- [x] 保持 SSE 事件、取消、状态和输出截断的用户体验。
- [x] 删除被新 OS 边界替代的高误阻断黑名单，仅保留跨平台产品策略；每个保留规则注明目的。
- [x] 增加端到端 fake-daemon 测试和现有 Agent Team 回归测试。

### 验证

```powershell
python -m pytest tests/test_agent_team_tool_calling.py tests/test_agent_tool_framework.py tests/test_agent_team_workspace.py tests/test_agent_team_git_workspace.py tests/test_agent_sandbox_client.py -q
python run_ruff.py --check
```

### 退出条件

- production code invariant test 能发现新裸 subprocess 旁路。
- image deployment 不存在自动 local fallback。
- Agent 正常命令、失败、超时和取消的行为有回归测试。

## 切片 5：部署、运维与文档

### 交付

- [x] `start.sh` 增加独立 sandbox 生命周期；路径、容器身份、socket listener 和删除范围必须 fail closed。
- [x] 开发/生产 Compose 只读挂载独立 UDS 并加入独立数字 GID；继续禁止 Docker socket。
- [x] 构建/发布 sandboxd 与 runner digest；生产配置锁定具体版本/digest。
- [x] 启动时验证 sandboxd health、协议、runtime 和 runner digest。
- [x] 更新中英文 README、CONFIGURATION、DEPLOYMENT；说明离线限制、开发 local 风险和故障排查。
- [x] 添加 Compose/start.sh 静态 invariant test。

### 关键文件

```text
start.sh                                                   modify
docker/docker-compose.yml                                 modify
docker/docker-compose.prod.yml                            modify
.github/workflows/...                                     modify if release assets are added
docs/CONFIGURATION.md                                     modify
docs/DEPLOYMENT.md                                        modify
README.md / README_EN.md                                  modify
tests/test_compose_updater_mount.py                       modify or split shared socket invariants
tests/test_docker_config_persistence.py                   modify
```

### 验证

```powershell
python -m pytest tests/test_compose_updater_mount.py tests/test_docker_config_persistence.py -q
python -m pytest -q
python run_ruff.py --check
```

### 退出条件

- Docker socket absence、UDS/GID 分离、部署状态持久化和生命周期命令均有测试。
- 中文/英文用户文档一致。
- Windows 上无法完成的 Linux daemon/Compose/Docker 证明被列为发布阻断质量门。

## 最终安全审查清单

- [x] 运行器是否有任何 `os.environ.copy()` 或秘密透传？
- [x] Git token 是否可能进入 URL、配置、日志、异常、命令行或沙箱？
- [x] Backend 是否能控制镜像、宿主路径、网络、runtime 或 Docker 参数？
- [x] 是否存在 sandboxd 失败后裸执行的路径？
- [x] 是否所有模型影响的 subprocess 和依赖 hook 都被覆盖？
- [x] runner 是否能看到 Docker socket、两个 UDS、应用目录或其他任务？
- [x] timeout/cancel/daemon crash 是否一定 kill/remove，失败是否告警？
- [x] 生产默认、配置迁移和旧部署升级是否 fail closed？
- [ ] Linux Docker 集成测试是否实际运行并保留证据？
