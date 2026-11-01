# 本地开发

本页介绍源码开发环境和常用验证命令。生产部署请看[部署指南](DEPLOYMENT.md)，协作流程见 [CONTRIBUTING.md](https://github.com/Sakura520222/Sakura-AI/blob/develop/CONTRIBUTING.md)。

## 支持的平台

源码开发支持 Windows 11 x86_64、Linux x86_64/arm64（glibc ≥ 2.28，非 musl）和 Apple Silicon macOS 14 及更新版本。

Intel Mac、macOS 13 及更早版本、Alpine/musl，以及 glibc 低于 2.28 的 Linux 无法安装当前依赖。上游没有提供对应 Python 3.14 的 onnxruntime wheel，也没有源码发行包；uv 和 pip 都受此限制。

项目当前使用 Python 3.14，暂不升级到 3.15。Dependabot 对 `/docker` 中的 Python 基础镜像忽略 3.15 及以上版本，继续接收 3.14 系列补丁和镜像 digest 更新。升级 Python 版本前，需要单独验证依赖与构建链兼容性。

## 安装与启动

推荐使用 uv。在仓库根目录运行：

```bash
git clone https://github.com/Sakura520222/Sakura-AI.git
cd Sakura-AI
uv sync
uv run python -m backend.main
```

`uv sync` 会创建 `.venv` 并安装项目依赖，包括以 editable 方式安装的 Updater。实际运行应用需要 MySQL 和 Redis；测试会模拟这些外部服务。

首次启动后打开 `http://localhost:8000/setup`。从本机私有文件 `config/.setup-token` 读取验证 Token（PowerShell：`Get-Content config/.setup-token`；POSIX：`cat config/.setup-token`），按向导完成配置。Token 不会写入日志；文件位置与权限要求见[部署指南的安全验证](DEPLOYMENT.md#安全验证)。

传统 pip 环境使用开发依赖清单：

```bash
python -m pip install -r requirements-dev.txt
python -m backend.main
```

`requirements-dev.txt` 包含运行依赖、`updater[dev]` 的 editable 安装及其测试额外依赖，还有 SQLite 测试驱动。完整测试套件需要这些依赖。

### Windows PowerShell

安装 Git for Windows 和 uv 后，在仓库根目录运行：

```powershell
uv sync --locked
uv run python -m backend.main
```

Windows 原生开发与通用测试不需要 WSL 或 Git Bash。Host Updater、sandboxd 和 Landlock/seccomp 本地进程隔离仍需要 Linux；部署平台要求见[部署指南](DEPLOYMENT.md)。

## 测试与代码检查

使用以下命令运行完整测试和只读代码检查：

```bash
uv run python -m pytest -q
uv run ruff check .
uv run python run_ruff.py --check
```

pip 环境中去掉 `uv run` 前缀，使用 `python -m pytest -q`、`ruff check .` 和 `python run_ruff.py --check`。`run_ruff.py` 不带 `--check` 时会修改并格式化文件。

## WebUI 开发

发布的源码包和 Git checkout 会使用与后端版本匹配的预构建前端资源。一般运行后端不需要安装 Node.js，也不需要手动构建前端。修改 React 前端时，请按[WebUI 开发指南](webui-2.md)安装依赖并运行 Vite；该指南也说明前端构建、API 类型和翻译校验流程。

## 热重载与重启

使用 `python -m backend.main` 启动时，应用子进程会对 `backend/` 中可热重载的改动执行模块级 reload，通常不重启进程。`backend/main.py`、数据库模型等进程级模块改动需要手动重启。Setup 完成和管理员重启操作会请求监督循环重新拉起应用进程。

此启动方式会在没有显式 `SAKURA_DEPLOY_MODE` 且没有 `SAKURA_BUILD_CHANNEL` 镜像构建标记时，将应用子进程识别为 `source`。显式部署模式优先；镜像构建标记存在时不会自动识别为源码模式。

## 调试 Setup Wizard

仅调试首次配置流程时，可使用隔离的开发启动脚本：

```bash
python scripts/dev_bootstrap.py
```

脚本默认把连接配置写入 `.sakura/dev/connection.json`，跳过后台任务，并将服务绑定到 `127.0.0.1:8000`。访问 `http://127.0.0.1:8000/setup` 开始调试。要清除该隔离配置并从 Setup 第一步重新开始：

```bash
python scripts/dev_bootstrap.py --reset
```

Setup Token 保存在当前连接配置文件旁的 `.setup-token`，例如默认位置 `.sakura/dev/.setup-token`。如果设置了 `SAKURA_CONNECTION_CONFIG_PATH`，Token 文件也会放在指定连接配置文件旁。Token 每次启动重新生成，不会写入日志；完成 Setup 后立即失效并尝试删除。开发配置可能包含数据库凭据，默认的 `.sakura/dev/` 路径已加入 `.gitignore`。

## English summary

Sakura AI source development supports Windows 11 x86_64, Linux x86_64/arm64 with glibc 2.28 or later (non-musl), and Apple Silicon macOS 14 or later. Upstream does not provide a Python 3.14 onnxruntime wheel or source distribution for Intel Macs, macOS 13 and earlier, Alpine/musl, or Linux with older glibc, so neither uv nor pip can install the current dependencies there.

The project stays on Python 3.14 for now. Dependabot ignores Python base-image versions 3.15 and newer under `/docker`, while continuing 3.14 patch and image digest updates. A Python upgrade requires separate dependency and build-chain validation. Use `uv sync` and `uv run python -m backend.main` for the recommended setup; pip users should install `requirements-dev.txt`, which includes the editable Updater and SQLite test driver. Run tests with `uv run python -m pytest -q` and use `uv run ruff check .` or `uv run python run_ruff.py --check` for read-only checks.

Published source packages and Git checkouts use matching prebuilt frontend assets. Contributors changing React should follow the [WebUI development guide](webui-2.md). `python -m backend.main` enables module-level hot reload for eligible `backend/` changes and supervises application restarts. It identifies the child process as `source` only when neither `SAKURA_DEPLOY_MODE` nor `SAKURA_BUILD_CHANNEL` is set.

Running the application requires MySQL and Redis. Open `/setup` on first launch and read the verification token from the private `config/.setup-token` file (`Get-Content config/.setup-token` in PowerShell or `cat config/.setup-token` on POSIX). Tokens never enter application logs. For isolated Setup Wizard debugging, run `python scripts/dev_bootstrap.py`; its connection config and Setup Token stay beside each other under `.sakura/dev/` by default. See the [Deployment Guide](DEPLOYMENT.md) for deployment details and [CONTRIBUTING_EN.md](https://github.com/Sakura520222/Sakura-AI/blob/develop/CONTRIBUTING_EN.md) for the contribution process.
