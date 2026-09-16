# 项目记忆

累计反思 33 次

## 核心审查原则（简要）
- **无评论≠无问题**：空结果需检查工具；结论需附验证依据。
- **Fail‑Closed**：宁可误报也不可漏报；阻断项必须标记 error/major。
- **动态审查策略**：增量审查 >3 次或改动 >1000 行时切换 full‑review。
- **最小权限**：CI 工作流显式 `permissions: {}`，配合同约测试。
- **锁文件同步**：`pyproject.toml`、`requirements.txt` 与 `uv.lock` 必须保持一致。
- **递归调用禁令**：网络请求封装层禁止自调用，需使用幂等包装。
- **安全 Cookie**：Set‑Cookie 必须声明 `Secure; HttpOnly; SameSite`，并在合适阶段写入。

## 新增反思要点
### 1. ISSUE‑582（Gitflow 同步冲突）
- **分类**：应归为 `workflow`/`git`，标签建议 `gitflow`, `ci-failure`, `merge-conflict`, `automation`（统一使用 `automation`）。
- **优先级**：当前 low，若项目对 Gitflow 自动同步有 SLA，提升至 medium。
- **可行性**：仅需 `git fetch && git log` 检查，工作量 <5 分钟。
- **经验**：在 CI 报警时附冲突文件列表，自动创建 PR 而非直接合并；在文档 `CONTRIBUTING.md` 中加入同步流程。

### 2. ISSUE‑581（待补）
（此文件暂无具体内容，保留占位以待后续补充）

### 3. PR‑580 增量审查（incr2 / incr3 / 主 PR）
- **覆盖度**：增量审查聚焦改动文件，常漏掉历史 `major` 风险（锁文件漂移、递归调用）。
- **模式**：最小化权限声明、Workflow Contract Tests、频繁小幅增量导致全局风险忽视。
- **建议**：
  1. **锁文件同步检查**：任何 `pyproject.toml`/`requirements.txt` 变动必须自动比对 `uv.lock`，不一致阻断合并。
  2. **递归调用检测**：对 `*_client.py` 使用 AST 检查自调用，标记 `major`。
  3. **增量+全局双模**：>3 次增量或累计改动行数 >800 自动触发 full‑review，列出所有未闭环 `major` 项。
  4. **CI 输出约定**：业务脚本统一返回 JSON（`{"status":"superseded"}`），CI 统一解析，避免字符串误判。
  5. **文档同步**：修改工作流或系统服务必须同步更新对应文档，缺失给 `minor` 警告。

### 4. 其他发现（系统服务、Auth 中间件）
- **systemd 脚本**：需保证幂等、原子写入、权限最小化；在非 systemd 环境提供回退。
- **Auth 中间件**：`WebUITokenRenewalMiddleware` 在 `http.response.start` 写 Cookie，必须声明安全属性并配合 CSRF 防护。
- **依赖升级**：大幅升级 `pydantic`, `redis`, `openai` 时需在 CI 加入兼容性测试套件。

## 行动建议
1. **立即在 PR 中同步锁文件**（`uv lock && git add uv.lock`）。
2. **重构 `updater_client.py`**，消除递归调用，加入幂等包装。
3. **在 CI 添加 `uv lock --check` 与递归调用静态检测**。
4. **更新项目标签列表**：统一使用 `automation`，新增 `merge-conflict`, `ci-failure`。
5. **在审查模板中加入 “前置未解决问题” 检查项**，确保每轮增量审查都回顾历史 `major` 风险。
6. **文档补全**：在 `CONTRIBUTING.md` 添加 Gitflow 同步流程、系统服务启动说明、Cookie 安全策略。

---

通过上述更新，审查覆盖度、准确度与完整性将得到显著提升，既保持快速迭代，又确保关键风险不被遗漏。