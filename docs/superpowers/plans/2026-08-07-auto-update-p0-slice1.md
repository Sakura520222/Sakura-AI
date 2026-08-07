# Auto-Update P0 — Slice 1: 部署状态基石 + 版本只读展示

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 `.deploy/deployment.env` 权威部署状态机制，容器内能检测部署模式，navbar 常驻显示当前版本号（只读，无网络检查、无更新按钮）。

**Architecture:** `.deploy/deployment.env` 是 **runtime 文件（不入库）**，由 start.sh 首次启动时按当前模式生成。两个职责分离的 env 通道：compose **`env_file:`** 把 `SAKURA_DEPLOY_MODE` 注入**容器环境**（容器内 Settings 经 BaseSettings 读取）；compose CLI **`--env-file`** 驱动 compose 模型的 **`${SAKURA_AI_IMAGE}` 插值**。容器内 `GET /version/info` 路由从 Settings 读模式、传给纯函数 `build_version_info()`；navbar 用 Alpine.js fetch 显示版本号。

**Tech Stack:** FastAPI / pydantic-settings（BaseSettings 自动从环境变量读）/ Jinja2 + Alpine.js / bash（start.sh）/ Docker Compose `env_file` + CLI `--env-file` / pytest + bash 测试脚本。

**关联设计：** [2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md)（§5 部署模式检测、§9.5 镜像引用持久化、§13.5 navbar 版本区域）。

---

## ⚠️ 提交合规

按项目 `CLAUDE.md`：**执行者不得自主 `git commit`，也不允许子代理提交**。每个 task 完成后**暂停**，将变更暂存（`git add`），由用户审查后决定是否提交。计划中的 commit 信息仅为建议，**仅在用户明确授权后执行**。

## 关键不变量（Task 2 + Task 3 必须共同满足）

以下两个端到端验收必须在 Task 2/3 合并后双双通过：

```
A. source 启动
   → start.sh 写 deployment.env = SAKURA_DEPLOY_MODE=source
   → 容器内 SAKURA_DEPLOY_MODE=source（env_file 注入）
   → GET /version/info 返回 deployment_type=source
   → Settings.sakura_deploy_mode == "source"

B. 全新环境 prod 启动
   → start.sh 写 deployment.env = SAKURA_DEPLOY_MODE=image + SAKURA_AI_IMAGE=<实际值>
   → docker compose --env-file .deploy/deployment.env config 的 image 字段来自该文件
   → 容器内 SAKURA_DEPLOY_MODE=image
   → GET /version/info 返回 deployment_type=image
```

## env_file 与 --env-file 职责区分（重要）

Docker Compose 有两条独立的 env 通道，**不可混淆**：

| 通道 | 作用域 | 用途 |
|---|---|---|
| compose YAML 内 `env_file:` | **容器环境** | 把变量注入容器内 `os.environ`（本 slice：`SAKURA_DEPLOY_MODE` → 容器内 Settings） |
| CLI `--env-file <file>` | **Compose 模型插值** | 驱动 compose YAML 里的 `${VAR}` 替换（本 slice：`${SAKURA_AI_IMAGE}` → 真实镜像引用） |

参考：[Docker Compose 变量插值](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/)。本 slice 两者都用，指向同一个 `.deploy/deployment.env`，但职责不同。

## 范围与非目标

**本 slice 交付：**
- `.deploy/deployment.env` runtime 文件机制（start.sh 初始化 + compose `env_file` 注入容器 + CLI `--env-file` 驱动插值）
- 容器内 `Settings.sakura_deploy_mode`（BaseSettings 自动从 `SAKURA_DEPLOY_MODE` 读）
- `GET /version/info` API（`build_version_info` 纯函数 + route 从 Settings 读）
- navbar 右上角常驻当前版本号

**本 slice 明确不做：**
- UpdateChecker / GitHub Releases 检查（Slice 2）
- Host Updater daemon / UDS IPC（Slice 3）
- 更新 badge / 版本管理器页面（Slice 2+）
- `backend/core/deployment_env.py` Python writer 模块（Slice 3 updater 接管 fsync+rename writer 时建；本 slice 写入由 bash 完成）
- `${SAKURA_AI_IMAGE}` 的 digest 具体化（Slice 4 updater activate）

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/webui/routes/version.py` | Create | `build_version_info(deploy_mode: str)` 纯函数 + `GET /version/info` 路由（route 从 Settings 读后传参） |
| `backend/webui/routes/__init__.py` | Modify | 注册 version router |
| `backend/core/config.py` | Modify | Settings 加 `sakura_deploy_mode` 字段（BaseSettings 自动从 `SAKURA_DEPLOY_MODE` 读） |
| `tests/test_version_info.py` | Create | `build_version_info` 纯函数 TDD（强制参数，无环境依赖） |
| `.deploy/deployment.env.example` | Create | 示例模板（入库），供手动 compose up 参考 |
| `.deploy/deployment.env` | **Runtime（gitignore）** | 真实部署状态，由 start.sh 首次生成 |
| `.gitignore` | Modify | ignore `.deploy/*` 运行时文件，仅保留 `deployment.env.example` |
| `docker/docker-compose.yml` | Modify | 加 `env_file:`（注入容器） |
| `docker/docker-compose.prod.yml` | Modify | 加 `env_file:`（注入容器） |
| `start.sh` | Modify | `init_deployment_env()`（写实际值 + fsync）+ 提前到 detect_compose 前 + `detect_compose` 含 `--env-file` |
| `tests/test_init_deployment_env.sh` | Create | bash 测试：source start.sh，真正调用 `init_deployment_env`，三场景断言 |
| `backend/webui/templates/components/navbar.html` | Modify | 版本号区域（Alpine fetch `/version/info`） |

---

## Task 1: version 路由与 build_version_info 纯函数（TDD）

**Files:**
- Create: `backend/webui/routes/version.py`
- Modify: `backend/webui/routes/__init__.py`（注册 router）
- Modify: `backend/core/config.py`（加 Settings 字段）
- Test: `tests/test_version_info.py`

- [ ] **Step 1: 写失败测试**

Create `tests/test_version_info.py`:

```python
"""build_version_info pure-function coverage (Slice 1).

build_version_info 接收明确的 deploy_mode 参数，不读取任何环境变量——
真正的纯函数，route 层负责从 Settings 读后传参。
"""

from backend import __version__
from backend.webui.routes.version import build_version_info


def test_image_mode_marks_updater_not_connected():
    info = build_version_info("image")
    assert info["current_version"] == __version__
    assert info["deployment_type"] == "image"
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "updater_not_connected"
    assert info["update_available"] is None
    assert info["latest_version"] is None


def test_source_mode_marks_updater_not_available():
    info = build_version_info("source")
    assert info["deployment_type"] == "source"
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "source_updater_not_available"


def test_explicit_unknown_mode():
    info = build_version_info("unknown")
    assert info["deployment_type"] == "unknown"
    assert info["update_supported"] is False
    assert info["update_unsupported_reason"] == "unknown_deployment"


def test_invalid_mode_normalized_to_unknown():
    info = build_version_info("garbage")
    assert info["deployment_type"] == "unknown"
    assert info["update_unsupported_reason"] == "unknown_deployment"


def test_empty_string_normalized_to_unknown():
    info = build_version_info("")
    assert info["deployment_type"] == "unknown"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_version_info.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.webui.routes.version'`

- [ ] **Step 3: 加 Settings 字段**

Modify `backend/core/config.py`：在 `Settings` 类中（`model_config` 之后，建议靠近其它 `sakura_*` 字段）加：

```python
    # 部署模式标记。BaseSettings 无 env_prefix + case_sensitive=False，
    # 故自动从环境变量 SAKURA_DEPLOY_MODE 读取（由 compose env_file 注入）。
    # 值：image / source / unknown。
    sakura_deploy_mode: str = Field(
        "unknown",
        description="部署模式：image（镜像拉取）/ source（源码）/ unknown",
    )
```

> 确认：`Field` 已在 config.py 顶部导入（项目已大量使用 pydantic Field）。`model_config` 无 `env_prefix`、`case_sensitive=False`（见 config.py:32-36），所以字段名 `sakura_deploy_mode` 匹配环境变量 `SAKURA_DEPLOY_MODE`。

- [ ] **Step 4: 创建 version 路由**

Create `backend/webui/routes/version.py`:

```python
"""Version & deployment info route.

Slice 1：只读展示当前版本与部署模式（无 checker、无 updater）。
- update_supported 恒为 False（尚无 updater 连接）。
- update_available / latest_version 恒为 None（Slice 2 UpdateChecker 接入后填充）。

build_version_info 是纯函数（接收明确的 deploy_mode，不读环境变量）；
route 层从 Settings 读 SAKURA_DEPLOY_MODE 后传参，避免环境变量读取出现两个入口。
"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from backend import __version__
from backend.core.config import get_settings
from backend.webui.deps import require_auth

router = APIRouter(tags=["Version"])

_VALID_MODES = {"image", "source"}


def build_version_info(deploy_mode: str) -> dict:
    """构造版本与部署信息（纯函数）。

    Args:
        deploy_mode: 部署模式。非法值归一化为 "unknown"。

    Returns:
        版本与部署信息 dict。deployment_type 归一化为 image/source/unknown。
    """
    mode = deploy_mode if deploy_mode in _VALID_MODES else "unknown"

    update_supported = False
    if mode == "source":
        reason = "source_updater_not_available"
    elif mode == "image":
        reason = "updater_not_connected"  # Slice 4 接入 updater 后改判
    else:
        reason = "unknown_deployment"

    return {
        "current_version": __version__,
        "deployment_type": mode,
        "update_supported": update_supported,
        "update_unsupported_reason": reason,
        "update_available": None,  # Slice 2 填
        "latest_version": None,  # Slice 2 填
    }


@router.get("/version/info")
async def get_version_info(user: dict = Depends(require_auth)):
    """返回当前版本与部署模式（所有登录用户可读）。

    deploy_mode 从 Settings.sakura_deploy_mode 读取（BaseSettings 自动从
    SAKURA_DEPLOY_MODE 环境变量加载，由 compose env_file 注入）。
    """
    mode = get_settings().sakura_deploy_mode or "unknown"
    info = build_version_info(mode)
    return JSONResponse(
        info,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
```

- [ ] **Step 5: 注册路由**

Modify `backend/webui/routes/__init__.py`：

在 import 块（`from backend.webui.routes import (...)`）中，按字母序加入 `version`（放在 `vector_db` 之后）：

```python
from backend.webui.routes import (
    action_logs,
    activity_observability,
    agent_skills,
    agent_team,
    assetlinks,
    auth,
    billing,
    config,
    dashboard,
    issues,
    legal,
    logs,
    pr,
    queue,
    repos,
    sakura_memory,
    scans,
    security,
    settings,
    sse,
    star_aid,
    system_config,
    users,
    vector_db,
    version,
)
```

在文件末尾（`webui_router.include_router(activity_observability.router)` 之后）加：

```python
webui_router.include_router(version.router)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `python -m pytest tests/test_version_info.py -v`
Expected: 5 passed

- [ ] **Step 7: ruff 检查**

Run: `python run_ruff.py --check backend/webui/routes/version.py backend/core/config.py tests/test_version_info.py`
Expected: 无错误（本地 "拒绝访问" warning 可忽略）。

- [ ] **Step 8: 暂存变更（不提交）**

```bash
git add backend/webui/routes/version.py backend/webui/routes/__init__.py backend/core/config.py tests/test_version_info.py
```

**建议 commit 信息（待用户授权）：** `feat(version): add /version/info route with pure build_version_info`

---

## Task 2: deployment.env 文件机制 + compose env_file 集成

**Files:**
- Create: `.deploy/deployment.env.example`（入库模板）
- Modify: `.gitignore`
- Modify: `docker/docker-compose.yml`
- Modify: `docker/docker-compose.prod.yml`

> **真实 `.deploy/deployment.env` 不入库**（runtime 文件，由 Task 3 的 start.sh 首次生成）。

- [ ] **Step 1: 创建示例模板（入库）**

Create `.deploy/deployment.env.example`:

```env
# Sakura AI 部署状态示例（authoritative deployment state，见 auto-update 设计 §9.5）
#
# 真实文件 .deploy/deployment.env 是 runtime 文件（不入库），由 start.sh 首次启动时
# 按当前模式（source/image）生成。手动 docker compose up 前需先复制：
#   cp .deploy/deployment.env.example .deploy/deployment.env
# 或先跑一次 ./start.sh。
#
# start.sh prod 模式首次启动会写入 SAKURA_AI_IMAGE（实际值，非表达式）。
SAKURA_DEPLOY_MODE=source
```

- [ ] **Step 2: 更新 .gitignore（runtime 忽略，仅保留 example）**

Modify `.gitignore`，在末尾加：

```gitignore

# Sakura AI deploy runtime（仅保留 example 模板；真实 deployment.env 与 updater 运行时文件忽略）
.deploy/*
!.deploy/deployment.env.example
```

- [ ] **Step 3: 改 docker-compose.yml（加 env_file 注入容器）**

Modify `docker/docker-compose.yml`，在 `web` 服务的 `volumes:` 块之前加 `env_file:`:

```yaml
services:
  web:
    build:
      context: ..
      dockerfile: docker/Dockerfile
    image: sakura-ai
    container_name: sakura-ai
    restart: unless-stopped
    network_mode: host
    env_file:
      - ../.deploy/deployment.env
    volumes:
      - ../logs:/app/logs
      # ... 其余 volumes 不变
```

> `env_file` 路径相对 compose 文件目录（`docker/`），故 `../.deploy/deployment.env`。此通道只把 `SAKURA_DEPLOY_MODE` 注入**容器环境**（容器内 Settings 读取），**不**驱动 compose 模型插值（dev compose 本身无 `${...}` 需插值）。

- [ ] **Step 4: 改 docker-compose.prod.yml（加 env_file 注入容器）**

Modify `docker/docker-compose.prod.yml`，在 `web` 服务的 `environment:` 块之前加 `env_file:`:

```yaml
services:
  web:
    image: ${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}
    container_name: sakura-ai
    restart: unless-stopped
    ports:
      - "8000:8000"
    env_file:
      - ../.deploy/deployment.env
    environment:
      DATABASE_URL: mysql+asyncmy://sakura:sakura-ai@mysql:3306/sakura_ai
      REDIS_URL: redis://redis:6379/0
    # ... 其余不变
```

> **关键**：`image: ${SAKURA_AI_IMAGE:-...}` 的 `${...}` 插值**不**由 `env_file:` 驱动，而由 CLI `--env-file` 驱动（见 Task 3 Step 4，start.sh 调 `docker compose --env-file .deploy/deployment.env ...`）。`env_file:` 与 `--env-file` 职责不同，见计划开头的对照表。

- [ ] **Step 5: 验证 compose env_file 解析（需先有真实文件）**

手动创建真实文件以验证（Task 3 的 start.sh 也会生成）:

```bash
cp .deploy/deployment.env.example .deploy/deployment.env
```

Run:
```bash
docker compose -f docker/docker-compose.yml config 2>&1 | grep -i SAKURA_DEPLOY_MODE
```
Expected: 输出含 `SAKURA_DEPLOY_MODE: source`（容器环境注入成功）。

验证 `--env-file` 驱动插值（模拟 prod）:
```bash
# 把测试值写入 deployment.env 文件本身（而非 shell 环境变量），确保验证的是 --env-file 通道
cat > .deploy/deployment.env <<'EOF'
SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v9.9.9-testing
EOF

# env -u 清除 shell 同名变量，强制 compose 只能从 --env-file 取值
env -u SAKURA_AI_IMAGE \
  docker compose \
    --env-file .deploy/deployment.env \
    -f docker/docker-compose.prod.yml \
    config 2>&1 | grep 'image:.*sakura-ai'
```
Expected: web 服务的 `image:` 解析为 `ghcr.io/sakura520222/sakura-ai:v9.9.9-testing`（证明 `${SAKURA_AI_IMAGE}` 由 `--env-file` 驱动，而非 shell 变量）。> 注：把测试值写入文件 + `env -u` 清除 shell 变量，确保验证的是 `--env-file` 通道本身；真实值由 start.sh init 写入 deployment.env。

- [ ] **Step 6: 清理临时文件**

```bash
rm -f .deploy/deployment.env  # 真实文件由 start.sh 生成，不入库
```

确认 `git status` 不含 `.deploy/deployment.env`（已被 .gitignore 排除），仅 `.deploy/deployment.env.example` 待提交。

- [ ] **Step 7: 暂存变更（不提交）**

```bash
git add .deploy/deployment.env.example .gitignore docker/docker-compose.yml docker/docker-compose.prod.yml
```

**建议 commit 信息（待用户授权）：** `feat(deploy): add deployment.env runtime + compose env_file/--env-file split`

---

## Task 3: start.sh 初始化 deployment.env + detect_compose 注入 --env-file

**Files:**
- Modify: `start.sh`（`init_deployment_env()` + 提前初始化 + `detect_compose` 含 `--env-file`）
- Test: `tests/test_init_deployment_env.sh`

- [ ] **Step 1: 加 init_deployment_env 函数**

Modify `start.sh`，在"工具函数"区块（`wait_for_pid` 之后、`detect_compose` 之前）插入：

```bash
# ============================================================
# 部署状态初始化 / Deployment state bootstrap
# ============================================================

# deployment.env 权威部署状态文件路径（见 auto-update 设计 §9.5）
DEPLOYMENT_ENV_FILE="$DEPLOY_DIR/deployment.env"

# 首次启动时初始化部署状态：写入部署模式（source/image）与实际镜像引用。
# - 已存在则不覆盖（updater 或之前初始化已写入）。
# - 写实际值（非 ${...} 表达式）：deployment.env 记录"当时实际选择的镜像"。
# - durability：write temp → fsync(sync -d) → atomic mv，满足 spec §9.5。
# - digest 具体化（:latest → :vX.Y.Z@sha256:...）留给 Slice 4 updater activate。
init_deployment_env() {
    local mode="source"
    if $prod; then
        mode="image"
    fi

    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        return 0
    fi

    mkdir -p "$DEPLOY_DIR"
    local tmp
    tmp="$DEPLOY_DIR/.deployment.env.$$"
    {
        echo "# Sakura AI 部署状态（由 start.sh 初始化；updater 接管后以 atomic write 维护）"
        echo "SAKURA_DEPLOY_MODE=$mode"
        if $prod; then
            # 写实际值：解析当前 SAKURA_AI_IMAGE 环境变量，缺省用默认 latest
            local image="${SAKURA_AI_IMAGE:-ghcr.io/sakura520222/sakura-ai:latest}"
            echo "SAKURA_AI_IMAGE=$image"
        fi
    } > "$tmp"

    # durability：fsync 文件数据后再 atomic rename。
    # sync -d 是 GNU coreutils 的 file-data sync；不支持 -d 时 fallback 全局 sync；
    # 两者都不可用时静默降级（atomic mv 仍是主保护）。
    sync -d "$tmp" 2>/dev/null || sync 2>/dev/null || true
    mv "$tmp" "$DEPLOYMENT_ENV_FILE"
    info "已初始化部署状态: $DEPLOYMENT_ENV_FILE (mode=$mode)"
}
```

- [ ] **Step 2: 改 detect_compose 含 --env-file**

Modify `start.sh` 的 `detect_compose()` 函数（约 line 78-85），改为：

```bash
detect_compose() {
    local env_file_opt=""
    if [[ -f "$DEPLOYMENT_ENV_FILE" ]]; then
        env_file_opt="--env-file $DEPLOYMENT_ENV_FILE"
    fi
    if docker compose version &>/dev/null; then
        echo "docker compose $env_file_opt -f $COMPOSE_FILE"
    elif command -v docker-compose &>/dev/null; then
        echo "docker-compose $env_file_opt -f $COMPOSE_FILE"
    else
        echo ""
    fi
}
```

> 这样所有 `$COMPOSE up/down/build` 调用在 deployment.env 存在时自动带 `--env-file`，驱动 `${SAKURA_AI_IMAGE}` 插值。

- [ ] **Step 3: 在 do_start 中提前初始化（先于 detect_compose）**

Modify `start.sh` 的 `do_start()` 函数。当前顺序为（约 line 405-426）：`check docker` → `set COMPOSE_FILE（prod?）` → `COMPOSE=$(detect_compose)` → `mkdir -p logs "$DEPLOY_DIR" ...`。

在 `COMPOSE_FILE` 设置之后、`detect_compose` 调用之前，插入 `.deploy` 目录创建 + init。找到 `ok "环境检查完成"` 之前（detect_compose 调用之前）加：

```bash
    # 生产模式使用生产 compose（跳过本地构建，直接拉取 GHCR 镜像）
    if $prod; then
        COMPOSE_FILE="$PROD_COMPOSE_FILE"
        info "生产模式：使用生产 compose ($PROD_COMPOSE_FILE)"
    fi

    # 先初始化部署状态（detect_compose 依赖 deployment.env 是否存在来决定 --env-file）
    mkdir -p "$DEPLOY_DIR"
    init_deployment_env

    # Detect compose
    COMPOSE=$(detect_compose)
```

> 原 `mkdir -p logs "$DEPLOY_DIR" workplace Skills`（约 line 426）保留，`mkdir -p` 对已存在目录幂等。

- [ ] **Step 4: bash 语法检查**

Run: `bash -n start.sh`
Expected: 无输出（语法 OK）。

- [ ] **Step 5: 写 bash 测试（真正调用 init_deployment_env）**

Create `tests/test_init_deployment_env.sh`:

```bash
#!/usr/bin/env bash
# 测试 init_deployment_env（source start.sh，真正调用实现函数，不重写逻辑）
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# 跳过 main：start.sh 末尾有 _START_SH_SOURCED guard
export _START_SH_SOURCED=1
# shellcheck disable=SC1091
source "$SCRIPT_DIR/start.sh"
# source 后 start.sh 的 set -euo pipefail 生效；测试断言不触发退出
set +e

pass=0
fail=0
report() { if [ "$1" -eq 0 ]; then echo "[OK] $2"; pass=$((pass+1)); else echo "[FAIL] $2"; fail=$((fail+1)); fi; }
assert_contains() { local file="$1" needle="$2" name="$3"; grep -q "$needle" "$file" && report 0 "$name" || report 1 "$name (expected '$needle' in $file)"; }
assert_not_contains() { local file="$1" needle="$2" name="$3"; grep -q "$needle" "$file" && report 1 "$name (unexpected '$needle' in $file)" || report 0 "$name"; }

# 场景 1：source 首次初始化（prod=false）
W1=$(mktemp -d)
prod=false DEPLOY_DIR="$W1" DEPLOYMENT_ENV_FILE="$W1/deployment.env" init_deployment_env >/dev/null
assert_contains "$W1/deployment.env" "SAKURA_DEPLOY_MODE=source" "S1: source 模式写 source"
assert_not_contains "$W1/deployment.env" "SAKURA_AI_IMAGE=" "S1: source 模式不写镜像引用"

# 场景 2：image 首次初始化（prod=true，写实际值非表达式）
W2=$(mktemp -d)
prod=true SAKURA_AI_IMAGE="" DEPLOY_DIR="$W2" DEPLOYMENT_ENV_FILE="$W2/deployment.env" init_deployment_env >/dev/null
assert_contains "$W2/deployment.env" "SAKURA_DEPLOY_MODE=image" "S2: image 模式写 image"
assert_contains "$W2/deployment.env" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest" "S2: image 模式写实际镜像值（默认 latest）"
assert_not_contains "$W2/deployment.env" 'SAKURA_AI_IMAGE=${' "S2: 不写 shell 表达式"

# 场景 2b：image 首次初始化（自定义镜像值）
W2b=$(mktemp -d)
prod=true SAKURA_AI_IMAGE="ghcr.io/sakura520222/sakura-ai:v3.1.0" DEPLOY_DIR="$W2b" DEPLOYMENT_ENV_FILE="$W2b/deployment.env" init_deployment_env >/dev/null
assert_contains "$W2b/deployment.env" "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0" "S2b: 自定义镜像值被记录"

# 场景 3：已有状态不覆盖（即使模式不同）
W3=$(mktemp -d)
printf 'SAKURA_DEPLOY_MODE=image\nSAKURA_AI_IMAGE=custom:preserved\n' > "$W3/deployment.env"
prod=false DEPLOY_DIR="$W3" DEPLOYMENT_ENV_FILE="$W3/deployment.env" init_deployment_env >/dev/null
assert_contains "$W3/deployment.env" "custom:preserved" "S3: 已有状态不被覆盖"
assert_contains "$W3/deployment.env" "SAKURA_DEPLOY_MODE=image" "S3: 已有 mode 不被改回 source"

# 场景 4：atomic write 不残留临时文件
W4=$(mktemp -d)
prod=true DEPLOY_DIR="$W4" DEPLOYMENT_ENV_FILE="$W4/deployment.env" init_deployment_env >/dev/null
leftovers=$(find "$W4" -name '.deployment.env.*' 2>/dev/null | wc -l)
[ "$leftovers" -eq 0 ] && report 0 "S4: 无临时文件残留" || report 1 "S4: 残留 $leftovers 个临时文件"

echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
```

- [ ] **Step 6: 运行 bash 测试**

Run: `bash tests/test_init_deployment_env.sh`
Expected: 输出 9 行 `[OK]` + `结果: 9 passed, 0 failed`，退出码 0。

> 断言计数：S1=2、S2=3、S2b=1、S3=2、S4=1，共 9 个。

- [ ] **Step 7: 端到端验收（关键不变量 A & B）**

**验收 A（source 模式）：**
```bash
# 清理后用 dev compose 模拟 source 启动初始化
rm -rf .deploy/deployment.env
# 抽取 init 在 source 模式运行（prod=false）
bash -c 'export _START_SH_SOURCED=1; source ./start.sh; set +e; prod=false DEPLOY_DIR=".deploy" DEPLOYMENT_ENV_FILE=".deploy/deployment.env" init_deployment_env'
cat .deploy/deployment.env
# 期望：SAKURA_DEPLOY_MODE=source，无 SAKURA_AI_IMAGE
```

**验收 B（prod 模式 + --env-file 插值）：**
```bash
rm -rf .deploy/deployment.env
bash -c 'export _START_SH_SOURCED=1; source ./start.sh; set +e; prod=true SAKURA_AI_IMAGE="ghcr.io/sakura520222/sakura-ai:v3.0.0" DEPLOY_DIR=".deploy" DEPLOYMENT_ENV_FILE=".deploy/deployment.env" init_deployment_env'
cat .deploy/deployment.env
# 期望：SAKURA_DEPLOY_MODE=image + SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0

# 验证 --env-file 驱动 compose 插值
docker compose --env-file .deploy/deployment.env -f docker/docker-compose.prod.yml config 2>&1 | grep 'image:.*sakura-ai'
# 期望：image: ghcr.io/sakura520222/sakura-ai:v3.0.0（来自 deployment.env，非 :latest 默认）
```

> 若验收 B 的 image 仍为 `:latest`，说明插值未从 deployment.env 取值——检查 `--env-file` 路径与文件内容。

- [ ] **Step 8: 清理验收产物 + 暂存（不提交）**

```bash
rm -f .deploy/deployment.env  # runtime，不入库
git add start.sh tests/test_init_deployment_env.sh
```

**建议 commit 信息（待用户授权）：** `feat(start): bootstrap deployment.env with atomic fsync + --env-file compose interpolation`

---

## Task 4: navbar 版本区域（Alpine fetch /version/info）

**Files:**
- Modify: `backend/webui/templates/components/navbar.html`

- [ ] **Step 1: 定位插入点**

`backend/webui/templates/components/navbar.html` 的"右侧"区从 `<!-- 右侧：主题切换 + 用户信息 -->` 下方的 `<div class="flex items-center gap-2">` 开始。版本区域插在该 `<div>` 的第一个子元素位置（主题切换 `<button>` 之前）。

- [ ] **Step 2: 插入版本区域**

在 `<!-- 右侧：主题切换 + 用户信息 -->` 下方的 `<div class="flex items-center gap-2">` 之后、主题切换 `<button>` 之前，插入：

```html
        {# 版本号区域（所有登录用户可见；点击进版本管理器的能力在 Slice 2 提供） #}
        <div x-data="{ version: '', async loadVersion() { try { const r = await fetch('/version/info', { headers: { 'Accept': 'application/json' } }); if (r.ok) { const d = await r.json(); this.version = d.current_version || ''; } } catch (e) { /* 静默：版本号非关键 */ } } }"
             x-init="loadVersion()"
             class="hidden sm:flex items-center px-2 py-1 text-xs font-medium text-gray-500 dark:text-gray-400"
             title="Sakura AI 版本">
            <span>v<span x-text="version || '...'"></span></span>
        </div>
```

> - fetch 完成前显示 `v...`，完成后显示如 `v3.0.0`。
> - `hidden sm:flex`：移动端隐藏（navbar 空间紧张），桌面端显示。
> - Slice 2 在此区域扩展更新 badge（红点 + `latestVersion`）与点击进入版本管理器。

- [ ] **Step 3: 启动应用手动验证**

经 `./start.sh` 启动，登录后检查：

- 开发者工具 Network：`/version/info` 返回 200，body 含 `"current_version":"3.0.0"`、`"deployment_type":"source"`（dev 经 start.sh）、`"update_supported":false`、`"update_unsupported_reason":"source_updater_not_available"`。
- navbar 显示 `v3.0.0`（fetch 完成后占位 `...` 消失）。
- 控制台无 JS 报错。
- 验证 Settings 链路：`docker compose exec sakura-ai env | grep SAKURA_DEPLOY_MODE` 输出 `SAKURA_DEPLOY_MODE=source`（env_file 注入成功）。

> 若 `/version/info` 返回 401/403：确认已登录；若 404：确认 Task 1 Step 5 的 router 注册生效（重启）。若 `deployment_type` 为 `unknown`：确认 env_file 注入失败（检查 compose config）。

- [ ] **Step 4: 暂存变更（不提交）**

```bash
git add backend/webui/templates/components/navbar.html
```

**建议 commit 信息（待用户授权）：** `feat(webui): show current version badge in navbar`

---

## Self-Review（计划自检）

**1. 审查意见覆盖（7 项）：**

| # | 审查点 | 修正位置 |
|---|---|---|
| 1 | deployment.env 不入库 | Task 2 Step 1-2（`.example` 入库 + `.gitignore` 排除真实文件） |
| 2 | env_file vs --env-file 语义 | 计划开头对照表 + Task 2 Step 3-4（env_file 注入容器）+ Task 3 Step 2-3（detect_compose 含 --env-file 驱动插值） |
| 3 | 写实际值非表达式 | Task 3 Step 1（`local image="${SAKURA_AI_IMAGE:-...}"` 解析后写实际值）+ Step 5 场景 2b 断言 |
| 4 | atomic write + fsync | Task 3 Step 1（`sync -d` + mv）+ Step 5 场景 4 断言无残留 |
| 5 | build_version_info 真正纯函数 | Task 1（强制 `deploy_mode: str` 参数，route 从 Settings 读后传参） |
| 6 | 字段名统一 update_unsupported_reason | Task 1（路由 + 测试）+ spec §5.2 已同步 |
| 7 | §17.2 worktree staging | spec §17.2 已同步 |
| 额外 | Task 3 真正调用实现 | Task 3 Step 5（source start.sh，三场景 + 自定义镜像 + atomic 残留检查） |

**2. 占位符扫描：** 无 TBD/TODO；所有代码块完整可运行；bash 测试与端到端验收含完整命令与预期。

**3. 类型一致性：**
- `build_version_info(deploy_mode: str)` 签名在测试、route 一致；返回 dict 的 key 在测试、route、navbar fetch 一致。
- `update_unsupported_reason` 枚举值（`updater_not_connected` / `source_updater_not_available` / `unknown_deployment`）在 route、测试、spec §5.2 三处一致。
- `SAKURA_DEPLOY_MODE` 值域 `image|source|unknown` 在 Settings 字段、deployment.env、compose env_file、start.sh init、build_version_info 五处一致。
- `.deploy/deployment.env` 路径在 start.sh `DEPLOYMENT_ENV_FILE`、compose `env_file:`、CLI `--env-file`、.gitignore 四处一致。

**4. 范围检查：** Slice 1 聚焦"部署状态基石 + 版本只读展示"。Task 1（纯函数 TDD）+ Task 2/3（deployment state invariant，共同满足验收 A/B）+ Task 4（展示）。合并后即交付可运行的"navbar 版本号 + 部署模式检测"能力，无提前夹带 Checker/Updater。

**后续 slice 预告（本计划不含）：**
- **Slice 2** UpdateChecker + 版本管理器只读页 + 更新 badge
- **Slice 3** Host Updater daemon + UDS IPC + PyInstaller 打包 + DaemonBackend + `backend/core/deployment_env.py` Python writer（fsync+rename，接管 start.sh 的 bash 写入）
- **Slice 4** ImageAdapter + 状态机 + 端到端更新闭环 + digest 具体化 + manifest 门禁
