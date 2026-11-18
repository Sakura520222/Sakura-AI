# Issue #738 设置页与回归验收

本轮基线为 `origin/feature/738-webui-regressions` 的 `8b8ddc4b`，目标分支为 `develop`。原分支的机制和历史验证见[首轮验收](2026-11-13-issue-738-validation.md)。以下记录区分独立复跑、浏览器实测及外部服务证据。

| #738 | 实现位置 | 验收证据与适用范围 |
| --- | --- | --- |
| 1 GitHub 用户隔离与社区管理权限 | `github_app.py`、`app/auth.tsx`、社区管理 API | 已验证 A/B 用户、会话切换及普通用户 API 拒绝；凭据及 GitHub 接口使用替身 |
| 2 PR 编号 | `api/v1/reviews.py`、`components/workflow/Workflow.tsx` | 已验证 API、导出字段以及 `authorization-review-738.spec.ts` 中有效编号和缺失编号两种链接状态 |
| 3 回退顺序、候选上限与运行时刷新 | `backend/core/ai_protocol/`、配置中心、Activity | 已用隔离 API 验证回退配置写入及 `0` 的备份往返，用替身模型验证执行顺序、上限、账号失效及避免沿用旧候选 |
| 4 拖拽与键盘回退排序 | `StructuredEditor.tsx` | `ai-settings-738.spec.ts` 验证拖拽，`settings-ux-738.spec.ts` 验证键盘按钮、批量保存和重载 |
| 5 已配置模型与能力覆盖 | `SettingsPage.tsx`、配置中心模型 API | 已验证启用账号和已配置模型选择、按供应商模型身份请求、跨分类定位并保留编辑值 |
| 6 授权活动正文 | `ActivityPage.tsx`、活动 API | 已验证直接呈现可见消息、受保护内容的服务端授权及未采集状态，使用隔离 API 和浏览器夹具 |
| 7 工具结果结构化 | `ActivityPage.tsx` | 已验证配对、筛选、JSON、Markdown、代码、长内容展开与复制，使用组件和浏览器夹具 |
| 8 连续设置、目录与高级字段 | `SettingsPage.tsx`、`sectionMeta.ts`、`settings.css` | 已验证桌面目录、字段深链和连续高级字段；支持内容可见的移动目录、弹层和搜索 |
| 9 重启入口 | `AppShell.tsx`、`SystemPanel.tsx` | 已验证顶栏入口、确认和对原 API 的调用，使用替身服务和启动身份；未操作实际部署 |
| 10 更新检查与调度 | `update_checker.py`、`backend/main.py`、版本页 | 已验证源码和镜像部署分支、共享检查任务、失败保留上次结果、启动停止及恢复；外部 registry 和 updater 使用替身 |
| 11 版本通道与构建来源 | `api/v1/version.py`、`VersionBrowser.tsx` | 已验证 development 筛选和完整 revision、digest、时间按可用性展示；镜像值来自版本 API 测试数据 |
| 12 发布说明原位展开 | `VersionBrowser.tsx` | 已验证版本列表内展开 Markdown 和收起，使用浏览器夹具 |
| 13 顶栏版本与更新状态 | `AppShell.tsx` | 顶栏只显示当前版本号。有新版时数字右上角显示红点。组件覆盖无新版和状态未知，浏览器验证桌面与手机红点及管理员入口 |
| 14 Star 互助图标 | `AppShell.tsx` | 已验证 Star 和 Issues 图标独立 |
| 15 普通用户与管理员管理分离 | `CommunityPage.tsx`、社区管理 API | 已验证管理权限、列表搜索分页、维护入口及个人展示仓库弹层，使用社区 API、组件和浏览器回归 |
| 16 全局开关归系统设置 | `CommunityPage.tsx`、设置中心 | 已验证业务页仅展示状态，系统配置受管理员权限保护 |
| 17 响应式仓库卡片 | `CommunityPage.tsx`、社区样式 | 已验证桌面和移动布局、仓库数据层级和摘要展开，使用浏览器夹具 |

| 设置页追加验收 | 位置 | 当前证据 |
| --- | --- | --- |
| A 目录、锚点与历史导航 | `SettingsPage.tsx`、`sectionMeta.ts` | 浏览器验证锚点后焦点、前进/后退、重载后字段状态和底部栏避让 |
| B 全局搜索与返回上下文 | `SettingsPage.tsx`、配置目录 API | 验证键盘进入搜索结果、双语目录、按键和别称匹配、权限隔离及跨分类草稿；包含索引错误重试与结果上限界面 |
| C 说明、字段状态与复杂编辑器 | `SettingsPage.tsx`、`StructuredEditor.tsx`、`config_metadata.py` | 核对字段单位、范围和依赖说明、继承、部署只读、重启状态、AI 账号和优先级布局 |
| D 会话草稿与保存 | `state.ts`、`ChangesDialog.tsx` | 验证不可变快照、部分成功、只重试失败项、并发编辑、重复提交、冲突、字段版本刷新、重叠写入和凭据脱敏 |
| E 可用性与视觉 | `settings.css` | Chromium 截图及键盘路径，涵盖中英、浅色和深色、桌面和窄屏；独立审查了底部操作条遮挡和修改清单重复信息 |

## 复查和验证边界

- 设置浏览器流程使用 Windows Chromium、隔离 SQLite、真实 FastAPI 配置端点和测试身份。部分失败通过单条 PATCH 的 503 注入，其他字段实际写入数据库。延迟响应场景明确核对接口实际保存的值与编辑器保留的后续草稿。
- 328 项后端定向回归通过，覆盖权限、配置及备份、回退、Activity、版本和 Star 互助。运行中没有使用生产凭据、真实收费模型、真实支付或真实收件人。
- 配置各项独立提交。重叠的结构对象及其子字段须先选择保留哪份修改，冲突项须由用户显式处理。多进程设置热同步仍以既有 revision 轮询为准。部署只读字段保持原有权限。
- 实际 GitHub 授权、真实镜像仓库和正在运行的生产实例重启保留为外部验证项。窄屏通过 CSS 视口检查重排，实机软键盘和浏览器原生缩放未实测。

## 本地验证

| 检查 | 结果 |
| --- | --- |
| 前端 Vitest | 125 项通过 |
| Chromium Playwright | 24 项通过，包含三条设置流程、并发保存、GitHub 帐号切换、PR 跳转、版本、活动、仓库互助及既有流程 |
| 生产构建与 API 契约 | TypeScript、Vite 生产构建、bundle 报告通过，重新生成的 OpenAPI 与 TypeScript 类型无内容差异 |
| 静态检查 | ESLint、Ruff、`run_ruff.py --check`、`git diff --check`、1676 条双语 WebUI 文案校验通过 |
| Windows 全量 pytest | 首轮 6,498 项通过、312 项跳过、3 项失败。修正时区别称后复跑了时间边界和相关 API。另两项 Agent 诊断脱敏耗时及子进程启动时限用例在 `8b8ddc4b` 的独立工作树同样失败 |

## 页面记录

| 场景 | 截图 |
| --- | --- |
| AI 回退链、桌面目录与修改栏 | [中文浅色](assets/issue-738/ai-fallback-desktop.png) |
| 审查设置 | [中文深色](assets/issue-738/review-desktop-dark.png) |
| 系统配置和重启生效提示 | [英文浅色](assets/issue-738/system-desktop-en.png) |
| 手机目录 | [英文目录展开](assets/issue-738/mobile-section-directory.png) |
| 查看跨分类修改 | [修改清单](assets/issue-738/pending-changes.png) |
| 两项保存、一项失败并可重试 | [部分保存结果](assets/issue-738/partial-save.png) |
| 相关 #738 回归 | [活动工具结果](assets/issue-738/activity.png)、[手机仓库互助管理](assets/issue-738/community-mobile.png)、[桌面版本信息](assets/issue-738/version.png)、[手机版本红点](assets/issue-738/version-mobile.png) |
