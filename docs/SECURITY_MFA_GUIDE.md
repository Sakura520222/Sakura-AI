# 安全与 MFA 指南

本文介绍 Sakura AI Reviewer 的多因素认证（MFA）、Passkeys/WebAuthn、安全中心和安全审计能力。适用于部署管理员、超级管理员和需要启用二次验证的普通用户。

---

## 功能概览

Sakura AI Reviewer 支持以下安全能力：

- **TOTP 两步验证**：用户可在个人设置中扫描二维码绑定认证器 App。
- **恢复码**：启用 TOTP 时生成一次性恢复码，用于认证器不可用时登录。
- **Passkeys/WebAuthn**：用户可注册平台通行密钥或安全密钥，并用于 WebUI 和 API 二次验证。
- **OAuth 后二次验证**：GitHub OAuth 认证成功后，如用户已启用 MFA，会先进入二次验证流程。
- **MFA 失败锁定**：连续 MFA 验证失败达到阈值后临时锁定账户，并通过 Telegram 通知管理员。
- **API Passkey 二次验证**：移动端 API 登录支持使用 Passkey 完成 MFA 二次验证，与 TOTP/恢复码并列可选。
- **全局 MFA 要求**：超级管理员可要求所有普通访问用户先注册至少一种 MFA 方法。
- **单用户 MFA 要求**：超级管理员可对指定用户强制要求注册 MFA。
- **安全中心**：集中查看用户 MFA 状态、Passkey 数量、最近安全事件并执行管理操作。
- **安全审计**：记录启用/关闭全局 MFA、强制/取消单用户 MFA、重置 TOTP、删除 Passkey 等关键操作。

---

## 用户使用流程

### 启用 TOTP

1. 登录 WebUI。
2. 打开个人设置页面。
3. 在“两步验证”区域开始设置。
4. 使用认证器 App 扫描二维码，或手动输入密钥。
5. 输入 6 位验证码完成绑定。
6. 妥善保存系统生成的恢复码。

启用后，后续 GitHub OAuth 登录会要求输入 TOTP 验证码或恢复码。

### 使用恢复码

当认证器不可用时，可在二次验证页面输入未使用过的恢复码。恢复码被使用后会标记为已消耗，不能再次使用。

### 注册 Passkey

1. 登录 WebUI。
2. 打开个人设置页面。
3. 在 Passkeys 区域点击注册。
4. 按浏览器提示使用系统通行密钥、硬件安全密钥或平台认证器完成注册。
5. 可为设备设置易识别的名称。

Passkey 可用于 WebUI 登录后的二次验证。Passkey 功能依赖浏览器 WebAuthn 支持。

---

## 管理员安全中心

超级管理员可通过 WebUI 安全中心执行以下操作：

- 查看所有用户的 TOTP 状态、Passkey 数量、MFA 强制状态和最近安全事件。
- 开启或关闭全局 MFA 要求。
- 对单个用户强制或取消 MFA 要求。
- 重置指定用户的 TOTP 与恢复码。
- 删除指定用户的全部或单个 Passkey。
- 查看最近安全审计事件。

> 重置 TOTP 或删除 Passkey 会影响用户登录能力，建议在确认用户身份后操作。

---

## API 登录中的 MFA

移动端或第三方客户端使用 API v1 OAuth 登录时，流程如下：

1. 调用 `GET /api/v1/auth/github/mobile` 获取 GitHub 授权 URL。
2. 用户完成 GitHub OAuth 授权。
3. 客户端调用 `POST /api/v1/auth/callback`。
4. 如果用户未启用 MFA，接口直接返回 `access_token`。
5. 如果用户已启用 MFA，接口返回 `message="mfa_required"` 与临时 `mfa_token`。
6. 客户端提示用户输入 TOTP 或恢复码，并调用 `POST /api/v1/auth/2fa/verify`。
7. 验证成功后获得正式 `access_token`。

当前 API v1 的二次验证接口支持 TOTP、恢复码和 Passkey；WebUI 支持 TOTP、恢复码和 Passkey 二次验证。

---

## 关键配置项

以下配置可通过 Settings 默认值和 WebUI 动态配置管理生效，具体以部署环境为准。

### TOTP / 恢复码

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `two_factor_enabled` | `true` | 是否允许用户启用两步验证 |
| `two_factor_issuer` | `Sakura AI Reviewer` | 认证器 App 中显示的发行方名称 |
| `two_factor_pending_token_expire_minutes` | `10` | OAuth 后等待二次验证的临时 Token 有效期 |
| `two_factor_verify_rate_limit` | `5/minute` | 二次验证接口限流规则 |
| `two_factor_setup_rate_limit` | `10/minute` | TOTP 设置接口限流规则 |
| `two_factor_recovery_code_count` | `10` | 生成的恢复码数量 |
| `two_factor_recovery_code_length` | `10` | 恢复码随机字符长度 |
| `two_factor_encryption_key` | 空 | TOTP Secret 加密密钥；为空时从 `webui_secret_key` 派生 |

### Passkeys / WebAuthn

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `passkeys_enabled` | `true` | 是否允许用户注册和使用 Passkeys/WebAuthn |
| `passkeys_rp_id` | 空 | WebAuthn Relying Party ID；为空时使用应用域名 |
| `passkeys_rp_name` | `Sakura AI Reviewer` | WebAuthn Relying Party 显示名称 |
| `passkeys_origin` | 空 | WebAuthn 允许的 Origin；为空时根据应用域名和端口推导 |
| `passkeys_challenge_ttl_seconds` | `300` | WebAuthn challenge 有效期（范围 60-900） |
| `passkeys_authentication_rate_limit` | `10/minute` | Passkey 认证接口限流规则 |

### MFA 失败锁定

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `mfa_lockout_threshold` | `5` | 连续 MFA 验证失败次数达到此值后锁定账户（范围 3-20） |
| `mfa_lockout_duration_minutes` | `10` | 锁定持续时间，单位分钟（范围 1-60） |

锁定机制说明：

- 基于 Redis 追踪每个用户的 MFA 验证失败次数（Redis 不可用时自动降级为内存追踪）。
- 验证成功后自动清除失败计数。
- 锁定触发后发送 Telegram 通知给管理员。
- `/auth/2fa/verify`、`/auth/2fa/passkey/options`、`/auth/2fa/passkey/verify` 三个端点均集成锁定检查。

---

## 部署注意事项

### HTTPS 要求

生产环境使用 Passkeys/WebAuthn 时必须通过 HTTPS 访问。浏览器通常只允许在安全上下文中使用 WebAuthn；`localhost` 可作为本地开发例外。

### RP ID 与 Origin

- `passkeys_rp_id` 通常应设置为主域名，例如 `example.com`。
- `passkeys_origin` 应包含协议和域名，例如 `https://pr-bot.example.com`。
- 如果通过反向代理、CDN 或非标准端口访问，请确保外部访问 Origin 与配置一致。

### Cookie Secure

生产 HTTPS 环境建议启用 `webui_cookie_secure`，避免登录 Cookie 在非安全连接中传输。

### Redis 可用性

TOTP 设置临时密钥和 WebAuthn challenge 优先存储在 Redis 中。Redis 不可用时系统提供有限内存回退，但多进程/多实例部署下应确保 Redis 正常运行，以避免二次验证流程跨进程失效。

---

## 安全审计事件

安全中心会记录关键安全操作，包括但不限于：

- 启用或关闭全局 MFA 要求。
- 对单用户强制或取消 MFA 要求。
- 重置用户 TOTP 与恢复码。
- 删除用户 Passkey。
- 用户 MFA/Passkey 相关验证事件。

审计信息用于追踪管理员操作和排查异常登录问题。建议定期检查安全中心最近事件。

---

## 常见问题

### 用户只注册了 Passkey，API 登录如何完成二次验证？

API v1 现已支持 Passkey 二次验证（`POST /auth/2fa/passkey/options` + `POST /auth/2fa/passkey/verify`）。客户端可根据 `mfa_required` 响应中的 `methods` 字段判断可用验证方式。面向移动端建议同时支持 TOTP 和 Passkey 两种路径，以获得最佳用户体验。

### 为什么 Passkey 注册失败？

常见原因包括：

- 未使用 HTTPS。
- `passkeys_origin` 与实际访问地址不一致。
- `passkeys_rp_id` 与访问域名不匹配。
- 浏览器或设备不支持 WebAuthn。

### 强制 MFA 后用户无法访问普通页面怎么办？

当全局或单用户 MFA 要求开启后，未注册任何 MFA 方法的用户会被引导到个人设置完成注册。API 请求可能返回 `428 MFA enrollment required`。超级管理员可在安全中心取消该用户的强制要求或协助重置 MFA。
