# 依赖升级审查增强规则（2026‑09 更新）

## 背景
近期 15 份审查反思（PR567‑PR577）暴露出 **依赖升级** 场景的若干盲点：
- 锁文件 (`uv.lock`) 未随 `pyproject.toml` / `requirements.txt` 同步，导致发布产物与 CI 环境不一致。
- 项目同时维护 **双清单**（`pyproject.toml` 与 `requirements.txt`），手动同步易出错。
- 存在 **未使用依赖**（如 `python‑slugify`），占用构建体积并潜在引入安全风险。
- CI 证据缺失：审查结论未附带 CI 运行链接或搜索命令。
- 大版本升级缺乏 **兼容性回归测试** 与 **Breaking Changes** 检查。

## 新增审查规则
| 编号 | 规则描述 | 触发条件 | 期望行为 |
|------|----------|----------|----------|
| **R‑U‑001** | **锁文件同步检查** | 任意修改 `pyproject.toml`、`requirements.txt`、`Pipfile`、`poetry.lock`、`uv.lock` | 必须在同一 PR 中提交对应的锁文件更新；若缺失，审查自动标记 **error** 并提示运行 `uv lock`（或对应工具）并提交。 |
| **R‑U‑002** | **双清单一致性校验** | PR 包含任意依赖声明文件的改动 | CI 自动运行脚本 `scripts/verify_requirements_sync.py`，比较两文件的关键依赖版本；不一致则 CI 失败并在审查报告中给出差异。 |
| **R‑U‑003** | **未使用依赖检测** | PR 中出现依赖版本升级或新增依赖 | 自动执行全库 `import‑checker`（或 `rg`）搜索该包的导入路径；若 0 命中，审查给出 **suggestion** “可移除”。 |
| **R‑U‑004** | **CI 证据强制** | 任意审查结论为 *none / minor / suggestion* | 审查报告必须附带 CI 运行 URL 或 `git grep` 命令输出；缺失则标记为 **error**。 |
| **R‑U‑005** | **大版本升级影响评估** | 依赖版本号跨越 **major** 或 **minor**（如 `pydantic >=2.13.5` → `>=3.0.0`） | 自动抓取上游 Release Notes，列出 **Breaking Changes**；若项目中有调用点，必须提供兼容性测试或迁移代码。 |
| **R‑U‑006** | **锁文件体积限制** | `uv.lock`（或 `poetry.lock`）文件大小 > 5 MB | 给出警告并要求审查员确认锁文件是否必要，或考虑拆分子项目。 |

## 实施建议
1. **CI 集成**：在 `.github/workflows/ci.yml` 中加入 `uv lock --check` 与 `scripts/verify_requirements_sync.py` 步骤，确保所有 PR 必须通过。 
2. **统一依赖更新脚本**：`scripts/update_deps.sh`
   ```bash
   uv lock && git add uv.lock pyproject.toml requirements.txt
   ```
   在本地运行后提交，防止手动遗漏。 
3. **文档同步**：在 `docs/dependency-management.md` 中加入 “未使用依赖检测” 与 “锁文件同步” 的说明，审查时可直接引用。 
4. **审查模板**：在审查摘要中预留 `CI 链接:` 与 `搜索命令:` 两行，强制审查员填写。

---

**适用范围**：所有涉及依赖版本变更的 PR（包括 Dependabot 自动 PR），以及任何新增/删除依赖的代码改动。