# Sakura AI 贡献指南 (Contributing Guide)

[English](CONTRIBUTING_EN.md) | **中文**

感谢你对 **Sakura AI** 项目的关注与支持！Sakura AI 是一个基于 AI 的 GitHub PR/Issue 代码审查与分析机器人。为了保证代码质量、架构一致性以及协作流程的规范顺畅，请在提交代码前仔细阅读本指南。

---

## 目录

- [行为准则](#行为准则)
- [Gitflow 分支规范](#gitflow-分支规范)
- [环境搭建与开发准备](#环境搭建与开发准备)
- [代码规范与架构契约](#代码规范与架构契约)
- [Commit 提交规范](#commit-提交规范)
- [测试与质量检查](#测试与质量检查)
- [Pull Request 提交流程](#pull-request-提交流程)
- [报告安全漏洞](#报告安全漏洞)

---

## 行为准则

我们致力于营造一个开放、包容、尊重与平等的开源协作环境。请与所有社区成员和贡献者保持友善沟通、理性讨论，并对不同观点抱持建设性态度。

---

## Gitflow 分支规范

本项目严格遵循标准 **Gitflow** 工作流：

- **`main`**：生产稳定分支，受保护分支，严禁直接 push。
- **`develop`**：日常集成主干分支，所有日常开发和特性的汇聚点，严禁直接 push。
- **`feature/<name>`**：特性分支。所有新功能、增强或常规修复都必须从 `develop` 分支检出，且 PR 的目标分支必须为 `develop`。
- **`release/x.y.z`**：版本发布分支。从 `develop` 检出，完成版本号提升、文档更新后 PR 合入 `main`。合入后由自动化或维护者将 `main` 回合（sync back）到 `develop`。
- **`hotfix/x.y.z`**：生产紧急修复分支。从 `main` 检出，修复验证完毕后 PR 合入 `main`，合入后必须同步回合到 `develop`。

```
main       ----------------------●-----------------● (v3.2.4)
                                 ^                 ^
release                          |--- release/x ---|
                                 |                 |
develop    ------●---------------●-----------------●
                 \              /
feature           \-- feature --/
```

---

## 环境搭建与开发准备

### 系统环境要求

- **Python 版本**：Python 3.14+
- **受支持开发平台**：
  - Linux x86_64 / aarch64（glibc ≥ 2.28，必须为 glibc 环境，Alpine 等 musl 系统不支持）
  - Apple Silicon macOS 14+
  - *说明*：由于底层依赖（如 `onnxruntime` / `chromadb`）在 Python 3.14 下仅发布了上述平台的预编译 wheel，暂不支持其他系统。

### 安装依赖

推荐使用 [`uv`](https://github.com/astral-sh/uv) 进行依赖管理，它会自动创建虚拟环境 `.venv`、以 editable 方式安装独立包 [updater/](updater/)，并锁定 `uv.lock`：

```bash
# 推荐方式 (uv)
uv sync
```

若使用传统 `pip` 方式：

```bash
python -m pip install -r requirements.txt
python -m pip install -e './updater[dev]'
```

> **注意**：
> - 根目录 [requirements.txt](requirements.txt) 是 pip 与 Docker 构建的权威依赖源；根 [pyproject.toml](pyproject.toml) 逐行镜像它，并由单元测试 [tests/test_uv_pyproject.py](tests/test_uv_pyproject.py) 强制校验。修改依赖时请务必保持两处同步。
> - 全仓采用 PEP 758 语法，要求 Python 3.14+。

### 常用运行命令

```bash
# 启动主应用 (开发监督循环模式，支持热重载)
python -m backend.main

# 使用 uvicorn 直启
python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000

# 运行 Setup Wizard 调试引导
python scripts/dev_bootstrap.py
```

---

## 代码规范与架构契约

为了保持代码库的高度整洁与可维护性，请遵循以下规范：

### 1. 编程语言与风格
- **Python 规范**：统一采用 4 空格缩进，函数与变量标识符全英文命名。
- **注释要求**：业务逻辑、复杂算法采用中英文双语或中文注释。
- **异步优先**：I/O 密集型操作、网络请求与数据库操作一律使用 `async`/`await`，严禁在异步路径中编写阻塞性 I/O。
- **日志记录**：统一使用 `loguru` 进行结构化日志输出，严禁随意使用 `print()`。

### 2. 代码静态检查 (Ruff)
本项目配置了严格的 Ruff 检查规则（针对 Python 3.14 优化）：

```bash
# 只读代码检查 (推荐在本地提交前运行)
python run_ruff.py --check
# 或者
ruff check .

# 自动格式化与修复
python run_ruff.py
```

### 3. 时间服务统一契约
- 应用内时间获取严禁直接使用 `datetime.now()` 或构造 naive datetime。
- 必须统一通过 [backend/core/time_service.py](backend/core/time_service.py) 获取 aware 时间戳，并在数据库模型中使用 [backend/models/time_types.py](backend/models/time_types.py) 定义的时间类型，以确保 DST（夏令时）折叠安全与时区一致。

### 4. 国际化与文案同步 (i18n)
- 任何在前端 WebUI 或接口返回的用户可见文案，必须同步更新：
  - [backend/webui/translations/zh-CN.yaml](backend/webui/translations/zh-CN.yaml)
  - [backend/webui/translations/en.yaml](backend/webui/translations/en.yaml)

### 5. 配置体系
- 运行时动态配置以数据库 `app_config` 表为最终存储，配置项默认值以 [backend/core/settings.py](backend/core/settings.py) 中的代码默认值为单一事实来源（Single Source of Truth）。
- 绝不要硬编码敏感凭证、密钥或开发调试 URL。
- [config/connection.json](config/connection.json) 与 `.deploy/deployment.env` 等运行态凭据文件绝对不得提交到 Git 仓库。

---

## Commit 提交规范

Git 提交信息必须遵循英文 **[Conventional Commits](https://www.conventionalcommits.org/)** 规范：

```
<type>(<scope>): <short description>

[optional body]

[optional footer(s)]
```

### 常用 Type 说明

- **`feat`**: 新功能 (Features)
- **`fix`**: 缺陷修复 (Bug fixes)
- **`docs`**: 仅文档更新 (Documentation)
- **`style`**: 不影响代码含义的格式调整 (空格、格式化、标点等)
- **`refactor`**: 既不修复错误也不添加功能的代码重构
- **`perf`**: 性能优化 (Performance improvements)
- **`test`**: 增加或修正测试用例
- **`chore`**: 构建过程、辅助工具、依赖项或日常维护变动

### 示例

```bash
feat(review): add support for semantic code diff analysis
fix(updater): resolve unix socket connection timeout on reload
docs(readme): update deployment instructions for Docker Compose
test(models): add unit tests for timezone-aware timestamp fields
```

---

## 测试与质量检查

在发起 Pull Request 之前，必须在本地确保全量测试通过：

```bash
# 运行完整测试套件 (包含主应用、updater、sandboxer)
python -m pytest -q

# 使用 uv 环境运行测试
uv run python -m pytest -q

# 运行特定模块测试
python -m pytest tests/test_main.py -q
python -m pytest updater/tests -q
python -m pytest sandboxer/tests -q
```

### 测试编写原则
1. **Mock 外部依赖**：单元测试严禁直连外部生产环境或网络服务，MySQL、Redis、GitHub API、AI 供应商（OpenAI/Gemini/Claude 等）和 ChromaDB 必须全部进行适当 Mock。
2. **测试命名**：测试文件以 `test_*.py` 命名，测试函数以 `test_*` 命名。

---

## Pull Request 提交流程

1. **Fork 本仓库** 到你个人的 GitHub 账号。
2. **克隆并检出分支**：
   ```bash
   git clone https://github.com/<your-username>/Sakura-AI.git
   cd Sakura-AI
   git checkout develop
   git checkout -b feature/your-feature-name
   ```
3. **编写代码与测试**：编写清晰的代码并添加对应的单元测试。
4. **运行本地校验**：
   ```bash
   python run_ruff.py --check
   python -m pytest -q
   ```
5. **提交变更**：使用英文 Conventional Commits 提交。
6. **推送到远程**：
   ```bash
   git push origin feature/your-feature-name
   ```
7. **创建 Pull Request**：
   - 目标分支（Base branch）**务必选择 `develop`**，严禁直接指向 `main`。
   - 详细填写 PR 模板，说明变更背景、核心改动点、验证方式及配置/数据库影响。
   - 如涉及 WebUI 界面改动，请在 PR 中附上截图或录屏。
8. **Code Review 与协作**：
   - 维护者和 Sakura AI 审查机器人会对 PR 进行 Review。
   - 针对审查意见进行修订并推送到原分支，CI 通过且 Review 同意后将被合并入 `develop`。

---

## 报告安全漏洞

如果你发现了安全漏洞或潜在安全风险，请**不要**直接在公开 Issue 中讨论。

请参考我们的 [安全策略 (SECURITY.md)](SECURITY.md) 或 [中文安全策略 (SECURITY_CN.md)](SECURITY_CN.md)，优先通过 **[GitHub Private Vulnerability Reporting](https://github.com/Sakura520222/Sakura-AI/security/advisories/new)** 或发送邮件至维护者邮箱 <sakura520222@outlook.com> 进行私密披露。
