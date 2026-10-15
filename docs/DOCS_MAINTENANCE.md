# 文档维护说明

本仓库使用 VitePress 和 GitHub Pages 构建文档站；**Markdown 原文仍保存在 `docs/`，无需复制到其他仓库。**

## 目录原则

- `docs/index.md` 是文档站首页；`docs/README.md` 是 GitHub 和文档站共用的文档目录。
- `docs/quick-start.md` 是简明入门指南，其余既有文档继续保留原路径，避免破坏仓库内旧链接。
- `docs/.vitepress/config.mjs` 负责分类、导航和搜索。
- `docs/superpowers/` 是设计与实施记录，不作为面向用户的站点内容；其他排除项请以 `srcExclude` 为准。
- 页面面向用户时优先提供操作步骤；实施日志和长篇验收证据不要混入入门指南。

## 本地预览

在仓库根目录执行：

```bash
npm install --prefix docs
npm run dev --prefix docs
```

构建校验：

```bash
npm run build --prefix docs
```

## 提交流程

1. 从 `develop` 创建 `docs/...` 分支，修改现有 `docs/*.md` 或添加新文档。
2. 如需在侧边栏公开新文档，更新 `docs/.vitepress/config.mjs` 中的 `sidebar`。
3. 检查所有相对链接、图片路径、标题锚点；链接到仓库根目录（如 `AGENTS.md`）时使用 GitHub 完整 URL。
4. 提交目标为 `develop` 的 PR，并通过文档站构建检查。
5. 随正式 `release/*` 分支合入 `main` 后，工作流自动部署 GitHub Pages。

## GitHub Pages 首次启用

仓库管理员需在 `Settings → Pages → Build and deployment → Source` 选择 **GitHub Actions**。正式站点默认地址为 <https://sakura520222.github.io/Sakura-AI/>；站点由 `main` 分支的 `.github/workflows/docs-pages.yml` 自动构建部署。

文档导航的 `base: '/Sakura-AI/'` 对应仓库项目站点路径。以后如绑定自定义域名，需同时修改此 base、配置 Pages 自定义域名和 DNS，并检查站内资源路径。
