# WebUI 会话续期架构概述

## 背景
在早期实现中，`auth_callback` 等路由直接注入 `Response` 并在业务代码里写入 `Set-Cookie`，导致以下问题：
- **函数签名污染**：每个需要写 Cookie 的路由都必须额外接受 `response: Response` 参数，增加了测试耦合和维护成本。
- **跨路由不一致**：部分路由（如 `RedirectResponse`）在返回时未能写入 Cookie，导致会话失效或登录循环。
- **安全属性分散**：Cookie 的 `Secure/HttpOnly/SameSite` 等属性在多个文件中硬编码，难以统一管理。

为了解决这些问题，项目在 PR579 中引入了 **WebUI 会话续期中间件**（`WebUITokenRenewalMiddleware`）以及 **依赖层续期函数**（`queue_webui_token_renewal`），实现了 **统一、幂等、可审计** 的 Cookie 写入机制。

## 关键组件
| 组件 | 位置 | 主要职责 |
|------|------|----------|
| `queue_webui_token_renewal` | `backend/api/v1/deps.py` | 在业务依赖层触发续期逻辑，读取/刷新 DB 中的 Claims，并将待写入的 JWT 放入 `request.state._webui_token`。 |
| `WebUITokenRenewalMiddleware` | `backend/api/v1/middleware.py` | 在 ASGI `http.response.start` 事件阶段检查 `request.state._webui_token`，若存在且未写入则使用统一的 Cookie 参数写入响应头。 |
| `refresh_login_claims` | `backend/services/auth_service.py` | 从 `TelegramUser` 表读取最新的 `role、active、email_verified、github_username`，生成权威 Claims。 |
| 常量定义 | `backend/api/v1/deps.py` | `WEBUI_TOKEN_COOKIE_NAME`、`SESSION_MAX_AGE`（秒）等统一配置，避免硬编码。 |

## 工作流
1. **请求进入**：FastAPI 通过依赖注入执行 `require_auth` → `enforce_mfa_enrollment` → `queue_webui_token_renewal`。
2. **Claims 刷新**：`queue_webui_token_renewal` 调用 `refresh_login_claims`，确保使用最新的用户状态生成 JWT。
3. **状态保存**：将生成的 JWT 存入 `request.state._webui_token`，不直接写入响应。
4. **响应阶段**：`WebUITokenRenewalMiddleware` 在 `http.response.start` 事件触发时检查 `request.state._webui_token`，若存在且 `has_webui_cookie` 为 `False`，使用 `response.set_cookie` 写入 Cookie。
5. **幂等守卫**：`has_webui_cookie` 在检测到已有同名 Cookie（如 logout）时阻止再次写入，防止冲突。
6. **异常处理**：401/428 错误处理器会调用 `response.delete_cookie` 删除旧 Cookie，防止登录循环。

## 安全属性
- **Secure**：仅在 HTTPS 环境下发送。
- **HttpOnly**：JS 无法读取，防止 XSS 窃取。
- **SameSite=Lax**（或 `Strict`）：防止跨站请求伪造（CSRF）。
- **Max-Age**：使用统一的 `SESSION_MAX_AGE`（默认 7 天），在代码中统一管理，避免硬编码漂移。

## 测试覆盖
- **单元测试**：`test_webui_session_renewal.py` 验证 `queue_webui_token_renewal` 正确设置 `request.state`，并在中间件中写入 Cookie。
- **负向用例**：测试 MFA 未完成、账号被禁用、Bearer/Query Token 不续期等路径，确保 401/428 处理器删除 Cookie。
- **端到端**：使用 `TestClient` 发起完整请求链路，检查 `Set-Cookie` 是否出现在 `RedirectResponse`、`HTMLResponse`、`JSONResponse` 等不同返回类型中。

## 迁移指南
1. **删除业务路由中的 `response.set_cookie`**，改为依赖 `queue_webui_token_renewal`。
2. **确保所有路由仍然返回 `Response`（或子类）**，中间件会在响应阶段统一处理。
3. **更新文档**：在 `docs/webui_auth_architecture.md` 中记录 Cookie 名称、时效、属性的变更。
4. **CI 检查**：新增 `test_webui_session_renewal.py`，并在 CI 中运行 `ruff` 检查 `response.set_cookie` 的直接调用是否仍存在。

---

**参考实现**
```python
# deps.py
async def queue_webui_token_renewal(request: Request, user: TelegramUser = Depends(get_current_user)):
    claims = await refresh_login_claims(user.id)
    token = create_jwt(claims)
    request.state._webui_token = token
    return token

# middleware.py
class WebUITokenRenewalMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        token = getattr(request.state, "_webui_token", None)
        if token and not request.cookies.get(WEBUI_TOKEN_COOKIE_NAME):
            response.set_cookie(
                key=WEBUI_TOKEN_COOKIE_NAME,
                value=token,
                max_age=SESSION_MAX_AGE,
                secure=True,
                httponly=True,
                samesite="Lax",
            )
        return response
```

此架构实现了 **职责分离**、**安全统一** 与 **幂等写入**，为后续功能扩展（如多域名、跨子系统）提供了可靠的基础。
