# 项目记忆

累计反思 35 次

## 核心审查原则

- **"无评论"≠"无问题"**：空结果可能源于工具故障；"无问题"结论须附验证依据（搜索命令、CI 链接）
- **高分警惕确认偏误**：高分仍须负向用例验证；"阻断合并"必须对应 error/major 标签
- **审查策略动态升级**：≥30 commits / >500 行，或增量超 3 轮 / 累计 >800 行时切 full；最简变更也宜 medium
- **Fail-Closed**：宁可误报不可漏报；文档不可作验证依据
- **报告结构化**：阻断项须在摘要可见；每轮增量后自动列出"全局待验证项"（历史 major 闭环状态）

## Gitflow 同步冲突与 Issue 自动化（#581-#584 高频主题）

- **自动同步勿直接 merge**：main→develop 先 `git merge --no-commit --no-ff` 预检；有冲突则建 draft PR 而非直接合并/建 Issue，并附 `git diff --name-only` 冲突文件清单
- **优先级看影响面**：冲突阻断 develop CI（阻塞全部在途 PR）宜升 high；已自愈仅一次告警可 low，但须复核自动化健壮性
- **重复检测防误报**：标题相似≠重复；须比对分支方向、冲突 SHA、时间点；同类不同次标 related/similar
- **闭环留痕**：关闭自愈 Issue 前注释解决的 commit SHA；同步流程写入 CONTRIBUTING.md
- **标签统一防碎片化**：细分 gitflow/merge-conflict/ci-failure，勿笼统 other；automated/automation 二选一；维护 LABELS.md，≤5 核心标签
- **.sakura/ 生成文件冲突**：宜重跑生成脚本而非手工编辑；设合并窗口降冲突频率

## 依赖与锁文件

- **锁文件同步=阻断项**：改 pyproject/requirements 必须同 PR 更新 uv.lock；CI 加 `uv sync --locked`/`uv lock --check`，漂移标 error
- **CI 与发布链路一致**：统一 uv sync --locked 防"CI 过、发布挂"；升级必附 Release Notes + snapshot 对比

## 会话认证与函数签名（PR578-579）

- **Cookie 写入统一走 ASGI 中间件**；续期前刷新权威 Claims；特权入口不得绕过 MFA；Bearer/Query 不滑动续期须文档明示
- **改公开函数签名前全库搜调用点**；常量集中定义，测试黑盒断言勿依赖内部常量
- **禁同方法内递归自身 _request**：统一封装外部调用；合约变更须端到端兼容+灰度+故障注入（高覆盖≠安全）

## 安全、CI 契约与流程

- **安全契约配套测试**：workflow `permissions: {}` 等最小权限写成 Workflow Contract Tests 断言，防回归
- **CI 验证不止 lint**：同步类工作流须含单测+type-check，仅 ruff 不足以保证功能可用
- **增量审查勿代全局评估**：每轮复查前轮未决 major；异常脱敏分级、禁裸 except；非 systemd 环境明确报错勿留僵尸进程
