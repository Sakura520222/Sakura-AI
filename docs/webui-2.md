# WebUI 2.0 / React workspace

WebUI 2.0 fully replaces the previous WebUI for [Issue #638](https://github.com/Sakura520222/Sakura-AI/issues/638). `/` opens the React application at `/app/`. Saved page URLs redirect to the corresponding React screen. The old Jinja pages, HTMX fragments and browser scripts have been removed from the served interface.

## 使用新版界面

启动应用后访问 `/app/`，未登录时进入 `/app/login`。登录继续使用现有 GitHub OAuth、HttpOnly Cookie、两步验证和通行密钥。被要求补充验证方式的账号只能进入独立的安全设置页面，不能借此访问业务页面。

设置入口为 `/app/settings/personal`。管理员还可访问 AI、审查、Issue、Agent、标签与扫描、系统和备份分类。搜索覆盖当前账号有权查看的设置，支持按名称、说明和配置键查找。每项修改显式保存；冲突保留本地草稿，由用户决定是否采用服务端值。默认值、来源和重置操作放在字段详情中。

AI 账号、角色绑定、单模型能力、插件权限、备份恢复、通知和安全设置复用原有服务。备份恢复继续保留部署时数据库及 Redis 连接设置。系统重启、数据库重置和文件删除仍要求明确确认。

页面保留 Sakura AI 原有的樱花标识，浏览器标签使用同一图案的 SVG favicon。浅色和深色主题共用语义颜色与组件；`Ctrl/Cmd+K` 打开页面和设置搜索。窄屏通过顶部按钮展开主导航。

## Run a published version

Release source archives and container images already include the matching
frontend dist. A Git checkout automatically obtains a matching prebuilt bundle
at startup. Users do not need Node.js or an npm build step.

```bash
uv sync
uv run python -m backend.main
```

After updating the source and Python dependencies, restart the application. The
frontend is selected by backend version and source identity; it never downloads
an unrelated `latest` frontend. Existing matching files work offline. If the
requested artifact has not been published or the network is unavailable, the
backend remains available and page requests return a retryable 503. Requests
retry after a 30-second cooldown; there is no instruction to build it manually.

## Published frontend artifacts

`.github/workflows/webui-build.yml` is called by the stable Release and develop
image workflows, and by trusted `main` pushes for source checkouts. It checks out the exact source ref, installs locked frontend
dependencies, builds once and packages these public assets:

- Versioned Release: `sakura-ai-webui-vX.Y.Z.zip` and `webui-manifest.json`.
- The `webui-builds-vX.Y.Z` prerelease: `sakura-ai-webui-<40-character-SHA>.zip` and
  `webui-<40-character-SHA>.json` for exact commit downloads.
- A short-lived Actions artifact carries the same dist to the source-archive and
  Docker jobs. `.vite/manifest.json` is included explicitly. End users download
  Release assets, so they do not need an Actions login or token.

The source archive includes `web/` with its compiled dist. Official Docker jobs
reuse and verify this prebuilt dist instead of compiling it separately for each
architecture. The runtime image contains only the static files and Python.
Local source Docker builds still build their own matching assets automatically.
The existing Host Updater downloads the complete image, including its frontend,
within the same deployment transaction; no separate frontend rollout is needed.

Every build contains `webui-build.json`: backend version, commit, a canonical
frontend source hash, and each file's size and SHA-256. The download manifest also
pins the archive's size and SHA-256. Runtime selection prefers valid bundled files,
then a matching local cache, then the exact commit asset, with a versioned Release
fallback only when its frontend sources match. Text inputs normalize CRLF so the
same source tree works on Windows and Linux. Backend-only commits may reuse a
bundle with identical frontend inputs and the same backend version.

Downloads admit only the configured GitHub repository and GitHub release CDNs over
HTTPS, enforce time/size limits, and reject unsafe paths, links and duplicate ZIP
entries. A cross-process lock coordinates installation. Extraction and checksum
validation finish in a temporary directory before an atomic cache-pointer update.
Old files are retained on failure. Downloaded builds live in `web/.dist-cache/`;
`web/.dist-current.json` selects the build for the next process. These paths and
`.dist.lock` are ignored by Git and Docker's build context.

Published frontend assets are immutable. A rerun reuses identical bytes and fails
if an existing name has different content. The manifest is uploaded after the
archive, so readers cannot observe a published manifest for an incomplete upload.
The stable update manifest is published only after the frontend, Web image,
Host Updater and sandbox jobs all succeed.

## Frontend development

Only contributors editing the frontend need Node.js 24:

```bash
npm ci --prefix web
npm run dev --prefix web
# To serve a local production build through FastAPI:
npm run build --prefix web
```

Keep FastAPI on port 8000. Vite proxies `/api`, `/auth`, `/star-aid/auth` and `/sse`
to it. OAuth and WebAuthn callback/relying-party settings must match the chosen
origin. `npm run build` writes build identity automatically; do not edit its
manifest or copy assets from an unrelated version.

To prepare release assets locally, after building:

```bash
uv run python -m scripts.package_webui --output .codex-tmp/webui-release
```

## Engineering boundaries

- React Router owns `/app/*`; features are lazy loaded. TanStack Query owns server data and refreshes.
- `web/src/api/client.ts` handles same-origin requests, CSRF, cancellation and session expiry. Browser authentication does not expose bearer tokens to JavaScript.
- Existing business services retain permissions, billing admission, events, concurrency guards and filesystem admission. Versioned adapters return JSON rather than rendered fragments.
- Settings reuse the configuration descriptor contract, immutable defaults and durable revisions. Secret values remain masked and are not stored as query results after mutation.
- Activity uses the existing authorized projection and signed cursors. Its shared SSE lifecycle closes on unmount/session expiry and reports reconnection; bounded polling repairs missed updates.
- UI copy is bilingual. Configuration descriptions and React copy both use `zh-CN.yaml` and `en.yaml`. The `webui_v2` namespace compiles into the small frontend catalog; source pairs identify messages and preserve readable call sites. Interpolated copy passes values separately, so changing language does not change configuration or financial values.

Refresh API types after backend contract changes:

```bash
uv run python -m scripts.export_webui_schema
npm run api:types --prefix web
```

Commit both `web/openapi.json` and `web/src/api/generated.ts`. The WebUI workflow regenerates them and checks for drift.

After changing UI copy, run `uv run python -m scripts.sync_webui_translations --write`. This adds new source pairs to both YAML files, preserves existing translations, and refreshes the compiled frontend catalogs. Existing Version Manager labels are compiled separately and loaded with that route. Commit the YAML and generated catalog changes together. CI runs the same tool with `--check`.

## Verification

```bash
npm run lint --prefix web
npm test --prefix web
npm run build --prefix web
npm run bundle:report --prefix web
npm run e2e --prefix web
```

Playwright starts a temporary SQLite fixture using real configuration routes. It never mounts its test authentication overrides into the production app. Tests using mocked providers and a virtual browser authenticator are distinct from real GitHub OAuth, hardware Passkeys, SMTP, Telegram or payment-provider validation.

The bundle report measures compressed build assets, with a 220 KiB budget for the initial shell. It does not claim a browser performance score. Screenshots and traces are retained as CI artifacts. Implementation details are recorded in the [upgrade notes](https://github.com/Sakura520222/Sakura-AI/blob/develop/docs/superpowers/plans/2026-10-30-webui-2-upgrade.md).
