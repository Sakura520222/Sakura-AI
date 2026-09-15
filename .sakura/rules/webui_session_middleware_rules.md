# WebUI 会话续期中间件规则

## 目标
统一、可靠地在 **FastAPI** 响应阶段为 WebUI 写入 `webui_token` Cookie，防止业务路由污染签名并保证安全属性。

## 必须遵守的要点
1. **统一入口**：所有对 `webui_token` 的写入只能通过 `queue_webui_token_renewal`（依赖 `request.state`) 或 `WebUITokenRenewalMiddleware` 完成。业务路由 **禁止** 直接 `response.set_cookie`。
2. **写入时机**：必须在 ASGI 事件 `http.response.start` 中写入，确保即使返回 `RedirectResponse`、`StreamingResponse` 也能携带 Cookie。使用 `await call_next(request)` 后检查 `request.state._webui_token` 并写入。
3. **安全属性**：Cookie 必须声明 `Secure; HttpOnly; SameSite=Lax|Strict`，并使用统一的 `WEBUI_TOKEN_COOKIE_NAME` 与 `SESSION_MAX_AGE`（秒）常量。
4. **幂等守卫**：在同一次请求的多个依赖层（如 `require_auth`、`require_api_auth`）可能都调用 `queue_webui_token_renewal`，中间件必须检测 `has_webui_cookie` 并只写一次，防止重复 `Set-Cookie`。
5. **Claims 刷新**：续期前必须调用 `refresh_login_claims` 从数据库读取最新 `role、active、email_verified、github_username`，防止已禁用/降权用户旧 Token 续期。
6. **异常路径**：在 401/428 等错误响应的处理器里必须 **删除** `webui_token` Cookie，防止登录循环。
7. **文档同步**：任何对 Cookie 名称、时效或属性的改动必须在 `docs/webui_auth_architecture.md` 与 README 中同步更新。

## 检查清单（审查时使用）
- [ ] `queue_webui_token_renewal` 只在 `request.state` 设置 `_webui_token`，未直接写 Cookie。
- [ ] `WebUITokenRenewalMiddleware` 在 `http.response.start` 写入，使用 `response.set_cookie(..., secure=True, httponly=True, samesite="Lax")`。
- [ ] 所有业务路由未出现 `response.set_cookie` 对 `webui_token` 的直接调用。
- [ ] `refresh_login_claims` 在续期前被调用，返回的 Claims 与 DB 完全一致。
- [ ] 401/428 错误处理器删除 Cookie (`response.delete_cookie`).
- [ ] 文档 `docs/webui_auth_architecture.md` 中列出 Cookie 名称、时效、属性。
- [ ] 中间件加载顺序在 `app.add_middleware` 中明确，位于所有路由之前。

**违背此规则的改动应标记为 `major` 或 `error`，并在 PR 中提供完整的单元/集成测试**。
