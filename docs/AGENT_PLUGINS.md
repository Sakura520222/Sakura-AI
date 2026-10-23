# Agent 插件配置

超级管理员从 Agent → Agent 插件（`/agent-plugins/`）管理部署级插件。页面列出 MCP 服务和 Hooks，提供 JSON 编辑器及现有 Skills 安装/管理入口。新增或删除数组条目即可添加或删除插件，`enabled` 控制启停。页面不会探测服务或执行命令；实际调用受执行时权限、网络策略和 Sandbox 边界约束。

配置存储于 `app_config.agent_team_harness_plugins`，格式版本为 `1`，默认 MCP 和 Hooks 均为空。`agent_team_permission_profile` 默认 `autonomous`，也支持 `read_only`、`workspace_write`、`full_access`；这些配置不能由模型、仓库文档或远端 MCP 结果修改。普通 `/config` 保存拒绝插件专用字段，使用插件页面保存。保存路由校验超级管理员角色和 CSRF，整个配置验证通过后才写入。

```json
{
  "version": 1,
  "mcp": {
    "servers": [{
      "id": "docs",
      "url": "https://mcp.example.com/mcp",
      "enabled": true,
      "timeout_seconds": 300,
      "credential_headers": {"Authorization": "SAKURA_MCP_DOCS_TOKEN"},
      "default_policy": {"enabled": true, "read_only": false},
      "tools": {"search": {"enabled": true, "read_only": true}}
    }]
  },
  "mcp_repository_scopes": {"docs": ["owner/repo"]},
  "hooks": [{
    "id": "checks",
    "event": "before_finish",
    "kind": "command",
    "argv": ["python", "-m", "pytest", "-q"],
    "required": true,
    "enabled": true
  }]
}
```

MCP 使用 HTTP/HTTPS 地址，不接受 URL 用户信息、查询字符串或 fragment。`timeout_seconds` 限制 HTTP I/O 空闲时间，不限制正在推进的任务总时长；省略时使用 Settings 中 `agent_team_mcp_io_timeout_seconds` 默认值 `300`。`default_policy` 默认允许主 Agent 使用服务发现的工具；`tools` 可对精确远端工具名覆写策略。只读子 Agent 仅能使用管理员明确标记 `read_only: true` 的 MCP 工具。能力要求使用运行时已知 capability 名，未知值拒绝保存。

锁定的 MCP SDK 2.3 对现代协议 discovery 握手另有 10 秒探测期限，探测不支持/超时可能转入兼容协议握手；这是连接协商边界，不是模型/工具累计预算，也不是正在执行的 `tools/call` 总时长限制。不能把工具 progress/idle 测试描述成所有握手都只受 I/O 空闲限制。

`credential_headers` 的值只能是 `SAKURA_MCP_*` 部署环境变量名。环境变量应保存完整请求头值，例如完整的 Bearer 值；运行时不会读取任意其他环境变量。`headers` 也支持静态请求头，页面及配置 JSON 响应将所有值替换为 `[UNCHANGED SECRET]`。保留遮罩可继续使用同一服务 ID、URL、请求头名称下的原值；更换端点时需重新指定请求头或引用。管理员日志只记录插件数量和权限配置，不记录请求头、URL 或命令参数。

`mcp_repository_scopes` 以 MCP 服务 ID 为键，值为 `owner/repo` 列表或 `null`。缺省或 `null` 表示不按仓库筛选，空列表表示任何仓库都不可用。仓库身份来自任务数据库，未知任务拒绝加载；无任务上下文时仅返回未限定范围的服务。

Hook 事件支持 `session_start`、`before_model`、`after_model`、`before_tool`、`after_tool`、`before_write`、`after_write`、`before_finish`、`after_finish`、`task_failed` 和 `task_cancelled`。`audit` 仅记录生命周期，不能携带 `argv`；`command` 的 `argv` 必须非空，不能声明 `read_only: true`，只在主会话执行。命令经现有执行后端在任务工作区运行，支持独立参数 `{workspace}`，不读取仓库 Hooks。`required: true` 的失败保留失败语义，`before_finish` 可以阻止完成并提供修正证据。只读子 Agent 不继承命令 Hook 权限。

## English summary

Super admins use **Agent → Agent Plugins** (`/agent-plugins/`) for a readable MCP/hook inventory, a JSON editor and a link to existing Skills installation and management. Add/remove array entries to add/delete plugins; set `enabled` to switch them on or off. Saving configuration does not contact MCP endpoints or execute commands.

Version `1` JSON lives in `app_config.agent_team_harness_plugins`; Settings defaults contain empty MCP and hook lists. The separate `agent_team_permission_profile` defaults to `autonomous`; `read_only`, `workspace_write` and `full_access` are supported. Profiles remain subject to the sandbox and network policy. Only the dedicated super-admin, CSRF-protected save route writes these settings; the generic configuration route rejects them. Invalid submissions make no changes.

The example above configures an HTTP MCP service, an exact read-only tool override and a required pre-finish test hook. MCP URLs cannot contain userinfo, query parameters or fragments. `timeout_seconds` is an HTTP I/O inactivity timeout, not an active task duration cap; omission uses the Settings default of 300 seconds. The default tool policy permits ordinary main-agent tools without individual setup. Only explicitly trusted `read_only` policies make MCP tools available to read-only children.

The locked SDK 2.3 separately applies a ten-second modern discovery probe deadline and may negotiate the legacy protocol after an unsupported/expired probe. This handshake boundary is not a cumulative workload budget or an active `tools/call` duration limit.

Credentials reference deployment variables named `SAKURA_MCP_*` containing the complete header value. Static header values are always masked in HTML and JSON responses; retaining `[UNCHANGED SECRET]` preserves the same server ID, endpoint and header's stored value. Changing endpoints requires supplying credentials again. Audit entries contain counts and profile only. Repository scopes are matched against the trusted task database, not model input. Missing/null scope is unrestricted, an empty list allows no repositories, and unknown tasks fail closed.

The eleven lifecycle event names are listed above. Audit hooks do not run commands. Command hooks require a nonempty argument array, cannot claim read-only status and run only in the main session through the existing workspace execution backend. `{workspace}` is supported as a standalone argument. Repository files cannot install hooks. Required failures remain visible; `before_finish` may prevent completion until corrected. Ordinary execution has no new approval agent or per-tool confirmation step.
