# 自动化工作流与 CI/CD 安全审查规则

## 一、Dependabot 与工作流权限防线 (Workflow Security Guard)

### 1.1 Dependabot 触发事件与 Secrets 作用域
1. **禁止在 `pull_request` 下使用 Secrets**：Dependabot 触发的 `pull_request` 事件处于 GitHub 安全沙箱中，`secrets` 变量恒为空。若工作流需要访问 Token/Secret（如推送修复提交或同步锁文件），必须使用 `pull_request_target` 事件。
2. **严格的身份门禁校验**：使用 `pull_request_target` 时，作业级 `if` 门禁必须严格校验：
   ```yaml
   if: github.event.pull_request.user.login == 'dependabot[bot]'
   ```
   严禁使用模糊匹配或误用 App ID（如 `app/dependabot`），防止非机器人 PR 或恶意 Fork 窃取权限。
3. **最小化 Permissions 声明**：`pull_request_target` 工作流必须显式限定最小权限（如仅授予 `contents: write`），严禁配置宽泛的全局写权限。

---

## 二、凭据与推送安全规范

### 2.1 凭据安全传递与防泄漏
1. **严禁在 URL 中硬拼 PAT**：严禁采用 `git push https://x-access-token:${PAT}@github.com/...` 形式传递凭据，防止因命令失败或调试日志开启导致 PAT 明文打印在 CI 终端。
2. **标准化 Checkout 鉴权**：统一通过 `actions/checkout` 的 `token:` 属性设置特权凭据，后续直接执行标准的 `git push origin HEAD:<branch>`。
3. **自动补锁降级可观测性**：补锁工作流（如 `uv-lock-sync.yml`）在缺失必需 PAT Secret 时，严禁仅以静默警告跳过而掩盖问题；必须提供清晰的失败诊断信息或向维护者发信。

---

## 三、工作流配置契约测试要求

### 3.1 YAML 契约测试规范
1. **关键工作流语法断言化**：针对涉及核心 CI 校验、构建发布、Dependabot 自动补锁的 GitHub Actions YAML，建议在测试集（如 `tests/test_*_workflows.py`）中编写基于 `pyyaml` 的契约测试，断言其触发事件、权限范围与关键门禁表达式，防止因手误重构导致流水线静默失效。
