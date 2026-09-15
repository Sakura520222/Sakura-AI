# PR578-PR580 系列经验教训与后续计划

## 背景概述
本轮审查覆盖了 PR578（Gitflow 分支前缀强制、递归请求治理、三镜像合约兼容检查）、PR579（WebUI 会话续期中间件化、MFA 绕过防护、登录循环治理、Cookie 安全属性统一）以及 PR580（CI 权限最小化、工作流安全契约、增量审查闭环）。这些改动在提升安全、可维护性和流程合规性方面具有里程碑意义。

## 关键经验教训
| 教训 | 具体表现 | 对策 |
|------|----------|------|
| **函数签名污染导致耦合** | `auth_callback` 直接注入 `Response`，导致测试耦合与业务侵入。 | 引入 `queue_webui_token_renewal` 与 `WebUITokenRenewalMiddleware`，统一 Cookie 写入。 |
| **递归请求隐蔽风险** | `UpdaterClient._request` 递归导致短超时误判、潜在死锁。 | 禁止同层递归，改为单独包装函数或使用装饰器 `@transport_once`。 |
| **锁文件同步失效** | 多次 PR 中出现 `uv.lock` 与 `pyproject.toml` 不一致，导致 CI 通过但发布失败。 | 强制在 PR 中同步锁文件，CI 增加 `uv lock --check` 步骤，违背即标记 `error`。 |
| **权限最小化缺失审计** | 工作流改动未检查 `permissions`，潜在凭证泄露。 | 在 `.sakura/rules/lockfile_dependency_rules.md` 中加入 **CI/CD 变更审查**，要求 `permissions:{}` 并配套合同测试。 |
| **中间件写入时序风险** | Cookie 可能在 `RedirectResponse` 前被覆盖。 | 明确在 `http.response.start` 写入，审查规则 `webui_session_middleware_rules.md` 强制检查。 |
| **增量审查盲区** | 只关注本次改动，忽略历史 `major` 项（锁文件、递归）。 | 实施 **增量+全局双模**：累计改动行数 >800 或增量次数 >2 时自动触发 full‑review，复核所有未闭环的历史问题。 |
| **系统服务脚本幂等性** | `start.sh` 大幅改动未验证幂等性。 | 新增 **系统服务脚本审查规则**，要求幂等性单测、原子写入、错误回退路径。 |
| **文档同步缺失** | 合约、Cookie、权限等改动未同步更新 README/CHANGELOG。 | 在审查清单加入 **doc‑diff** 检查，确保每次代码改动对应文档更新。 |

## 后续行动计划
1. **规则完善**
   - 完成 `rules/webui_session_middleware_rules.md`（已实现）。
   - 在 `rules/lockfile_dependency_rules.md` 中加入子项目锁文件同步检查。
   - 新增 `rules/systemd_script_rules.md`，覆盖幂等、原子写入、权限最小化。
2. **审查流程升级**
   - 实装 **增量+全局双模**：在 PR 超过阈值自动切换为 full‑review，审查模板中加入 “历史未闭环问题列表”。
   - 引入 **CI 合约测试**：每次修改工作流必须提供对应的 `test_workflow_contract.py`，验证 `permissions`、`secrets` 使用。
3. **自动化工具**
   - 开发 `sakura_check.py` 脚本，自动执行：
     - `uv lock --check` 与 `pyproject.toml` 对比。
     - 函数调用图检测递归 (`callgraph`)。
     - 工作流 `permissions` 合约校验。
   - 将脚本集成到 CI，任何违规即阻断合并。
4. **文档与培训**
   - 更新 `docs/webui_auth_architecture.md`，加入中间件工作原理、Cookie 属性说明。
   - 编写 **安全合约手册**（`docs/security_contracts.md`），覆盖 CI 权限、锁文件、系统服务脚本等。
   - 对团队进行 **增量审查闭环** 与 **全局风险复核** 的培训，强调历史 `major` 项的追踪。
5. **测试扩展**
   - 为 `queue_webui_token_renewal` 添加 **幂等单元测试**（同一请求多次调用仅写一次 Cookie）。
   - 为 `UpdaterClient._request` 添加 **递归防护测试**，确保不再出现自调用。
   - 为 `start.sh` 编写 **幂等性测试脚本**（重复执行后系统状态不变）。

## 里程碑
- **2026-09-20**：完成规则文件撰写并提交至 `rules/` 目录。
- **2026-09-25**：CI 中集成 `sakura_check.py`，所有 PR 必须通过锁文件、递归、权限合约检查。
- **2026-10-05**：全库审查模板更新，增量+全局双模机制上线。
- **2026-10-15**：完成文档同步与团队培训，确保每位审查者熟悉新规则。

通过上述措施，项目将在 **安全、可维护性、审查覆盖度** 三方面实现显著提升，避免因小幅增量改动而遗漏关键风险，确保每一次合并都是在全局风险闭环的前提下进行的。
