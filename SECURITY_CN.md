# 安全策略 (Security Policy)

[English](SECURITY.md) | **中文**

Sakura AI 团队高度重视应用程序、基础设施及用户数据的安全性。我们非常感谢安全社区和用户对潜在漏洞的负责任披露。

---

## 受支持的版本

我们为以下版本和分支提供安全补丁与维护支持：

| 版本 / 分支       | 是否受支持          | 说明 |
| ---------------- | ------------------ | ------ |
| `3.2.x` (≥ 3.2.4)| :white_check_mark: | 当前稳定版本系列 |
| `develop`        | :white_check_mark: | 日常集成主干分支 |
| `main`           | :white_check_mark: | 生产发布分支 |
| `< 3.2.0`        | :x:                | 已停止维护；请及时升级至最新版本 |

如果你正在运行不受支持的早期版本，请在提交漏洞报告前先升级至最新的稳定版本。

---

## 报告安全漏洞

**请切勿通过公开的 GitHub Issues、Pull Requests 或公开交流渠道报告安全漏洞。**

### 首选渠道：GitHub 私密漏洞报告 (Private Vulnerability Reporting)

我们强烈建议使用 GitHub 内置的私密漏洞上报功能：

1. 访问本仓库的 **Security** 页面：[https://github.com/Sakura520222/Sakura-AI/security](https://github.com/Sakura520222/Sakura-AI/security)
2. 点击左侧栏的 **"Advisories"**。
3. 点击 **"Report a vulnerability"** 按钮（或直接访问：[创建安全报告](https://github.com/Sakura520222/Sakura-AI/security/advisories/new)）。
4. 填写漏洞摘要、受影响版本、危害评估及复现步骤。

这将创建一个私密协作空间，供报告者与仓库维护团队进行安全沟通与补丁验证。

### 备用渠道：直接邮件报告

如果你无法使用 GitHub Security Advisories 功能，或者需要附带敏感的 PoC 附件，可以直接向维护者发送邮件：

- **联系邮箱**：[sakura520222@outlook.com](mailto:sakura520222@outlook.com)
- **邮件主题**：`[SECURITY VULNERABILITY] Sakura AI - <漏洞简述>`

### 报告中建议包含的内容

为了帮助我们更快地核实并修复问题，请在报告中尽可能提供详尽信息：

- **漏洞类型**：（如：远程代码执行、身份验证绕过、注入、SSRF、信息泄露、沙箱逃逸等）。
- **受影响组件**：具体的 API 路由、后台服务、Worker、WebUI 页面或 Updater 模块。
- **受影响版本 / Commit Hash**：发现该漏洞的环境版本。
- **复现步骤 (PoC)**：清晰详尽的逐步复现说明或测试脚本。
- **危害影响评估**：攻击者利用该漏洞可能造成的危害或权限范围。
- **修复建议**：可选的补丁方案或缓解措施。

---

## 响应与披露流程

1. **初步确认**：我们将在收到漏洞报告后的 **48 小时** 内进行初步响应与确认。
2. **定级与验证**：验证漏洞有效性并评估 CVSS 严重级别，持续与报告者同步进展。
3. **补丁开发与测试**：在私密安全分支中开发修复补丁并执行全量回归测试。
4. **协同披露**：
   - 伴随修复版本发布后，我们将在 GitHub 发布对应的安全公告（Security Advisory）。
   - 我们遵循负责任的协同披露原则，通常提供自初次报告起 90 天的披露缓冲期，以便自建实例用户及时升级。
   - 在安全通告中，我们将正式对漏洞发现者致谢（除非报告者希望匿名）。

---

## 私有化部署安全建议

对于自建（Self-Hosting）Sakura AI 的用户，请注意以下基础安全配置：

- **生产环境禁用调试模式**：确保关闭 `DEV_BOOTSTRAP` 和调试配置。
- **严控凭证权限**：对于 `config/connection.json` 和 `.deploy/deployment.env` 等敏感文件设置严格的文件权限（如 `chmod 600`）。
- **定期轮换凭据**：定期轮换 GitHub App 私钥、Webhook Secret、数据库密码以及 Session 密钥。
- **启用双重认证 (MFA)**：所有管理员账户强烈建议在 WebUI 安全中心开启 TOTP / Passkeys 双重认证。
- **网络隔离防护**：切勿将内部存储与服务端口（MySQL、Redis、ChromaDB、Updater unix socket）直接暴露在公网，建议使用防火墙、Docker 内部网络或 Cloudflare Tunnel 保护。
