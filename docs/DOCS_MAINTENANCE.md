# 文档维护说明

本仓库使用 VitePress 构建文档站，由 Cloudflare Pages 托管；**Markdown 原文仍保存在 `docs/`，无需复制到其他仓库。**

## 目录原则

- `docs/index.md` 是文档站首页；`docs/README.md` 是 GitHub 和文档站共用的文档目录。
- `docs/quick-start.md` 是简明入门指南，其余既有文档继续保留原路径，避免破坏仓库内旧链接。
- `docs/.vitepress/config.mjs` 负责分类、导航和搜索。
- `docs/superpowers/` 是设计与实施记录，不作为面向用户的站点内容；其他排除项请以 `srcExclude` 为准。
- Agent 与 Billing 的验收记录也不生成公开页面或搜索条目；引用排除文件时使用 GitHub 完整 URL。
- `.gitignore` 只忽略 `.vitepress/dist/` 与 `.vitepress/cache/`，配置和主题源码应正常提交。
- 页面面向用户时优先提供操作步骤；实施日志和长篇验收证据不要混入入门指南。

## 本地预览

在仓库根目录执行：

```bash
npm ci --prefix docs
npm run dev --prefix docs
```

构建校验：

```bash
npm run build --prefix docs
```

## 提交流程

1. 从 `develop` 创建 `docs/...` 分支，修改现有 `docs/*.md` 或添加新文档。
2. 如需在侧边栏公开新文档，更新 `docs/.vitepress/config.mjs` 中的 `sidebar`。
3. 检查所有相对链接、图片路径、标题锚点；链接到仓库根目录（如 `AGENTS.md`）时使用 GitHub 完整 URL。GitHub 与文档站共用的章节入口可添加显式 HTML 锚点，并保留原有标题，避免依赖两个平台不同的标题 slug。
4. 提交目标为 `develop` 的 PR，并通过文档站构建检查。
5. 推送到 `develop` 后，Cloudflare Pages 的 Git 集成自动构建并部署文档。`main` 的改动通过 Gitflow 同步到 `develop` 后进入同一发布流程。

GitHub 的 `.github/workflows/docs-pages.yml` 仅执行构建检查，按事件和 ref 分组；不启用或部署 GitHub Pages。

## Cloudflare Pages 配置

项目 `sakura-ai-docs` 连接本仓库，生产分支为 `develop`，自动生产部署已启用。正式站点为 <https://sakura-ai.pyabunny.com/>，默认域名为 <https://sakura-ai-docs.pages.dev/>。

- 根目录：`docs`
- 构建命令：`npm ci --no-audit --no-fund && npm run build -- --base /`
- 输出目录：`.vitepress/dist`

Cloudflare 的构建参数 `--base /` 覆盖配置中历史保留的 `/Sakura-AI/` 子路径；GitHub 构建检查使用同一参数。排查未发布的新提交时，先确认其已进入 `develop`，再检查 Cloudflare 项目的部署记录。
