# Auto-Update P0 — Slice 3c: Updater Binary 构建、发布与可信安装 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 使用 old-glibc 原生双架构环境构建 PyInstaller onefile updater binary，通过现有 Release owner 发布两个 Linux binary 与单一 `SHA256SUMS`，并由 `start.sh updater install` 在宿主机无 Python、binary 尚不存在时完成可信、并发安全、same-filesystem atomic rename 的首次安装；所有 pre-commit 失败保留旧 binary byte-for-byte unchanged，post-commit durability/final-safety failure 则明确报告新 inode 可能已安装。

**Architecture:** 首次 binary acquisition 属于 `start.sh` host bootstrap，而不是 `DaemonBackend.install()`：shell 先严格解析 app version/architecture、取得独立 install lock、经 HTTPS 下载目标 Release 的 binary 与 `SHA256SUMS`、校验并原子安装，再调用新 binary 的 `backend install` 完成现有 root/group/run-dir/state-dir bootstrap。PyInstaller 在目标架构的 native GitHub runner 内使用 `python:3.12-slim-bullseye` 构建 onefile 产物；`.github/workflows/updater-build.yml` 是由现有 `release-on-pr-merge.yml` 调用的 reusable workflow，绝不创建或编辑 Release，Release 的唯一 owner 保持不变。

**Tech Stack:** Bash、curl、sha256sum、flock、GNU coreutils、PyInstaller 6.21.0、pyinstaller-hooks-contrib 2026.6、Python 3.12、Debian bullseye（glibc 2.31）、GitHub Actions native amd64/arm64 runners、pytest、Ruff。

**关联设计：** [2026-08-07-auto-update-design.md](../specs/2026-08-07-auto-update-design.md) §4 ADR、§15、§16；本 plan 将 §16.2 固化为 bullseye native 双架构构建，并把 `update-manifest.json`、`min_upgrade_from` policy、manifest parsing/gate 延后至 Slice 4。

**前置提交：** Slice 3a `3623ea81`；Slice 3b `57f91d97`、`b0ca656c`、`5aa26d6f`、`ff3e3787`。

---

## 提交合规

按 `CLAUDE.md`：**执行者不得自主 `git commit`，也不允许子代理提交**。每个 Task 只运行 `git add` 暂存，由用户审查后决定是否提交。计划末尾的 Conventional Commit message 仅是建议，不授权执行提交。

## 已批准的 3c 边界

### 交付

- PyInstaller Linux onefile binary：amd64 + arm64。
- old-glibc 构建基线：`python:3.12-slim-bullseye`，glibc ceiling `<= 2.31`。
- native GitHub runners：`ubuntu-24.04` + `ubuntu-24.04-arm`；不使用 QEMU 或 cross-PyInstaller。
- Release Assets 仅包含：
  - `sakura-ai-updater-linux-amd64`
  - `sakura-ai-updater-linux-arm64`
  - `SHA256SUMS`
- `start.sh updater install` 在 updater binary 缺失时完成首次 acquisition，然后调用 binary `backend install`。
- shell 与 Python 双层 executable 安全检查。
- install lock、HTTPS-only 下载、严格 checksum 解析、同目录临时文件、same-filesystem atomic rename、post-commit directory metadata fsync、按阶段报告失败、pre-commit 失败保留旧 binary byte-for-byte unchanged。
- 运行中的 executable 被替换后不自动 restart；明确输出 restart-required 提示。
- `release-on-pr-merge.yml` 继续作为唯一 Release owner；`updater-build.yml` 仅被 `workflow_call` 调用并上传 assets。
- `ci.yml` 增加 updater 独立质量 job。
- 同步 README、README_EN 与 auto-update design spec。

### 不做

- Python `acquire.py` 或 Python 首次下载器。
- updater self-update policy / 自动寻找最新兼容 updater（P2）。
- `update-manifest.json`（Slice 4）。
- `min_upgrade_from` authoritative policy（Slice 4）。
- manifest parsing / compatibility gate / `release_visible != update_ready`（Slice 4）。
- ImageAdapter、`/v1/update`、`/v1/preflight`、`/v1/check`、`/v1/jobs/*`、Docker pull/activate、digest pinning、rollback、SourceAdapter、SystemdBackend。
- 自动 restart 当前 daemon；3c 只安装并警告。
- minisign/cosign 等独立签名信任根（P2）。3c 明确接受 binary 与 `SHA256SUMS` 同一 GitHub Release 信任根：校验提供传输/存储完整性，不抵御 Release 发布凭据整体失陷。

## 冻结契约

1. production resolver 仍是 binary-first；只有 `SAKURA_UPDATER_DEV=1` 才允许 Python fallback。
2. daemon binary 路径仍为 `.deploy/updater/sakura-ai-updater`。
3. daemon lifecycle（PID meta、PID reuse protection、readiness、start/stop/status/is-running）不重写。
4. transport layer 继续拥有 UDS pre-bind/chown/chmod/listener handoff。
5. `root:sakura-ai`、GID `9472`、`/run/sakura-ai` `0770`、socket `0660` 不变。
6. Web compose 的 `group_add: 9472`、只读 `/run/sakura-ai` mount、不挂 docker.sock 不变。
7. app version 与 updater version 继续分离：`backend.__version__` 选择 Release；`sakura_ai_updater.__version__` 只描述 updater 自身。
8. `DaemonBackend.install()` 继续只做 host bootstrap，不承担首次网络下载。

## Correctness 与安全不变量

### Version resolution

`resolve_updater_app_version()` 是 **deployment-mode-aware** 的：

1. 读取 `SAKURA_DEPLOY_MODE`（image/source）；缺失或非 image/source → fail-closed，不下载。
2. `SAKURA_DEPLOY_MODE=image`：
   - concrete `SAKURA_AI_IMAGE=:vX.Y.Z`（可带 `@sha256:` + 64 位 hex digest）→ image version 权威。
   - `:latest` / 无 tag → 回退到 `backend/__init__.py` 的 `__version__`。
   - 镜像部署时 host checkout 版本与运行版本可能不同，image version 始终权威——二者不构成 conflict。
3. `SAKURA_DEPLOY_MODE=source`：`backend/__init__.py` 的精确 `__version__ = "X.Y.Z"` 权威。
4. 最终版本用 `^[0-9]+\.[0-9]+\.[0-9]+$` 校验；3c 不支持 prerelease/build metadata（正式 Release/tag 契约是 `vX.Y.Z`）。
5. 无法确定具体版本 → fail-closed，不猜测 `latest`。

### Platform mapping

- `uname -s` 必须是 `Linux`。
- `x86_64|amd64` → `sakura-ai-updater-linux-amd64`。
- `aarch64|arm64` → `sakura-ai-updater-linux-arm64`。
- 其他 OS/arch 明确失败，绝不把原始值拼入 URL。

### Executable path safety

production shell resolver 在**首次 exec 之前**要求：

- `[[ -f "$UPDATER_BINARY" ]]`
- `[[ ! -L "$UPDATER_BINARY" ]]`
- executable owner UID 为 `0`
- group/other write bits 均为 `0`
- `[[ -x "$UPDATER_BINARY" ]]`

任一不满足时不得执行 binary。若 binary 路径存在但不安全，production 必须 fail-closed，不能把它当“缺失”后直接覆盖；`SAKURA_UPDATER_DEV=1` 仍可走显式 dev Python。Python `_resolve_executable()` 以 `os.lstat()` + `stat.S_ISREG()` + production owner/mode 校验做 defense-in-depth。

### Trusted acquisition

固定顺序：

```text
require root
→ prepare state_dir：首次 mkdir/chown/chmod（root:root 0700）；已存在则 owner 必须 root + group/other 不可写 → chmod 0700 harden；non-root / group-other-writable / symlink → fail-closed
→ acquire .deploy/updater/install.lock（flock -n）
→ resolve version + architecture
→ mktemp（必须在 state_dir 内，0600）
→ curl binary + SHA256SUMS 到不同 temp files
→ strict parse SHA256SUMS：目标 asset 恰好一条
→ pre-commit：下载、checksum、`chmod 0700`、temp file fsync、temp safety validation
→ same-filesystem atomic `mv -f` 到最终路径（此 rename 是 commit point）
→ post-commit：`sync "$UPDATER_STATE_DIR"`（GNU coreutils 对目录 fd 做 metadata fsync；不使用 `sync -d`/`sync --data`）
→ rename 后仅确认 final path regular/non-symlink/root-owned/non-group-or-other-writable/executable
→ 清 checksum temp + 释放 lock
→ 仅在 post-commit durability 与 final confirmation 均成功后调用 binary backend install
→ 若 daemon 在安装前已运行，输出 restart-required；不自动 stop/start
```

下载 URL 固定为：

```text
https://github.com/Sakura520222/Sakura-AI/releases/download/v${version}/${asset}
https://github.com/Sakura520222/Sakura-AI/releases/download/v${version}/SHA256SUMS
```

pre-commit 阶段（download、checksum、`chmod 0700`、temp file fsync、temp safety validation）任一失败都必须令旧 final binary byte-for-byte unchanged；此阶段不得触碰最终路径。same-filesystem atomic `mv -f` 是 commit point。post-commit directory fsync 必须使用 GNU coreutils `sync "$UPDATER_STATE_DIR"` 的目录 metadata fsync 语义；不得使用 data-only `sync -d` 形式。若 post-commit `sync "$UPDATER_STATE_DIR"` 失败，必须报告 durability failure，不能声称旧 binary 未变，也不得自动调用 backend install/start：此时新 inode 可能已经安装。final inode metadata/path safety 尽量在 rename 前对 root-owned temp 完成；rename 后只做确认，若确认失败，错误必须描述为 post-commit final safety failure，不得描述为“old binary untouched”。只有 post-commit durability 与 rename 后确认均成功，才允许调用 `backend install`；随后流程如需启动 daemon，必须以该成功结果为前提。

```bash
TMPDIR="$UPDATER_STATE_DIR/tmp" "$UPDATER_BINARY" backend "$@"
```

执行，降低宿主 `/tmp` 为 `noexec` 时 PyInstaller onefile 启动失败概率。TMPDIR 仅改变解包位置，不改变 daemon state、UDS 或 PID meta 路径。

### Running executable replacement

Linux rename 只替换目录项，已运行 daemon 继续使用旧 inode，属于安全行为。`updater install` 在 acquisition 前记录 `is-running` 结果；安装成功后若先前正在运行，打印明确提示并成功退出：binary 已安装，但需管理员显式 `./start.sh updater stop && ./start.sh updater start` 应用。3c 不新增自动 restart action。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `start.sh` | Modify | 首次 acquisition、版本/arch 解析、root/path gate、flock、HTTPS 下载、strict checksum、same-filesystem atomic rename、directory metadata fsync、TMPDIR、restart-required |
| `tests/test_start_sh_updater.sh` | Modify | shell resolver/acquisition/version/arch/checksum/atomic failure/TMPDIR 行为测试 |
| `updater/src/sakura_ai_updater/backends/daemon.py` | Modify | Python defense-in-depth executable `lstat`/owner/mode 校验；不下载 |
| `updater/tests/test_daemon_backend.py` | Modify | symlink/owner/mode 防护测试；保持 install bootstrap 契约 |
| `updater/build/sakura-ai-updater.spec` | Create | PyInstaller onefile 配置与 updater package hidden imports |
| `updater/build/requirements-build.txt` | Create | pin PyInstaller 与 hooks-contrib |
| `updater/build/build.sh` | Create | 在 bullseye/Python 3.12 内构建、build-container smoke、final outer-ELF GLIBC gate、命名；不依赖 docker socket |
| `updater/build/check_glibc.py` | Create | 只解析 final onefile outer ELF/bootloader 的 `GLIBC_X.Y` Version needs，最大值必须 `<=2.31`；checker 不读取 embedded ELF |
| `updater/build/run-fresh-runtime-smoke.sh` | Create | 在干净 pinned bullseye runtime 中创建 `state/tmp`（均 `0700`），导出 controlled `TMPDIR`，复制 mounted artifact 为 root-owned `0700` libexec binary，运行 `--version`、`backend install/start/status/is-running`、UDS health、`backend stop` 与停止后的 `is-running` smoke，清理 root-compatible 临时路径；apt 安装 `curl passwd` 失败归类为 harness infrastructure failure |
| `updater/tests/test_build_config.py` | Create | spec/build script/pin/onefile/outer-ELF ceiling/fresh-runtime smoke 静态契约测试 |
| `.github/workflows/updater-build.yml` | Create | `workflow_call` reusable native matrix + build-container outer gate + fresh-runtime smoke + artifact fan-in + single-writer SHA256SUMS upload |
| `.github/workflows/ci.yml` | Modify | updater 独立 install/pytest/ruff job |
| `.github/workflows/release-on-pr-merge.yml` | Modify | source assets 定向删除/覆盖；source job 成功后调用 reusable updater build；保持唯一 Release owner |
| `tests/test_release_workflows.py` | Create | YAML 解析 DAG、runner、single writer、asset ownership、manifest exclusion 静态测试 |
| `docs/superpowers/specs/2026-08-07-auto-update-design.md` | Modify | 修正 §16.2/§16.5 与 Slice 3c/4 边界 |
| `README.md` | Modify | production updater install/校验/权限/显式 restart 文档 |
| `README_EN.md` | Modify | 英文同步 |

---

## Task 1: `start.sh` trusted bootstrap acquisition + 双层 executable 防护

**Files:**
- Modify: `start.sh:119-177`
- Modify: `tests/test_start_sh_updater.sh`
- Modify: `updater/src/sakura_ai_updater/backends/daemon.py:245-262`
- Modify: `updater/tests/test_daemon_backend.py:369-418,889-902`

- [ ] **Step 1: 扩展 bash 测试夹具，先写失败的 resolver/path safety 测试**

在 `tests/test_start_sh_updater.sh` 中保留现有 S1-S7，并新增隔离 fake `stat`/binary 的测试。测试必须覆盖：安全 root-owned regular executable 被执行；symlink、非 root owner、group-write、other-write、不可执行路径在 production 模式均不被 exec 并返回安全拒绝；同一 unsafe/non-exec path 在 `SAKURA_UPDATER_DEV=1` 下不得执行该路径而必须调用 Python；不安全路径在 production 存在时返回非零且不覆盖；`SAKURA_UPDATER_DEV=1` 对缺失 binary 仍调用 Python。

核心断言形态：

```bash
updater_binary_is_safe() { return 1; }
: > "$FAKE_LOG"
updater_backend status 2>/dev/null
rc=$?
[ "$rc" -ne 0 ] && ! grep -q '^BINARY:' "$FAKE_LOG"
```

由于 Windows Git Bash 无可靠 root UID，shell unit test 对 owner/mode 使用可注入 helper（例如 `updater_binary_owner_uid()` / `updater_binary_mode()`）覆写；真实 Linux 验收再使用系统 `stat`。新增 acquisition failure matrix：在 pre-commit 的 curl/checksum/chmod/temp-fsync/temp-safety 任一点注入失败，断言旧 final binary `sha256sum` 与注入前完全一致、最终路径没有半成品、temp 已清理且未调用 `backend install/start`；另注入 post-commit directory fsync 与 rename 后 final safety failure，分别断言错误为 durability/final-safety failure、记录“new inode may already be installed”语义、旧 binary 不作 unchanged 承诺，且两者均不调用 `backend install/start`。

- [ ] **Step 2: 运行 resolver 测试，确认红灯**

Run:

```bash
bash tests/test_start_sh_updater.sh
```

Expected: 新增 path-safety 用例失败；现有 S1-S7 仍通过。

- [ ] **Step 3: 在 shell resolver 中实现 exec 前安全校验与受控 TMPDIR**

在 `start.sh` 增加：

```bash
updater_binary_owner_uid() {
    stat -c '%u' "$1"
}

updater_binary_mode() {
    stat -c '%a' "$1"
}

updater_binary_is_safe() {
    local binary="$1" owner mode
    [[ -f "$binary" && ! -L "$binary" && -x "$binary" ]] || return 1
    owner=$(updater_binary_owner_uid "$binary") || return 1
    mode=$(updater_binary_mode "$binary") || return 1
    [[ "$owner" == "0" ]] || return 1
    (( (8#$mode & 8#022) == 0 )) || return 1
}
```

`updater_backend()` 的 resolver 顺序必须把显式 dev fallback 放在 unsafe-path production 拒绝之前，但不降低 production gate：

```bash
if updater_binary_is_safe "$binary"; then
    local runtime_tmp="$UPDATER_STATE_DIR/tmp"
    mkdir -p "$runtime_tmp" || return 1
    chmod 0700 "$runtime_tmp" || return 1
    TMPDIR="$runtime_tmp" "$binary" backend "$@"
elif [[ "${SAKURA_UPDATER_DEV:-0}" == "1" ]]; then
    "${SAKURA_UPDATER_PYTHON:-python3}" -m sakura_ai_updater backend "$@"
elif [[ -e "$binary" || -L "$binary" ]]; then
    fail "refusing unsafe updater executable: $binary"
    return 126
else
    fail "updater executable not installed: $binary"
    return 127
fi
```

生产路径由后续 acquisition root gate 保证 TMPDIR 为 root-owned；resolver 额外检查现存 TMPDIR 不是 symlink，并拒绝不安全目录。上述 dev fallback 仅对 shell resolver 的显式开发模式生效，不改变 Python `_resolve_executable()` production owner/mode defense-in-depth。

`updater_binary_is_safe()` 真 → 使用 safe binary；否则 `SAKURA_UPDATER_DEV=1` → 只调用 Python，即使 unsafe/non-executable path 存在也不得 exec/覆盖该 path；无 dev override 且 path 存在或是 symlink → production fail-closed 126；无 dev override 且 path 缺失 → 127。

- [ ] **Step 4: 运行 resolver 测试，确认绿灯**

Run:

```bash
bash tests/test_start_sh_updater.sh
```

Expected: 所有 resolver/path/TMPDIR 用例通过。

- [ ] **Step 5: 写 version/arch resolution 的失败测试**

新增测试覆盖：

```text
deployment.env=:v3.0.0 + backend=3.0.0 → 3.0.0
deployment.env=:latest + backend=3.0.0 → 3.0.0
deployment.env=:v3.1.0 + backend=3.0.0 → fail
只有 deployment.env=:v3.0.0 → 3.0.0
只有 backend=3.0.0 → 3.0.0
两者都无 concrete version → fail
非 Linux / unsupported arch → fail
x86_64 → linux-amd64
aarch64 → linux-arm64
```

测试通过覆写 `UPDATER_DEPLOYMENT_ENV_FILE`、`UPDATER_BACKEND_VERSION_FILE` 与 `updater_uname_s/m` helper 隔离宿主环境。

- [ ] **Step 6: 运行 version/arch 测试，确认红灯**

Run:

```bash
bash tests/test_start_sh_updater.sh
```

Expected: 缺少 `resolve_updater_app_version` / `resolve_updater_asset` 导致新增用例失败。

- [ ] **Step 7: 实现严格 version/arch resolution**

在 `start.sh` 实现以下完整边界；解析失败返回非零并把原因写到 stderr，stdout 只输出成功值，便于 command substitution：

```bash
updater_uname_s() { uname -s; }
updater_uname_m() { uname -m; }

resolve_updater_app_version() {
    local image_version="" package_version="" line image version

    if [[ -f "$UPDATER_DEPLOYMENT_ENV_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            [[ "$line" == SAKURA_AI_IMAGE=* ]] || continue
            image=${line#SAKURA_AI_IMAGE=}
            if [[ "$image" =~ ^ghcr\.io/sakura520222/sakura-ai:v([0-9]+\.[0-9]+\.[0-9]+)(@sha256:[0-9a-f]{64})?$ ]]; then
                image_version=${BASH_REMATCH[1]}
            fi
            break
        done < "$UPDATER_DEPLOYMENT_ENV_FILE"
    fi

    if [[ -f "$UPDATER_BACKEND_VERSION_FILE" ]]; then
        while IFS= read -r line || [[ -n "$line" ]]; do
            if [[ "$line" =~ ^__version__[[:space:]]*=[[:space:]]*\"([0-9]+\.[0-9]+\.[0-9]+)\"[[:space:]]*$ ]]; then
                package_version=${BASH_REMATCH[1]}
                break
            fi
        done < "$UPDATER_BACKEND_VERSION_FILE"
    fi

    if [[ -n "$image_version" && -n "$package_version" && "$image_version" != "$package_version" ]]; then
        fail "updater version signals disagree: image=$image_version package=$package_version"
        return 1
    fi
    version=${image_version:-$package_version}
    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        fail "cannot determine concrete Sakura AI version"
        return 1
    fi
    printf '%s\n' "$version"
}

resolve_updater_asset() {
    [[ "$(updater_uname_s)" == "Linux" ]] || {
        fail "updater binary install supports Linux only"
        return 1
    }
    case "$(updater_uname_m)" in
        x86_64|amd64) printf '%s\n' 'sakura-ai-updater-linux-amd64' ;;
        aarch64|arm64) printf '%s\n' 'sakura-ai-updater-linux-arm64' ;;
        *) fail "unsupported updater architecture: $(updater_uname_m)"; return 1 ;;
    esac
}
```

定义默认路径：

```bash
UPDATER_DEPLOYMENT_ENV_FILE="${UPDATER_DEPLOYMENT_ENV_FILE:-$DEPLOYMENT_ENV_FILE}"
UPDATER_BACKEND_VERSION_FILE="${UPDATER_BACKEND_VERSION_FILE:-backend/__init__.py}"
```

实现只读取固定文件、逐行解析固定 key，不 `source deployment.env`、不 `eval`、不把文件内容作为 shell 代码执行。`:latest` 不匹配 concrete image signal，只有 package version 可用时自然回退。

- [ ] **Step 8: 写 acquisition 失败路径与原子性测试**

通过覆写 helper（`updater_curl`、`updater_sha256`、`updater_flock`、`updater_sync_temp`、`updater_sync_state_dir`）测试：

- root gate 早于 mkdir/download；
- install lock busy 立即失败且不下载；
- curl argv 精确含 `--fail --location --proto =https --proto-redir =https`；
- asset URL 精确绑定 `v${version}`，不用 latest；
- binary 404、checksum 404、checksum 缺失/重复/非 64 hex、hash mismatch 全失败；
- CRLF `SHA256SUMS` 正确接受；
- Content-Length 存在且不符失败，缺失不单独失败；
- pre-commit failure matrix（download/checksum/chmod/temp fsync/temp safety）必须逐一断言旧 final binary byte-for-byte unchanged；post-commit fsync/final safety 注入必须逐一断言不得作 unchanged 声明；
- 成功调用顺序为 lock→download→verify→chmod temp→temp safety validation→sync temp→same-filesystem atomic rename（commit point）→`sync "$UPDATER_STATE_DIR"`（目录 metadata fsync）→rename 后 final safety confirmation→backend install；
- post-commit directory fsync 注入失败时，断言输出 durability failure/new inode may already be installed，不断言 old binary unchanged，且不调用 `backend install/start`；
- rename 后 final safety confirmation 注入失败时，断言输出 post-commit final safety failure，不断言 old binary unchanged，且不调用 `backend install/start`；
- daemon 原本 running 时，仅在上述所有 post-commit gate 成功后输出 restart-required 且不 stop/start。

严格 checksum 解析不得用 `sha256sum -c` 直接消费远端文件名；逐行匹配 `64-hex + optional * + exact asset name`，目标 asset 必须恰好一条，再对 temp binary 自行计算 hash。

- [ ] **Step 9: 运行 acquisition 测试，确认红灯**

Run:

```bash
bash tests/test_start_sh_updater.sh
```

Expected: `install_updater_binary` 尚不存在或行为不满足断言。

- [ ] **Step 10: 实现 host bootstrap acquisition 并接入两个入口**

实现 `install_updater_binary()` 与 `cmd_updater_install()`：

- `id -u == 0`；
- state_dir 预先存在时必须是 non-symlink、root-owned、group/other 不可写；不存在时 root 创建后 `chown 0:0`、`chmod 0700`；目录 metadata 的 state_dir 创建/权限变更完成后，按 GNU coreutils `sync "$UPDATER_STATE_DIR"` 语义持久化；
- `flock -n` 持有 `$UPDATER_STATE_DIR/install.lock` 贯穿下载至 final verification；
- temp 由 `mktemp "$UPDATER_STATE_DIR/.updater-download.XXXXXX"` 与 `mktemp "$UPDATER_STATE_DIR/.updater-checksums.XXXXXX"` 创建；函数开头初始化两个变量，`trap 'rm -f -- "$binary_tmp" "$sums_tmp"' RETURN`（或单一 cleanup 函数覆盖每个 return），成功 rename 后清空 `binary_tmp`，确保不会误删 final path；
- final inode metadata/path safety 尽量在 rename 前对 root-owned temp 完成：temp 设为 `0700`、确认 regular/non-symlink/root-owned/non-group-or-other-writable/executable，并对 temp 调用 `sync "$binary_tmp"`；same-filesystem `mv -f` 是 commit point；
- rename 后调用 GNU coreutils `sync "$UPDATER_STATE_DIR"`，这是目录 metadata fsync；不得改写为 data-only `sync -d`，因为其是 fdatasync 语义，不适合目录 metadata。若该 post-commit fsync 失败，报告 durability failure、说明新 inode 可能已经安装，不声称旧 binary 未变，也不执行 `backend install/start`；
- fsync 成功后仅确认 final path regular/non-symlink/root-owned/non-group-or-other-writable/executable；该确认失败报告 post-commit final safety failure，不把它描述为旧 binary untouched，也不执行 `backend install/start`；
- 仅在上述 post-commit gates 成功后执行：

```bash
updater_backend install \
    --state-dir "$UPDATER_STATE_DIR" \
    --socket-path "$UPDATER_SOCKET_PATH"
```

- 安装成功后才允许 `backend install` 返回成功；

改接线：

```text
cmd_updater install → cmd_updater_install
ensure_updater_running：
  binary 安全存在 → binary backend install
  binary 缺失 → cmd_updater_install
  binary 不安全 → fail，不覆盖
  install 成功 → updater_backend start
```

避免 bootstrap 循环：binary 不存在时绝不先调用 `updater_backend install`。

- [ ] **Step 11: 实现 Python defense-in-depth executable 检查**

在 `daemon.py` 新增纯 helper：

```python
def _is_safe_executable(path: str, *, require_root_owner: bool) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    if st.st_mode & 0o022:
        return False
    if require_root_owner and st.st_uid != 0:
        return False
    return bool(st.st_mode & 0o111)
```

`_resolve_executable()` 在 production 使用 `require_root_owner=True`；dev binary path 可测试但不降低 production gate。保持 `SAKURA_UPDATER_DEV=1` 是唯一 Python fallback。

- [ ] **Step 12: 扩展并运行 DaemonBackend 测试**

测试至少包括：regular safe file 接受；symlink 拒绝；group/other writable 拒绝；production 非 root owner 拒绝（patch `os.lstat`）；无 binary + dev 保持 Python fallback；`install()` 仍只做 bootstrap、不下载。

Run:

```bash
uv run --project updater pytest updater/tests/test_daemon_backend.py -q
```

Expected: PASS。

- [ ] **Step 13: 运行 Task 1 targeted validation**

Run:

```bash
bash -n start.sh
bash tests/test_start_sh_updater.sh
uv run --project updater pytest updater/tests/test_daemon_backend.py -q
uv run ruff check start.sh updater/src/sakura_ai_updater/backends/daemon.py updater/tests/test_daemon_backend.py
```

Expected: bash syntax/test、pytest、Ruff 全通过（Ruff 对 shell 参数会忽略或只传 Python 文件；若当前 runner 不接受 shell path，则执行 `uv run ruff check updater/src/sakura_ai_updater/backends/daemon.py updater/tests/test_daemon_backend.py`）。

- [ ] **Step 14: 暂存 Task 1（不得 commit）**

```bash
git add start.sh tests/test_start_sh_updater.sh \
  updater/src/sakura_ai_updater/backends/daemon.py \
  updater/tests/test_daemon_backend.py
```

Suggested commit message（仅建议）：`feat(start): securely bootstrap updater binary`

---

## Task 2: PyInstaller onefile + bullseye native 双架构构建

**Files:**
- Create: `updater/build/sakura-ai-updater.spec`
- Create: `updater/build/requirements-build.txt`
- Create: `updater/build/build.sh`
- Create: `updater/build/check_glibc.py`
- Create: `updater/build/run-fresh-runtime-smoke.sh`
- Create: `updater/tests/test_build_config.py`
- Modify: `.gitignore`

- [ ] **Step 1: 写失败的 build contract 测试**

`updater/tests/test_build_config.py` 用 pathlib 读取构建文件与 reusable workflow，断言：

```python
from pathlib import Path

BUILD = Path(__file__).parents[1] / "build"
WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "updater-build.yml"


def test_pyinstaller_build_is_onefile_and_pinned():
    spec = (BUILD / "sakura-ai-updater.spec").read_text(encoding="utf-8")
    requirements = (BUILD / "requirements-build.txt").read_text(encoding="utf-8")
    assert "COLLECT(" not in spec
    assert "EXE(" in spec
    assert "pyinstaller==6.21.0" in requirements
    assert "pyinstaller-hooks-contrib==2026.6" in requirements


def test_build_targets_bullseye_python_312_and_two_compatibility_gates():
    script = (BUILD / "build.sh").read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert (
        "python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db"
        in script
    )
    checker = (BUILD / "check_glibc.py").read_text(encoding="utf-8")
    assert "(2, 31)" in checker
    assert "outer" in checker.lower()
    assert "CArchive" in checker
    assert (
        "debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792"
        in workflow
    )
    assert "linux/amd64" in workflow and "linux/arm64/v8" in workflow
    assert "--version" in workflow and "backend status" in workflow
    assert "run-fresh-runtime-smoke.sh" in workflow
    helper = (BUILD / "run-fresh-runtime-smoke.sh").read_text(encoding="utf-8")
    assert 'export TMPDIR="$runtime_tmp"' in helper
    assert 'install -d -m 0700 "$state_dir" "$runtime_tmp"' in helper
    assert 'install -m 0700 "$mounted_binary" "$installed_binary"' in helper
    assert 'backend install "${common_args[@]}"' in helper
    assert 'backend start "${common_args[@]}"' in helper
    assert 'backend status "${common_args[@]}"' in helper
    assert 'backend is-running "${common_args[@]}"' in helper
    assert 'backend stop "${common_args[@]}"' in helper
    assert (
        'if "$installed_binary" backend is-running "${common_args[@]}"; then' in helper
    )
    assert 'curl --unix-socket "$socket_path" http://localhost/v1/health' in helper
    assert (
        "curl --unix-socket /run/sakura-ai/updater.sock http://localhost/v1/health"
        in workflow
    )
```

并断言产物名只允许 `sakura-ai-updater-linux-amd64|arm64`，不存在 `update-manifest.json`。额外要求 build contract 同时读取 `build.sh` 与 reusable workflow：前者必须执行 final onefile outer ELF/bootloader static gate，后者必须在 build container 成功退出后，把同一 final onefile 放入 pinned、fresh `debian:bullseye-slim` manifest-list，先复制 read-only mounted artifact 到 `/usr/local/libexec/sakura-ai-updater`，再由 helper 在 controlled TMPDIR 下依次验证 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、UDS `/v1/health`、`backend stop` 与停止后的 `backend is-running` 返回 1；不能复用 build container 作为 runtime evidence。

- [ ] **Step 2: 运行测试确认红灯**

Run:

```bash
uv run --project updater pytest updater/tests/test_build_config.py -q
```

Expected: 构建文件不存在，FAIL。

- [ ] **Step 3: 创建 pinned build dependencies 与 onefile spec**

`requirements-build.txt` 固定：

```text
pyinstaller==6.21.0
pyinstaller-hooks-contrib==2026.6
```

spec 使用现有 `sakura_ai_updater.__main__:main` 入口对应的 wrapper script 或 `updater/src/sakura_ai_updater/__main__.py`，onefile 只产生 `EXE`，不产生 `COLLECT`。使用 `collect_submodules("sakura_ai_updater")` 保证 updater 自身包进入 bundle；依赖 Uvicorn/AnyIO/certifi 由 pinned hooks-contrib 官方 hooks 收集，不添加盲目的逐模块 hidden-import 清单。禁用 UPX。

- [ ] **Step 4: 创建数值式 GLIBC ceiling checker**

- 只解析 final onefile outer ELF/bootloader 的 `readelf --version-info --wide` 输出；忽略 `GLIBC_PRIVATE`；checker 不读取 embedded ELF；
- 用整数 tuple 比较，不做字符串排序；
- 无 outer-ELF GLIBC version needs 或最大值 `> (2, 31)` 均 exit 1；
- 输出检测到的 outer-ELF 最大版本和目标 ceiling；
- build contract 测试必须同时断言 checker 的 outer-ELF-only 文案/实现边界，并要求 fresh runtime smoke contract 存在，避免把静态 ceiling gate 当成运行时兼容性证明。

增加 checker 自身纯函数测试样本：`2.9 < 2.10 < 2.31 < 2.34`，防字符串排序误判。

- [ ] **Step 5: 创建 bullseye build script**

`build.sh` 必须：

1. 检查当前构建容器 Python 是 3.12、`ldd --version` 显示 2.31；否则失败。
2. 安装 `binutils` 与 build requirements。
3. 安装 updater package。
4. 运行 PyInstaller spec。
5. 按 `uname -m` 严格映射 asset 名。
6. 对 final onefile 执行 outer static gate：`file "$output/$asset"`、`python updater/build/check_glibc.py "$output/$asset"`；checker 只检查 final onefile outer ELF/bootloader 的 GLIBC ceiling，不读取 embedded ELF。
7. 执行 build-container smoke，至少运行 `"$output/$asset" --version` 与 `"$output/$asset" backend status --state-dir "$smoke_dir/state" --socket-path "$smoke_dir/updater.sock"`，要求命令成功且 status stdout 是合法 JSON；该 build-container smoke 使用其自身受控 `TMPDIR`，但不替代 fresh runtime 对 controlled TMPDIR 的验证。
8. 只把单个 executable 复制到输出目录并 chmod 0755；build.sh 不调用 docker、不挂载 docker socket。authoritative fresh-runtime smoke 由 Task 2 的 host-side helper 与 Task 3 的 native workflow 在 build container 退出后执行。

`run-fresh-runtime-smoke.sh` 的实现必须是完整、可直接执行的 shell helper；它必须创建 root-compatible `state` 与 controlled onefile extraction `tmp` 目录，均为 `0700`，并在所有 binary CLI 命令之前 `export TMPDIR="$smoke_root/tmp"`：

```bash
#!/usr/bin/env bash
set -euo pipefail

binary=${1:?usage: run-fresh-runtime-smoke.sh /absolute/path/to/binary}
[[ "$binary" = /* && -f "$binary" && ! -L "$binary" && -x "$binary" ]] || {
  printf 'fresh runtime requires an absolute regular executable: %s\n' "$binary" >&2
  exit 2
}

smoke_root=/run/sakura-ai-smoke
state_dir=$smoke_root/state
run_dir=/run/sakura-ai
runtime_tmp=$smoke_root/tmp
socket_path=$run_dir/updater.sock
pid=''
cleanup() {
  if [[ -n "$pid" ]]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -rf -- "$smoke_root"
}
trap cleanup EXIT

apt-get update
apt-get install -y --no-install-recommends curl passwd
install -d -m 0700 "$state_dir" "$runtime_tmp"
export TMPDIR="$runtime_tmp"
mounted_binary=$binary
installed_binary=/usr/local/libexec/sakura-ai-updater
install -d -m 0700 /usr/local/libexec
install -m 0700 "$mounted_binary" "$installed_binary"
common_args=(
  --state-dir "$state_dir"
  --socket-path "$socket_path"
  --binary-path "$installed_binary"
)
"$installed_binary" --version
"$installed_binary" backend install "${common_args[@]}"
"$installed_binary" backend start "${common_args[@]}"
"$installed_binary" backend status "${common_args[@]}"
"$installed_binary" backend is-running "${common_args[@]}"
for attempt in $(seq 1 50); do
  if curl --silent --show-error --fail --unix-socket "$socket_path" http://localhost/v1/health >/dev/null; then
    break
  fi
  sleep 0.1
done
if ! curl --silent --show-error --fail --unix-socket "$socket_path" http://localhost/v1/health >/dev/null; then
  printf 'fresh runtime UDS health did not become ready\n' >&2
  exit 1
fi
"$installed_binary" backend stop "${common_args[@]}"
if "$installed_binary" backend is-running "${common_args[@]}"; then
  printf 'backend remained running after stop\n' >&2
  exit 1
fi
```

- `debian:bullseye-slim` intentionally does not include `curl` or the `groupadd` provider required by `DaemonBackend.install()`; therefore this helper installs `curl passwd` and requires network access to the pinned runtime Debian package repositories for `apt-get update` and `apt-get install`. A failure in either apt command is a fresh-runtime test-harness infrastructure failure, not evidence that the final binary failed its compatibility smoke; the workflow must report the job failed and must not upload the artifact. No apt cache cleanup is required because the runtime is disposable. Numeric `root:9472` socket ownership is compatible with this root smoke even if the group name is absent; state and `/run/sakura-ai-smoke` are root-writable.

该 helper 的 smoke 必须在 clean runtime 内运行；workflow 必须把 final artifact 与 helper 以 read-only mount 注入 runtime，不能把 helper 或 runtime 测试留在 build container 内。

```text
python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db
```

该 build digest 已验证同时包含 `linux/amd64`（manifest `sha256:9323da90ffa74944efca668f09ce7a0f8be95264e43dc87f2807556f1e82edbf`）与 `linux/arm64/v8`（manifest `sha256:359c36d23b4ed4f489b0b9e225758144f6e0d1016fe190217787b862dc8d9af0`），镜像版本为 `3.12.11-slim-bullseye`。实现时 workflow 和本地命令都使用各自的 manifest-list digest；**不得**改回 bookworm 或使用浮动 tag。若 registry 后续不再提供任一 digest，必须先用同一 `docker buildx imagetools inspect` 命令验证替代 digest 的两个目标平台并更新 plan/spec，不能静默退回未 pin tag。

- [ ] **Step 6: 运行静态 contract 测试与 Ruff，并验证两层 smoke contract**

Run:

```bash
uv run --project updater pytest updater/tests/test_build_config.py -q
uv run ruff check updater/build/check_glibc.py updater/tests/test_build_config.py
bash -n updater/build/build.sh updater/build/run-fresh-runtime-smoke.sh
```

Expected: PASS；contract test 明确同时验证 outer-ELF-only GLIBC gate、pinned fresh bullseye runtime digest、read-only artifact copy、helper 的 `state/tmp` 目录与 controlled `TMPDIR`，以及 `--version`、`backend install/start/status/is-running`、UDS health、`backend stop` 和停止后 `is-running` 返回 1。

- [ ] **Step 7: 在 Linux amd64 真实构建并执行 outer gate + fresh runtime smoke 两层验收**

Run（WSL/Linux）：

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD:/workspace" -w /workspace \
  python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db \
  bash updater/build/build.sh /workspace/dist/updater
file dist/updater/sakura-ai-updater-linux-amd64
python updater/build/check_glibc.py dist/updater/sakura-ai-updater-linux-amd64
docker run --rm --platform linux/amd64 \
  -v "$PWD/dist/updater/sakura-ai-updater-linux-amd64:/usr/local/bin/sakura-ai-updater:ro" \
  -v "$PWD/updater/build/run-fresh-runtime-smoke.sh:/usr/local/bin/run-fresh-runtime-smoke.sh:ro" \
  debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792 \
  bash /usr/local/bin/run-fresh-runtime-smoke.sh /usr/local/bin/sakura-ai-updater
```

Expected：第一层输出 final outer ELF，checker 显示 outer-ELF GLIBC 最大值 `<=2.31`；第二层在干净 pinned `debian:bullseye-slim` runtime 中把 read-only mounted artifact 复制为 root-owned `0700` `/usr/local/libexec/sakura-ai-updater`，创建 `state/tmp` 两个 `0700` 目录并导出 `TMPDIR=/run/sakura-ai-smoke/tmp`，随后使用同一 CLI 路径参数成功执行 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、controlled-TMPDIR onefile daemon readiness 与 UDS curl `/v1/health`、`backend stop`，且停止后的 `backend is-running` 返回 1。该 fresh runtime smoke 不是 build-container smoke 的重复，任何一层失败都阻断验收。arm64 使用相同命令在 `ubuntu-24.04-arm` native runner 执行，不使用 QEMU。

- [ ] **Step 8: 更新 `.gitignore` 并暂存 Task 2（不得 commit）**

忽略 PyInstaller `build/`、`dist/` 与 spec 工作缓存，但不忽略受控的 `updater/build/*.spec|*.sh|*.py|*.txt`。确认 fresh-runtime helper 也被纳入受控脚本，不被通配规则忽略。运行：

```bash
git add .gitignore updater/build updater/tests/test_build_config.py
```

Suggested commit message（仅建议）：`build(updater): add bullseye onefile packaging`

---

## Task 3: reusable updater build workflow + 独立 CI quality job

**Files:**
- Create: `.github/workflows/updater-build.yml`
- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_release_workflows.py`

- [ ] **Step 1: 写 workflow contract 的失败测试**

用 `yaml.safe_load` 解析 workflow，兼容 YAML 1.1 把 `on` 解析为 bool 的现有测试风格。断言：

- `updater-build.yml` 的 `build-updater` matrix 每个架构必须在 pinned `python:3.12-slim-bullseye` build container 完成 outer static gate 后，再在同架构、干净且 pinned 的 `debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792` runtime 中执行 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、UDS health、`backend stop` 与停止后 `backend is-running` 返回 1；不得只在 build container 中运行 smoke；
- matrix job 必须只 upload 通过两层 gate 的 Actions artifact，不调用 `gh release`；
- build script/workflow 必须同时包含 build digest 与 fresh runtime digest，并在 workflow matrix 中让 `linux/amd64` 使用 `ubuntu-24.04`、`linux/arm64` 使用 `ubuntu-24.04-arm`；
- matrix job 的 fresh runtime step 必须把 final onefile 与 `run-fresh-runtime-smoke.sh` 以 read-only mounts 放入 `debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792`，并在 build container step 成功退出后执行 helper；helper 必须创建 `state` 与 `tmp` 两个 `0700` 目录、复制 mounted artifact 为 `/usr/local/libexec/sakura-ai-updater`（root-owned `0700`）、导出 `TMPDIR=/run/sakura-ai-smoke/tmp`，再使用统一 `--state-dir`、`--socket-path`、`--binary-path` 运行 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、UDS health、`backend stop` 与停止后的 `backend is-running` 返回 1，以验证 onefile 真实解包及 daemon lifecycle；
- 每个 matrix artifact 只有在 outer static gate 与 fresh runtime lifecycle smoke 都成功后才允许上传；fresh runtime smoke 必须在 controlled `TMPDIR=/run/sakura-ai-smoke/tmp` 下对 root-owned `/usr/local/libexec/sakura-ai-updater` 执行 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、`curl --unix-socket /run/sakura-ai/updater.sock http://localhost/v1/health`、`backend stop`，并断言停止后的 `backend is-running` 返回 1；不能仅依赖 build-container smoke；
- publish job `needs: build`、下载两个已经双门禁通过的 artifacts、恰好一次生成 `SHA256SUMS`、恰好一次 `gh release upload`；
- 上传资产精确为两个 binary + `SHA256SUMS`，无 `update-manifest.json`；
- workflow 不含 `gh release create`/`gh release edit`/`latest`；
- `ci.yml` 有独立 updater job，使用 Python 3.12、`pip install -e './updater[dev]'`、`pytest updater/tests`、`ruff check updater`，并执行 build contract 测试，确保 CI 也验证 outer static gate + fresh runtime smoke contract。

- [ ] **Step 2: 运行 contract 测试确认红灯**

Run:

```bash
pytest tests/test_release_workflows.py -q
```

Expected: updater workflow/CI job 不存在，FAIL。

- [ ] **Step 3: 创建 `updater-build.yml` reusable workflow**

DAG：

```text
workflow_call(version)
  └─ build-updater (matrix native amd64/arm64)
       └─ publish-updater-assets (single writer)
```

`build-updater`：

- checkout caller commit；
- 校验 `backend/__init__.py` 的版本精确等于 input `version`；
- matrix `include` 精确设置 `ubuntu-24.04` + `linux/amd64` 与 `ubuntu-24.04-arm` + `linux/arm64`，不允许 QEMU/cross-PyInstaller；
- native runner 上运行 pinned `python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db` 容器，运行 Task 2 build script 的 outer ELF/bootloader GLIBC gate 与 build-container smoke，输出 final onefile 到 runner workspace；
- build container 成功退出后，在同一 native runner 上启动 pinned `debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792`，将 final binary 和 `run-fresh-runtime-smoke.sh` 分别以 `:ro` mount 注入，执行 helper；helper 必须先执行 `install -m 0700 "$mounted_binary" /usr/local/libexec/sakura-ai-updater`，创建 `/run/sakura-ai-smoke/tmp`（`0700`）并导出 `TMPDIR=/run/sakura-ai-smoke/tmp`，再用同一 `--state-dir`、`--socket-path`、`--binary-path` 依次验证 `--version`、`backend install`、`backend start`、`backend status`、`backend is-running`、UDS health、`backend stop` 与停止后的 `backend is-running` 返回 1，从而覆盖 onefile 解包及 daemon PID/readiness/start/stop；helper 返回非零时 job 失败且不得上传 artifact；
- upload-artifact，artifact name 含 arch 与 `${{ github.run_id }}`，避免 rerun 同名冲突；
- `retention-days: 1`；
- 不授予 contents:write，不碰 Release。

`publish-updater-assets`：

- `needs: build-updater`；`runs-on: ubuntu-24.04`；`permissions: contents: write`；
- download 两个 artifact；
- 精确检查两个 expected filename 都存在、regular、非 symlink、非空，且无其他文件；
- 在一个 job/step 中执行：

```bash
sha256sum \
  sakura-ai-updater-linux-amd64 \
  sakura-ai-updater-linux-arm64 > SHA256SUMS
```

  文件名顺序固定为 amd64、arm64；
- 校验 `SHA256SUMS` 恰好两行；
- 先 `gh release view "v${version}"` 验证 Release 已由 caller 创建，不 create/edit；
- 使用单次命令上传且允许同一 run 重试覆盖：

```bash
gh release upload "v${VERSION}" \
  sakura-ai-updater-linux-amd64 \
  sakura-ai-updater-linux-arm64 \
  SHA256SUMS \
  --clobber
```
- rerun 幂等依赖 caller workflow concurrency + `--clobber`，但不声称 replacement 原子；source assets 已在调用前完成，更新检查 gate 属 Slice 4。

- [ ] **Step 4: 修改 `ci.yml` 增加 updater quality job**

新增独立 job：

```yaml
updater-quality:
  runs-on: ubuntu-latest
  permissions:
    contents: read
  steps:
    - uses: actions/checkout@v7
    - uses: actions/setup-python@v7
      with:
        python-version: '3.12'
        cache: pip
        cache-dependency-path: updater/pyproject.toml
    - run: pip install -e './updater[dev]' ruff
    - run: ruff check updater
    - run: pytest updater/tests -q
    - run: pytest updater/tests/test_build_config.py -q
```

`test_build_config.py` 必须验证 build script 中 pinned build manifest、outer-ELF-only GLIBC checker、build-container smoke 命令；并验证 `run-fresh-runtime-smoke.sh` 与 reusable workflow 中 pinned fresh bullseye runtime manifest、read-only binary/helper mounts、read-only artifact 到 `/usr/local/libexec/sakura-ai-updater` 的 copy、`state/tmp` 目录创建、`export TMPDIR=/run/sakura-ai-smoke/tmp`、统一 `--state-dir`/`--socket-path`/`--binary-path`、`--version`/`backend install/start/status/is-running`/UDS health/`backend stop`/停止后 `is-running=1` smoke contract；真实两架构 fresh runtime 执行由 reusable matrix workflow 完成。

- [ ] **Step 5: 运行 workflow contract 与 updater tests**

Run:

```bash
pytest tests/test_release_workflows.py -q
uv run --project updater pytest updater/tests -q
uv run ruff check updater tests/test_release_workflows.py
```

Expected: PASS；workflow contract 明确覆盖 native 双架构、pinned build/fresh-runtime 镜像、outer static GLIBC gate、fresh runtime controlled TMPDIR、root-owned copied artifact、`--version`/`backend install/start/status/is-running`/UDS health/`backend stop`/停止后 `is-running=1` 两层门禁，以及 single Release owner。

- [ ] **Step 6: 用 actionlint/YAML parser 验证 workflow**

Run（CI/WSL 有 actionlint 时）：

```bash
actionlint .github/workflows/updater-build.yml .github/workflows/ci.yml
```

Expected: 0 errors。若本地未安装 actionlint，`tests/test_release_workflows.py` 必须仍通过，并在最终验证中明确标记 actionlint 由 CI 执行，不能静默声称已跑。

- [ ] **Step 7: 暂存 Task 3（不得 commit）**

```bash
git add .github/workflows/updater-build.yml .github/workflows/ci.yml \
  tests/test_release_workflows.py
```

Suggested commit message（仅建议）：`ci(updater): build native release binaries`

---

## Task 4: 现有 Release 单一 owner 集成 + docs/spec 同步

**Files:**
- Modify: `.github/workflows/release-on-pr-merge.yml:484-770`
- Modify: `tests/test_release_workflows.py`
- Modify: `docs/superpowers/specs/2026-08-07-auto-update-design.md:760-850`
- Modify: `README.md`
- Modify: `README_EN.md`

- [ ] **Step 1: 扩展 Release ownership/DAG 失败测试**

新增断言：

- `release-on-pr-merge.yml` 仍是唯一包含 `gh release create/edit` 的 updater 发布相关 workflow；
- `check_assets` 只可能删除：
  - `Sakura-AI-v${VERSION}.tar.gz`
  - `Sakura-AI-v${VERSION}.zip`
- 不枚举删除 `.assets[].name`；不删除 updater binaries/SHA256SUMS；
- reusable updater caller job `needs: [generate-release, build-and-upload-assets]`；
- caller job `if` 要求两个 dependency success；
- caller 将 `needs.generate-release.outputs.version` 传为 `version`；
- stable image job 原有语义不被 updater caller 破坏；
- concurrency 继续 `cancel-in-progress: false`；
- source package job 仍使用 `--clobber` 上传自己的两个 asset。

- [ ] **Step 2: 运行测试确认红灯**

Run:

```bash
pytest tests/test_release_workflows.py -q
```

Expected: 现有全量 asset 删除和缺少 reusable caller 导致 FAIL。

- [ ] **Step 3: 将 asset cleanup 收窄到 source archives**

在 `check_assets` 中删除“读取 `.assets[].name` 并循环全部删除”的逻辑，只对当前版本两个精确 source asset 调 `gh release delete-asset`，且资产不存在时幂等跳过。不得删除其他名称。

保持 source upload：

```bash
gh release upload "$TAG_NAME" \
  "${ASSET_NAME}.tar.gz" "${ASSET_NAME}.zip" --clobber
```

- [ ] **Step 4: 从现有 Release workflow 调用 reusable updater workflow**

新增 job：

```yaml
  publish-updater-assets:
    name: 构建并上传 Host Updater
    needs:
      - generate-release
      - build-and-upload-assets
    if: >-
      needs.generate-release.result == 'success' &&
      needs.build-and-upload-assets.result == 'success'
    permissions:
      contents: write
    uses: ./.github/workflows/updater-build.yml
    with:
      version: ${{ needs.generate-release.outputs.version }}
    secrets: inherit
```

GitHub reusable workflow caller job 不允许混入 `runs-on`/`steps`；只用合法 caller keys。`generate-release` 继续唯一创建/edit Release；`updater-build.yml` 只验证和上传。

- [ ] **Step 5: 同步 auto-update design spec 的 slice 边界**

修订 §16：

- §16.1 onefile（不是 onedir），说明受控 TMPDIR 与低频 daemon 启动权衡；
- §16.2 `python:3.12-slim-bullseye` native amd64/arm64，outer onefile ELF/bootloader 的 GLIBC ceiling gate 只作为污染检测；checker 不读取 embedded ELF，也不替代 old-glibc build；
- §16.2 增加 authoritative fresh runtime smoke：每个 final onefile 必须在 pinned `debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792` 的同架构干净 runtime 中，以 controlled `TMPDIR=/run/sakura-ai-smoke/tmp`、root-owned `/usr/local/libexec/sakura-ai-updater` 和统一 CLI path 参数通过 `--version`、`backend install/start/status/is-running`、UDS `/v1/health`、`backend stop`、停止后 `is-running=1`；
- §16.3 Slice 3c assets 仅双 binary + SHA256SUMS；最终 P0 assets 的 `update-manifest.json` 由 Slice 4 添加；
- §16.4 首次 acquisition 属 `start.sh`，随后调用 binary `backend install`；Python downloader/self-update 属 P2；
- §16.5 Release 单一 owner + reusable workflow + matrix artifact fan-in + checksum single writer；每个 matrix artifact 只有通过 outer static gate 和 fresh runtime lifecycle smoke 两层门禁后才能发布；
- §16.5 明确同信道 checksum 信任模型与 P2 签名边界；
- 不改变 manifest v1 的最终 schema，只把其产生时间移到 Slice 4，`min_upgrade_from` 不在 3c 硬编码。

- [ ] **Step 6: 更新 README 与 README_EN**

两个 README 同步说明：

- `sudo ./start.sh updater install` 可在无宿主 Python、binary 尚不存在时安装；
- 安装严格绑定 local app version，不取 latest updater；`:latest` 镜像不是 version signal，回退本地 package version；signals 冲突 fail-closed；
- 支持 Linux amd64/arm64，其他架构失败；
- GitHub Release 同源 SHA256 校验、HTTPS-only；
- root-owned `0700` binary/state dir、install lock 与原子替换；
- 安装失败保留旧 binary仅适用于 pre-commit 阶段；download/checksum/chmod/temp fsync/temp safety 任一失败必须可证明旧 final binary byte-for-byte unchanged；post-commit directory fsync 或 final safety confirmation 失败不得使用该表述，必须说明新 inode 可能已安装；
- daemon 正在运行时安装不自动 restart，给出显式 stop/start 命令；
- production 不依赖 Python，`SAKURA_UPDATER_DEV=1` 仍仅供开发。

- [ ] **Step 6: 运行 Task 4 targeted tests**

Run:

```bash
pytest tests/test_release_workflows.py -q
bash tests/test_start_sh_updater.sh
uv run --project updater pytest updater/tests -q
uv run ruff check updater tests/test_release_workflows.py
```

Expected: PASS；workflow contract 同时确认 single Release owner、native matrix、outer static GLIBC gate、fresh bullseye runtime lifecycle smoke（controlled TMPDIR、root-owned copied artifact、`--version`、`backend install/start/status/is-running`、UDS `/v1/health`、`backend stop`、停止后 `is-running=1`）与 Slice 4 manifest exclusion。

- [ ] **Step 7: 暂存 Task 4（不得 commit）**

```bash
git add .github/workflows/release-on-pr-merge.yml \
  tests/test_release_workflows.py \
  docs/superpowers/specs/2026-08-07-auto-update-design.md \
  README.md README_EN.md
```

Suggested commit message（仅建议）：`feat(release): publish trusted updater assets`

---

## Final Validation（四个 Task 完成后）

- [ ] **Step 1: Windows/cross-platform Python tests**

```bash
uv run --project updater pytest updater/tests -q
pytest updater/tests/test_build_config.py tests/test_release_workflows.py tests/test_compose_updater_mount.py -q
```

Expected: all passed；`test_build_config.py` 必须同时通过 outer static GLIBC gate 与 fresh-runtime smoke contract 断言；仅既有 POSIX skip 可接受并需记录。

- [ ] **Step 2: Linux/WSL updater + bash tests**

```bash
uv run --project updater pytest updater/tests -q
bash tests/test_start_sh_updater.sh
bash tests/test_init_deployment_env.sh
bash -n start.sh updater/build/build.sh updater/build/run-fresh-runtime-smoke.sh
```

Expected: all passed，0 unexpected skip/failure；Task 1 失败注入必须区分 pre-commit unchanged 与 post-commit durability/final-safety failure；fresh-runtime helper 的 `backend install/start/status/is-running/stop` 序列及 controlled TMPDIR smoke contract 必须通过。

- [ ] **Step 3: Ruff**

```bash
uv run ruff check updater tests/test_release_workflows.py
```

Expected: All checks passed。

- [ ] **Step 4: Workflow syntax**

```bash
actionlint \
  .github/workflows/ci.yml \
  .github/workflows/updater-build.yml \
  .github/workflows/release-on-pr-merge.yml
```

Expected: 0 errors。

- [ ] **Step 5: Native amd64 build + outer gate + fresh runtime smoke**

```bash
docker run --rm --platform linux/amd64 \
  -v "$PWD:/workspace" -w /workspace \
  python:3.12-slim-bullseye@sha256:411fa4dcfdce7e7a3057c45662beba9dcd4fa36b2e50a2bfcd6c9333e59bf0db \
  bash updater/build/build.sh /workspace/dist/updater
file dist/updater/sakura-ai-updater-linux-amd64
python updater/build/check_glibc.py dist/updater/sakura-ai-updater-linux-amd64
docker run --rm --platform linux/amd64 \
  -v "$PWD/dist/updater/sakura-ai-updater-linux-amd64:/usr/local/bin/sakura-ai-updater:ro" \
  -v "$PWD/updater/build/run-fresh-runtime-smoke.sh:/usr/local/bin/run-fresh-runtime-smoke.sh:ro" \
  debian:bullseye-slim@sha256:f313b4bd62667092a59b3a664d7d3ab8b5e65f41675f48e81455a15dc5abe792 \
  bash /usr/local/bin/run-fresh-runtime-smoke.sh /usr/local/bin/sakura-ai-updater
```

Expected：outer onefile ELF/bootloader gate 显示 GLIBC ceiling `<=2.31`；fresh pinned runtime smoke 创建 `/run/sakura-ai-smoke/state` 与 `/run/sakura-ai-smoke/tmp`（均 `0700`），导出 `TMPDIR=/run/sakura-ai-smoke/tmp`，把 mounted artifact 复制成 root-owned `0700` binary 后，成功执行 `--version`、`backend install/start/status/is-running`，由 `curl --unix-socket /run/sakura-ai/updater.sock http://localhost/v1/health` 返回 HTTP 200，再执行 `backend stop` 并确认 `backend is-running` 返回 1。该 smoke 必须在 clean runtime 中完成，不能把 build container 的运行结果作为 fresh-runtime evidence；arm64 由同一 build script 在 `ubuntu-24.04-arm` native runner 完成，不使用 QEMU。

- [ ] **Step 6: GitHub native matrix dry run / feature branch CI**

在不发布 Release 的 feature branch 上运行 CI，确认 `updater-quality` 通过。`updater-build.yml` 只支持 trusted `workflow_call`，不得从 feature branch 上传正式 Release。合入 main 后正式 run 验收：

```text
amd64 build passed
arm64 native build passed
fresh bullseye runtime smoke passed for both architectures
both outer-ELF GLIBC ceilings <= 2.31
publish job generated exactly one SHA256SUMS
Release contains both binaries + SHA256SUMS + source tar/zip
```

- [ ] **Step 7: Linux fresh-host install E2E**

在临时 Release 或受控正式 Release 上：

```bash
rm -rf .deploy/updater
sudo ./start.sh updater install
sudo stat -c '%F %u %a' .deploy/updater/sakura-ai-updater
sudo ./start.sh updater status
sudo ./start.sh updater start
sudo ./start.sh updater status
```

Expected:

```text
regular file / uid 0 / mode 700
backend install bootstrap succeeds
start/readiness/PID meta remains unchanged
status reports running
```

失败注入：checksum mismatch/404/install lock busy 时，已有 binary hash 不变，temp 清空，daemon 不重启。

- [ ] **Step 8: 安全边界回归**

```bash
pytest tests/test_compose_updater_mount.py -q
```

Expected: 两个 compose 仍 `group_add: 9472`、`/run/sakura-ai` read-only，且无 `/var/run/docker.sock`。

- [ ] **Step 9: 最终工作区审查**

```bash
git status --short
git diff --check
git diff --cached --stat
```

Expected: 仅本 plan 列出的代码/测试/workflow/docs 文件；无 PyInstaller dist/build cache、temp binary、checksum 或用户未授权文件；无 whitespace errors。执行者停在 staged state，**不得 commit/push/开 PR**。

## 六项阻断修订映射

| 审查阻断 | 落地位置 |
|---|---|
| Python downloader bootstrap 循环 | Task 1：首次 acquisition 完全归 `start.sh`；成功后才调用 binary `backend install` |
| bookworm + ceiling 不是兼容策略 | Task 2：`python:3.12-slim-bullseye` 真 old-glibc 构建；ceiling 只做污染 gate |
| onedir 与单文件安装契约冲突 | Task 2：PyInstaller onefile；Task 1：受控 TMPDIR |
| symlink hardening 首次 exec 后才发生 | Task 1：shell exec 前安全 gate + Python defense-in-depth |
| Release 双 owner | Task 3/4：reusable workflow 只 build/upload，现有 workflow 唯一 create/edit owner |
| 3c 无 authoritative `min_upgrade_from` | Scope + Task 4：`update-manifest.json` 和 policy 整体延至 Slice 4 |

## Plan Verdict

**APPROVED FOR IMPLEMENTATION**

六项结构性阻断均已落实为明确文件、测试、实现顺序与验收门禁；无需第三轮完整架构研究。执行必须按 Task 1 → 2 → 3 → 4 线性进行，每个 Task TDD、targeted validation、Ruff/CI validation、`git add`，但不得自主 commit。
