# 锁文件与依赖一致性规则

> 来源：PR572-PR577 依赖管理系列反思（2026-09）。与 dependency_update_review_rules.md 互补，聚焦"声明→锁→构建→发布"全链路一致性。

## 1. 锁文件同步（阻断级）
1. 任何修改 `pyproject.toml` / `requirements.txt` 的 PR，必须同 PR 提交 `uv.lock` 更新；未同步标 **error**，不是 minor/suggestion。
2. CI 强制 `uv lock --check`；锁文件漂移直接失败，作为 merge-gate。
3. 锁文件是发布产物：打包阶段校验锁文件与声明文件版本匹配，不匹配则发布失败。

## 2. 消除双轨解析
4. CI 用 `pip install -r requirements.txt` 而发布用 uv.lock 会导致"CI 过、发布挂"。全链路（CI、发布、Dockerfile）统一 `uv sync --locked`，保证单一解析结果。
5. 双清单（pyproject↔requirements）逐行镜像，用脚本/测试自动比对（参照 `test_pyproject_dependencies_mirror_requirements_txt`）。

## 3. 多子项目防漂移
6. sandboxer/updater 等子项目的核心依赖（pydantic/fastapi 等）版本下限必须与根项目一致；升级时用 `rg "pydantic" --glob "**/pyproject.toml"` 全部核对。
7. 子项目不一致必须在 PR 描述中说明理由，否则标 major。

## 4. 死依赖与保留意图
8. 全库零调用的依赖：升级 PR 中必须注明保留意图或直接移除（如 python-slugify 已被 `re.sub`+`md5` 手写实现取代）。
9. "零导入点 ≠ 可删除"：删除前必须检索 CI 脚本、Dockerfile、`alembic.ini`、`migrations/` 等非代码消费点（alembic 教训）。
10. 标记"保留但未使用"的依赖必须在 docs 记录用途与清理计划。

## 5. 约束风格与运行时
11. 核心库约束建议加上限（`>=X,<Y`），防止未来大版本破坏性变更静默进入。
12. 面向新 Python 版本（如 3.14）升级前，验证 cp3xx wheel 可用性：`pip download --only-binary :all:`。
13. 行为变更升级必须附官方 Release Notes 并核对 Breaking Changes；核心库（pydantic 等）升级后跑完整回归测试。

## 6. 文档同步
14. README/docs/CI 示例中硬编码的版本约束必须与声明文件比对；文档视为代码的只读入口，同步校验。

## 7. 工具约束与统一脚本
15. 大文件（uv.lock 常超搜索上限）不可因此跳过验证，改用分块读取或预生成摘要。
16. 提供 `scripts/update_deps.sh`（`uv lock` + `git add uv.lock pyproject.toml requirements.txt`）并在 CI 强制执行，形成"声明→锁→打包"闭环。
