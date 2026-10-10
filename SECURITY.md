# Security Policy

**English** | [中文](SECURITY_CN.md)

The Sakura AI team takes the security of our application, infrastructure, and user data seriously. We appreciate the responsible disclosure of vulnerabilities by the security community and our users.

---

## Supported Versions

Security updates and patches are actively maintained for the following versions and branches:

| Version / Branch | Supported          | Status |
| ---------------- | ------------------ | ------ |
| `3.2.x` (≥ 3.2.4)| :white_check_mark: | Current stable release series |
| `develop`        | :white_check_mark: | Active integration branch |
| `main`           | :white_check_mark: | Production release branch |
| `< 3.2.0`        | :x:                | End of life; please upgrade to latest stable |

If you are running an older unsupported version, please upgrade to the latest stable release before filing a vulnerability report.

---

## Reporting a Vulnerability

**Please do NOT report security vulnerabilities through public GitHub Issues, Pull Requests, or public discussion channels.**

### Preferred Method: GitHub Private Vulnerability Reporting

We strongly recommend reporting vulnerabilities using GitHub's built-in private reporting feature:

1. Navigate to the repository's **Security** tab: [https://github.com/Sakura520222/Sakura-AI/security](https://github.com/Sakura520222/Sakura-AI/security)
2. Click **"Advisories"** in the left sidebar.
3. Click **"Report a vulnerability"** (or use the direct link: [New Security Advisory](https://github.com/Sakura520222/Sakura-AI/security/advisories/new)).
4. Provide a detailed summary, affected versions, severity assessment, and reproduction steps.

This creates a private workspace where you can collaborate securely with the repository maintainers.

### Alternative Method: Direct Email

If you cannot use GitHub Security Advisories or need to attach sensitive materials, please send an encrypted or direct email to the project maintainer:

- **Contact Email**: [sakura520222@outlook.com](mailto:sakura520222@outlook.com)
- **Subject Line**: `[SECURITY VULNERABILITY] Sakura AI - <Brief Description>`

### What to Include in Your Report

To help us triage and resolve the issue quickly, please include as much information as possible:

- **Vulnerability Category**: (e.g., Remote Code Execution, Authentication Bypass, Injection, SSRF, Information Disclosure, Sandbox Escape).
- **Affected Component**: Specific service, endpoint, background worker, or updater module.
- **Affected Versions / Commit Hash**: The environment where the issue was discovered.
- **Proof of Concept (PoC)**: Clear, step-by-step reproduction instructions or exploit scripts.
- **Impact Assessment**: The potential impact if exploited by an attacker.
- **Suggested Fix**: Any recommended patches or mitigations (optional).

---

## Response & Disclosure Process

1. **Initial Acknowledgment**: We aim to acknowledge receipt of your vulnerability report within **48 hours**.
2. **Investigation & Triage**: We will verify the vulnerability, evaluate its severity (CVSS score), and keep you informed of our progress.
3. **Patch Development & Testing**: A fix will be developed in a private security advisory branch and thoroughly tested.
4. **Coordinated Disclosure**:
   - Once a patched release is published, we will publish the GitHub Security Advisory.
   - We follow responsible disclosure principles and request a 90-day embargo period from the initial report to give users adequate time to patch their installations.
   - You will be properly credited in the security advisory release notes (unless you prefer anonymity).

---

## Security Best Practices for Self-Hosters

If you are self-hosting Sakura AI, please follow these baseline recommendations:

- **Disable Debug Mode in Production**: Ensure `DEV_BOOTSTRAP` and debug flags are turned off.
- **Protect Credentials**: Keep `config/connection.json` and `.deploy/deployment.env` restricted with strict OS file permissions (`chmod 600`).
- **Rotate Secrets**: Regularly rotate GitHub App private keys, webhook secrets, database credentials, and session tokens.
- **Enable MFA**: Enforce Two-Factor Authentication (TOTP / Passkeys) in the WebUI Security Center for all administrative accounts.
- **Network Isolation**: Do not expose internal services (MySQL, Redis, ChromaDB, Updater unix socket) directly to the public internet; use firewalls, Docker networks, or Cloudflare Tunnels.
