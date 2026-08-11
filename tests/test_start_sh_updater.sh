#!/usr/bin/env bash
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export _START_SH_SOURCED=1
source "$SCRIPT_DIR/start.sh"
set +e

pass=0; fail=0
report() { if [ "$1" -eq 0 ]; then echo "[OK] $2"; pass=$((pass+1)); else echo "[FAIL] $2"; fail=$((fail+1)); fi; }

# 创建临时 UPDATER_STATE_DIR 隔离
TMPDIR=$(mktemp -d)
export UPDATER_STATE_DIR="$TMPDIR/updater"
export UPDATER_BINARY="$TMPDIR/updater/sakura-ai-updater"
mkdir -p "$UPDATER_STATE_DIR"

# Some Git Bash images omit util-linux flock; this test-only shim uses Perl's
# real advisory flock(2) so the contention case remains a cross-process lock test.
if ! command -v flock >/dev/null 2>&1; then
    TEST_FLOCK_BIN="$TMPDIR/test-bin"
    mkdir -p "$TEST_FLOCK_BIN"
    cat > "$TEST_FLOCK_BIN/flock" <<'EOF'
#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(LOCK_EX LOCK_NB);
my ($option, $fd) = @ARGV;
exit 2 unless defined $option && $option eq '-n' && defined $fd && $fd =~ /^\d+$/;
open(my $handle, ">&=$fd") or exit 1;
exit(flock($handle, LOCK_EX | LOCK_NB) ? 0 : 1);
EOF
    chmod +x "$TEST_FLOCK_BIN/flock"
    export PATH="$TEST_FLOCK_BIN:$PATH"
fi

# fake binary 和 fake python 写 argv 到日志
FAKE_LOG="$TMPDIR/fake_calls.log"

# Git Bash 不提供可控的 root-owned metadata；通过 helper 注入 root/mode。
FAKE_UID=0
FAKE_BINARY_OWNER=0
FAKE_BINARY_MODE=700
FAKE_STATE_OWNER=0
FAKE_STATE_MODE=700
updater_current_uid() { printf '%s\n' "$FAKE_UID"; }
updater_binary_owner_uid() {
    printf '%s\n' "$FAKE_BINARY_OWNER"
}
updater_binary_mode() {
    printf '%s\n' "$FAKE_BINARY_MODE"
}
updater_directory_owner_uid() { printf '%s\n' "$FAKE_STATE_OWNER"; }
updater_directory_mode() { printf '%s\n' "$FAKE_STATE_MODE"; }
updater_chown() { :; }
updater_binary_is_safe() {
    local binary="$1" owner mode
    [[ -f "$binary" && ! -L "$binary" ]] || return 1
    owner=$(updater_binary_owner_uid "$binary") || return 1
    mode=$(updater_binary_mode "$binary") || return 1
    [[ "$owner" == "0" && "$mode" =~ ^[0-7]+$ ]] || return 1
    (( (8#$mode & 8#022) == 0 && (8#$mode & 8#111) != 0 ))
}
updater_sync_state_dir() { :; }
updater_flock() { return 0; }
updater_sync_temp() { :; }
updater_chmod() { chmod "$@"; }
updater_mv() { mv "$@"; }

# --- resolver 测试 ---

# S1: 可执行 binary 存在 → 直接调用 binary
cat > "$UPDATER_BINARY" <<'EOF'
#!/usr/bin/env bash
echo "BINARY:$@" >> "$FAKE_LOG_FILE"
exit 0
EOF
chmod +x "$UPDATER_BINARY"
export FAKE_LOG_FILE="$FAKE_LOG"
updater_backend status --state-dir "$UPDATER_STATE_DIR" --socket-path test
grep -q "^BINARY:backend status" "$FAKE_LOG" && report 0 "S1: binary 优先" || report 1 "S1"

# S1b: 安全检查拒绝 symlink、非 root owner、group/other write、不可执行路径。
for unsafe_case in symlink non_root group_write other_write non_exec; do
    rm -f "$UPDATER_BINARY"
    case "$unsafe_case" in
        symlink)
            printf '%s\n' 'target' > "$TMPDIR/unsafe-target"
            ln -s "$TMPDIR/unsafe-target" "$UPDATER_BINARY"
            FAKE_BINARY_OWNER=symlink; FAKE_BINARY_MODE=700
            ;;
        non_root)
            printf '%s\n' 'binary' > "$UPDATER_BINARY"
            chmod +x "$UPDATER_BINARY"
            FAKE_BINARY_OWNER=1000; FAKE_BINARY_MODE=700
            ;;
        group_write)
            printf '%s\n' 'binary' > "$UPDATER_BINARY"
            chmod +x "$UPDATER_BINARY"
            FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=720
            ;;
        other_write)
            printf '%s\n' 'binary' > "$UPDATER_BINARY"
            chmod +x "$UPDATER_BINARY"
            FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=702
            ;;
        non_exec)
            printf '%s\n' 'binary' > "$UPDATER_BINARY"
            chmod 644 "$UPDATER_BINARY"
            FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=600
            ;;
    esac
    : > "$FAKE_LOG"
    export SAKURA_UPDATER_DEV=0
    updater_backend status 2>/dev/null
    rc=$?
    [ "$rc" -eq 126 ] && ! grep -q '^BINARY:' "$FAKE_LOG" \
        && report 0 "S1b-$unsafe_case: production 拒绝不安全路径" \
        || report 1 "S1b-$unsafe_case: rc=$rc"
done
rm -f "$UPDATER_BINARY" "$TMPDIR/unsafe-target"
FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700

# S2: binary 不存在 + 无 dev → 返回 127
rm -f "$UPDATER_BINARY"
export SAKURA_UPDATER_DEV=0
: > "$FAKE_LOG"
updater_backend status 2>/dev/null
rc=$?
[ "$rc" -eq 127 ] && report 0 "S2: 无 binary 无 dev → 127" || report 1 "S2: rc=$rc"

# S3: binary 不存在 + SAKURA_UPDATER_DEV=1 → 调 python
export SAKURA_UPDATER_DEV=1
FAKE_PY="$TMPDIR/fake_python"
cat > "$FAKE_PY" <<PYEOF
#!/usr/bin/env bash
echo "PYTHON:\$@" >> "$FAKE_LOG_FILE"
exit 0
PYEOF
chmod +x "$FAKE_PY"
export SAKURA_UPDATER_PYTHON="$FAKE_PY"
: > "$FAKE_LOG"
updater_backend status --state-dir test --socket-path test 2>/dev/null
grep -q "^PYTHON:-m sakura_ai_updater backend status" "$FAKE_LOG" && report 0 "S3: dev → python -m" || report 1 "S3"

# S4: 不可执行 binary 不作为 production executable（回退到 dev）
echo "not executable" > "$UPDATER_BINARY"
chmod 644 "$UPDATER_BINARY"
FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=600
: > "$FAKE_LOG"
updater_backend status --state-dir test --socket-path test 2>/dev/null
grep -q "^PYTHON:" "$FAKE_LOG" && report 0 "S4: 不可执行 binary 回退 dev" || report 1 "S4"

# S4b: dev fallback 也不能执行现存 unsafe binary。
rm -f "$FAKE_LOG"
printf '%s\n' 'unsafe binary' > "$UPDATER_BINARY"
chmod +x "$UPDATER_BINARY"
FAKE_BINARY_OWNER=1000; FAKE_BINARY_MODE=700
export SAKURA_UPDATER_DEV=1
updater_backend status --state-dir test --socket-path test 2>/dev/null
! grep -q '^BINARY:' "$FAKE_LOG" && grep -q '^PYTHON:' "$FAKE_LOG" \
    && report 0 "S4b: dev 对 unsafe binary 仍调用 Python" || report 1 "S4b"
rm -f "$UPDATER_BINARY"
FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700

# S4c: production unsafe existing path 不覆盖。
printf '%s\n' 'old unsafe bytes' > "$UPDATER_BINARY"
old_hash=$(sha256sum "$UPDATER_BINARY" | cut -d' ' -f1)
FAKE_BINARY_OWNER=1000; FAKE_BINARY_MODE=700
export SAKURA_UPDATER_DEV=0
updater_backend status 2>/dev/null
rc=$?
new_hash=$(sha256sum "$UPDATER_BINARY" | cut -d' ' -f1)
[ "$rc" -eq 126 ] && [ "$old_hash" = "$new_hash" ] \
    && report 0 "S4c: unsafe production 路径拒绝且不覆盖" || report 1 "S4c: rc=$rc"
rm -f "$UPDATER_BINARY"
FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700

# S2b: binary 缺失 + 无 dev → 仍返回 127（重复覆盖 acquisition 前 resolver）。
rm -f "$UPDATER_BINARY"
export SAKURA_UPDATER_DEV=0
: > "$FAKE_LOG"
updater_backend status 2>/dev/null
rc=$?
[ "$rc" -eq 127 ] && report 0 "S2b: 缺失 binary → 127" || report 1 "S2b: rc=$rc"

# --- root gate and command-flow tests ---
ROOT_GATE_LOG="$TMPDIR/root-gate.log"
(
    : > "$ROOT_GATE_LOG"
    FAKE_UID=1000
    updater_binary_is_safe() { echo filesystem-check >> "$ROOT_GATE_LOG"; return 1; }
    cmd_updater_install >/dev/null 2>&1
    rc=$?
    [ "$rc" -ne 0 ] && ! grep -q '^filesystem-check$' "$ROOT_GATE_LOG"
)
[ "$?" -eq 0 ] && report 0 "F1: cmd_updater_install root gate precedes path checks" || report 1 "F1"

EXPLICIT_DIR="$TMPDIR/explicit-install"
(
    mkdir -p "$EXPLICIT_DIR"
    export UPDATER_STATE_DIR="$EXPLICIT_DIR" UPDATER_BINARY="$EXPLICIT_DIR/sakura-ai-updater"
    printf '#!/bin/sh\nold\n' > "$UPDATER_BINARY"
    chmod 0700 "$UPDATER_BINARY"
    FAKE_UID=0; FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700
    EXPLICIT_LOG="$EXPLICIT_DIR/calls.log"
    EXPLICIT_OUT="$EXPLICIT_DIR/output.log"
    install_updater_binary() { echo ACQUIRE >> "$EXPLICIT_LOG"; }
    updater_backend() {
        echo "BACKEND:$1" >> "$EXPLICIT_LOG"
        [ "$1" = "is-running" ] && return 0
        return 0
    }
    cmd_updater_install >"$EXPLICIT_OUT" 2>&1
    rc=$?
    acquire_line=$(grep -n '^ACQUIRE$' "$EXPLICIT_LOG" | cut -d: -f1)
    backend_line=$(grep -n '^BACKEND:install$' "$EXPLICIT_LOG" | cut -d: -f1)
    [ "$rc" -eq 0 ] && [ -n "$acquire_line" ] && [ -n "$backend_line" ] \
        && [ "$acquire_line" -lt "$backend_line" ] \
        && grep -q 'restart-required' "$EXPLICIT_OUT" \
        && ! grep -q '^BACKEND:\(start\|stop\)$' "$EXPLICIT_LOG"
)
[ "$?" -eq 0 ] && report 0 "F2: explicit production install reacquires and warns restart" || report 1 "F2"

EXPLICIT_FAIL_DIR="$TMPDIR/explicit-failure"
(
    mkdir -p "$EXPLICIT_FAIL_DIR"
    export UPDATER_STATE_DIR="$EXPLICIT_FAIL_DIR" UPDATER_BINARY="$EXPLICIT_FAIL_DIR/sakura-ai-updater"
    printf '#!/bin/sh\nold-safe\n' > "$UPDATER_BINARY"
    chmod 0700 "$UPDATER_BINARY"
    old_hash=$(sha256sum "$UPDATER_BINARY" | cut -d' ' -f1)
    FAKE_UID=0; FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700
    EXPLICIT_LOG="$EXPLICIT_FAIL_DIR/calls.log"
    install_updater_binary() { return 1; }
    updater_backend() { echo "BACKEND:$1" >> "$EXPLICIT_LOG"; return 0; }
    cmd_updater_install >/dev/null 2>&1
    rc=$?
    new_hash=$(sha256sum "$UPDATER_BINARY" | cut -d' ' -f1)
    [ "$rc" -ne 0 ] && [ "$old_hash" = "$new_hash" ] && ! grep -q '^BACKEND:install$' "$EXPLICIT_LOG"
)
[ "$?" -eq 0 ] && report 0 "F3: explicit acquisition failure preserves old binary" || report 1 "F3"

SAFE_ENSURE_DIR="$TMPDIR/safe-ensure"
(
    mkdir -p "$SAFE_ENSURE_DIR"
    export UPDATER_STATE_DIR="$SAFE_ENSURE_DIR" UPDATER_BINARY="$SAFE_ENSURE_DIR/sakura-ai-updater"
    printf '#!/bin/sh\n' > "$UPDATER_BINARY"
    chmod 0700 "$UPDATER_BINARY"
    FAKE_UID=0; FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700
    SAFE_ENSURE_LOG="$SAFE_ENSURE_DIR/calls.log"
    cmd_updater_install() { echo WRONG_ACQUISITION >> "$SAFE_ENSURE_LOG"; return 1; }
    updater_backend() {
        echo "BACKEND:$1" >> "$SAFE_ENSURE_LOG"
        [ "$1" = "is-running" ] && return 1
        return 0
    }
    ensure_updater_running >/dev/null 2>&1
    rc=$?
    [ "$rc" -eq 0 ] && ! grep -q '^WRONG_ACQUISITION$' "$SAFE_ENSURE_LOG" \
        && grep -q '^BACKEND:install$' "$SAFE_ENSURE_LOG" \
        && grep -q '^BACKEND:start$' "$SAFE_ENSURE_LOG"
)
[ "$?" -eq 0 ] && report 0 "F4: ensure safe binary bootstraps without acquisition" || report 1 "F4"

MISSING_ENSURE_DIR="$TMPDIR/missing-ensure"
(
    mkdir -p "$MISSING_ENSURE_DIR"
    export UPDATER_STATE_DIR="$MISSING_ENSURE_DIR" UPDATER_BINARY="$MISSING_ENSURE_DIR/sakura-ai-updater"
    FAKE_UID=0; FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700
    MISSING_ENSURE_LOG="$MISSING_ENSURE_DIR/calls.log"
    install_updater_binary() {
        echo ACQUIRE >> "$MISSING_ENSURE_LOG"
        printf '#!/bin/sh\n' > "$UPDATER_BINARY"
        chmod 0700 "$UPDATER_BINARY"
    }
    updater_backend() {
        echo "BACKEND:$1" >> "$MISSING_ENSURE_LOG"
        [ "$1" = "is-running" ] && return 1
        return 0
    }
    ensure_updater_running >/dev/null 2>&1
    rc=$?
    acquire_line=$(grep -n '^ACQUIRE$' "$MISSING_ENSURE_LOG" | cut -d: -f1)
    install_line=$(grep -n '^BACKEND:install$' "$MISSING_ENSURE_LOG" | cut -d: -f1)
    start_line=$(grep -n '^BACKEND:start$' "$MISSING_ENSURE_LOG" | cut -d: -f1)
    [ "$rc" -eq 0 ] && [ -n "$acquire_line" ] && [ -n "$install_line" ] && [ -n "$start_line" ] \
        && [ "$acquire_line" -lt "$install_line" ] && [ "$install_line" -lt "$start_line" ]
)
[ "$?" -eq 0 ] && report 0 "F5: ensure missing binary acquires then installs and starts" || report 1 "F5"

BOOTSTRAP_ERROR_OUT="$TMPDIR/bootstrap-error.out"
(
    export UPDATER_STATE_DIR="$TMPDIR/bootstrap-error" UPDATER_BINARY="$TMPDIR/bootstrap-error/binary"
    mkdir -p "$UPDATER_STATE_DIR"
    FAKE_UID=0
    updater_binary_is_safe() { return 1; }
    cmd_updater_install() { fail "underlying acquisition failure" >&2; return 1; }
    ensure_updater_running
) >"$BOOTSTRAP_ERROR_OUT" 2>&1
[ "$?" -ne 0 ] && grep -q 'updater bootstrap failed; see previous error' "$BOOTSTRAP_ERROR_OUT" \
    && report 0 "F6: ensure reports generic bootstrap failure" || report 1 "F6"

TMPDIR_CHECK_DIR="$TMPDIR/runtime-tmp-check"
(
    mkdir -p "$TMPDIR_CHECK_DIR/tmp"
    export UPDATER_STATE_DIR="$TMPDIR_CHECK_DIR" UPDATER_BINARY="$TMPDIR_CHECK_DIR/sakura-ai-updater"
    printf '#!/bin/sh\necho BINARY >> %s\n' "$FAKE_LOG" > "$UPDATER_BINARY"
    chmod 0700 "$UPDATER_BINARY"
    FAKE_UID=0; FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700; FAKE_STATE_OWNER=0; FAKE_STATE_MODE=755
    updater_backend status >/dev/null 2>&1
    rc=$?
    [ "$rc" -ne 0 ] && ! grep -q '^BINARY$' "$FAKE_LOG"
)
[ "$?" -eq 0 ] && report 0 "F7: existing runtime TMPDIR requires exact 0700" || report 1 "F7"

# --- real updater_curl contract tests ---
CURL_TEST_DIR="$TMPDIR/curl-contract"
mkdir -p "$CURL_TEST_DIR"
(
    CURL_STATUS=200
    CURL_ARG_LOG="$CURL_TEST_DIR/args-200.log"
    curl() {
        printf '%s\n' "$@" > "$CURL_ARG_LOG"
        local output="" headers=""
        while [[ "$#" -gt 0 ]]; do
            case "$1" in
                --output) output="$2"; shift 2 ;;
                --dump-header) headers="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        printf 'payload\n' > "$output"
        [[ -z "$headers" ]] || : > "$headers"
        printf '%s' "$CURL_STATUS"
        return 0
    }
    updater_curl 'https://github.com/Sakura520222/Sakura-AI/releases/download/v3.0.0/test' "$CURL_TEST_DIR/body" "$CURL_TEST_DIR/headers" > "$CURL_TEST_DIR/status"
    rc=$?
    [ "$rc" -eq 0 ] && grep -Fxq -- '--fail' "$CURL_ARG_LOG" \
        && grep -Fxq -- '--location' "$CURL_ARG_LOG" \
        && grep -Fxq -- '--proto' "$CURL_ARG_LOG" \
        && grep -Fxq -- '=https' "$CURL_ARG_LOG" \
        && grep -Fxq -- '--proto-redir' "$CURL_ARG_LOG" \
        && grep -Fxq -- '--connect-timeout' "$CURL_ARG_LOG" \
        && grep -Fxq -- '--max-time' "$CURL_ARG_LOG" \
        && grep -Fxq -- '--write-out' "$CURL_ARG_LOG" \
        && grep -Fxq -- '%{http_code}' "$CURL_ARG_LOG"
)
[ "$?" -eq 0 ] && report 0 "C1: updater_curl uses bounded HTTPS contract" || report 1 "C1"
for curl_status in 302 404; do
    (
        CURL_STATUS="$curl_status"
        CURL_ARG_LOG="$CURL_TEST_DIR/args-$curl_status.log"
        curl() {
            printf '%s\n' "$@" > "$CURL_ARG_LOG"
            local output="" headers=""
            while [[ "$#" -gt 0 ]]; do
                case "$1" in
                    --output) output="$2"; shift 2 ;;
                    --dump-header) headers="$2"; shift 2 ;;
                    *) shift ;;
                esac
            done
            printf 'payload\n' > "$output"
            [[ -z "$headers" ]] || : > "$headers"
            printf '%s' "$CURL_STATUS"
            return 0
        }
        updater_curl "https://github.com/Sakura520222/Sakura-AI/releases/download/v3.0.0/test-$curl_status" "$CURL_TEST_DIR/body-$curl_status" "$CURL_TEST_DIR/headers-$curl_status" >/dev/null
        [ "$?" -ne 0 ]
    )
    [ "$?" -eq 0 ] && report 0 "C2-$curl_status: final HTTP status rejected" || report 1 "C2-$curl_status"
done

payload_file="$CURL_TEST_DIR/length-payload"
printf '12345' > "$payload_file"
printf 'HTTP/2 200\r\nContent-Length: 4\r\n\r\n' > "$CURL_TEST_DIR/length-bad.headers"
updater_validate_content_length "$payload_file" "$CURL_TEST_DIR/length-bad.headers" >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "C3: Content-Length mismatch rejected" || report 1 "C3"
: > "$CURL_TEST_DIR/length-missing.headers"
updater_validate_content_length "$payload_file" "$CURL_TEST_DIR/length-missing.headers" >/dev/null 2>&1
[ "$?" -eq 0 ] && report 0 "C4: missing Content-Length allowed" || report 1 "C4"

VERSION_DIR="$TMPDIR/version"
mkdir -p "$VERSION_DIR"
UPDATER_DEPLOYMENT_ENV_FILE="$VERSION_DIR/deployment.env"
UPDATER_BACKEND_VERSION_FILE="$VERSION_DIR/backend_init.py"
export UPDATER_DEPLOYMENT_ENV_FILE UPDATER_BACKEND_VERSION_FILE
cat > "$UPDATER_BACKEND_VERSION_FILE" <<'EOF'
__version__ = "3.0.0"
EOF
updater_uname_s() { printf '%s\n' Linux; }
updater_uname_m() { printf '%s\n' x86_64; }
updater_health_payload() { return 1; }
cat > "$UPDATER_DEPLOYMENT_ENV_FILE" <<EOF
SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0@sha256:$(printf '%064d' 0)
EOF
[ "$(resolve_updater_app_version)" = "3.1.0" ] \
    && report 0 "V1: image mode concrete image is authoritative" || report 1 "V1"
[ "$(resolve_updater_asset)" = "sakura-ai-updater-linux-amd64" ] && report 0 "V2: x86_64 asset mapping" || report 1 "V2"
cat > "$UPDATER_DEPLOYMENT_ENV_FILE" <<'EOF'
SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest
EOF
[ "$(resolve_updater_app_version)" = "3.0.0" ] \
    && report 0 "V3: image latest falls back to package version" || report 1 "V3"
updater_health_payload() { printf '%s\n' '{"status":"healthy","version":"3.2.0"}'; }
[ "$(resolve_updater_app_version)" = "3.2.0" ] \
    && report 0 "V3b: running image version is authoritative for latest" || report 1 "V3b"
updater_health_payload() { printf '%s\n' '{"status":"healthy","version":"latest"}'; }
[ "$(resolve_updater_app_version)" = "3.0.0" ] \
    && report 0 "V3c: malformed health version falls back to package" || report 1 "V3c"
updater_health_payload() { return 1; }
rm -f "$UPDATER_BACKEND_VERSION_FILE"
cat > "$UPDATER_DEPLOYMENT_ENV_FILE" <<'EOF'
SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0
EOF
[ "$(resolve_updater_app_version)" = "3.1.0" ] \
    && report 0 "V4: concrete image does not require package version" || report 1 "V4"
cat > "$UPDATER_BACKEND_VERSION_FILE" <<'EOF'
__version__ = "3.0.0"
EOF
cat > "$UPDATER_DEPLOYMENT_ENV_FILE" <<'EOF'
SAKURA_DEPLOY_MODE=source
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.1.0
EOF
[ "$(resolve_updater_app_version)" = "3.0.0" ] \
    && report 0 "V5: source mode package version is authoritative" || report 1 "V5"
printf 'SAKURA_DEPLOY_MODE=unknown\n' > "$UPDATER_DEPLOYMENT_ENV_FILE"
resolve_updater_app_version >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "V6: unknown deploy mode rejected" || report 1 "V6"
: > "$UPDATER_DEPLOYMENT_ENV_FILE"
resolve_updater_app_version >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "V7: missing deploy mode rejected" || report 1 "V7"
cat > "$UPDATER_DEPLOYMENT_ENV_FILE" <<'EOF'
SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:latest
EOF
cat > "$UPDATER_BACKEND_VERSION_FILE" <<'EOF'
# no concrete version
EOF
resolve_updater_app_version >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "V8: image latest without package version rejected" || report 1 "V8"
updater_uname_s() { printf '%s\n' Linux; }
updater_uname_m() { printf '%s\n' aarch64; }
[ "$(resolve_updater_asset)" = "sakura-ai-updater-linux-arm64" ] && report 0 "V9: aarch64 asset mapping" || report 1 "V9"
updater_uname_m() { printf '%s\n' riscv64; }
resolve_updater_asset >/dev/null 2>&1
[ "$?" -ne 0 ] && report 0 "V10: unsupported architecture rejected" || report 1 "V10"

# Restore test fixture paths for resolver/ensure tests.
unset UPDATER_DEPLOYMENT_ENV_FILE UPDATER_BACKEND_VERSION_FILE

# --- state_dir migration tests ---
# Git Bash cannot provide real root-owned metadata or reliable symlink detection,
# so migration tests inject owner/mode helpers (same pattern as S1b unsafe cases).
# Two variants:
#   M1 expects harden (chmod to 0700 invoked and reported mode becomes 0700)
#   M1-pass expects pass-through (already 0700, no chmod needed, succeeds)
run_state_dir_migration_case() {
    local case_name="$1" existing_mode="$2" expect_chmod="${3:-1}"
    local case_dir="$TMPDIR/state-mig-$case_name"
    (
        export _START_SH_SOURCED=1
        source "$SCRIPT_DIR/start.sh"
        set +e
        local state_dir="$case_dir/state"
        mkdir -p "$state_dir"
        export UPDATER_STATE_DIR="$state_dir"
        local chmod_called=0 hardened_mode="$existing_mode"
        updater_path_is_symlink() { return 1; }
        updater_current_uid() { printf '%s\n' 0; }
        updater_directory_owner_uid() { printf '%s\n' 0; }
        updater_directory_mode() { printf '%s\n' "$hardened_mode"; }
        updater_chmod() {
            if [[ "$1" == 0700 ]]; then
                chmod_called=1
                hardened_mode=700
            fi
            chmod "$@"
        }
        updater_chown() { chown "$@"; }
        updater_sync_state_dir() { :; }
        updater_prepare_state_dir >/dev/null 2>&1
        rc=$?
        if [[ "$expect_chmod" == 1 ]]; then
            [[ "$rc" -eq 0 ]] && [[ "$chmod_called" -eq 1 ]] && [[ "$hardened_mode" == 700 ]]
        else
            [[ "$rc" -eq 0 ]] && [[ "$hardened_mode" == 700 ]]
        fi
    )
    case_rc=$?
    [ "$case_rc" -eq 0 ] \
        && report 0 "M1-$case_name: root-owned $existing_mode handled as 0700" \
        || report 1 "M1-$case_name"
}
run_state_dir_migration_case 0755-to-0700 755 1
run_state_dir_migration_case 0750-to-0700 750 1
run_state_dir_migration_case already-0700 700 0

run_state_dir_unsafe_existing_case() {
    local case_name="$1" existing_mode="$2"
    local case_dir="$TMPDIR/state-unsafe-$case_name"
    (
        export _START_SH_SOURCED=1
        source "$SCRIPT_DIR/start.sh"
        set +e
        local state_dir="$case_dir/state"
        mkdir -p "$state_dir"
        export UPDATER_STATE_DIR="$state_dir"
        local chmod_called=0
        updater_path_is_symlink() { return 1; }
        updater_current_uid() { printf '%s\n' 0; }
        updater_directory_owner_uid() { printf '%s\n' 0; }
        updater_directory_mode() { printf '%s\n' "$existing_mode"; }
        updater_chmod() { chmod_called=1; chmod "$@"; }
        updater_chown() { chown "$@"; }
        updater_sync_state_dir() { :; }
        updater_prepare_state_dir >/dev/null 2>&1
        rc=$?
        [[ "$rc" -ne 0 ]] && [[ "$chmod_called" -eq 0 ]]
    )
    case_rc=$?
    [ "$case_rc" -eq 0 ] \
        && report 0 "M2-$case_name: unsafe group/other writable $existing_mode rejected" \
        || report 1 "M2-$case_name"
}
run_state_dir_unsafe_existing_case 0770 770
run_state_dir_unsafe_existing_case 0775 775
run_state_dir_unsafe_existing_case 0777 777

# non-root owner rejected without chmod attempt
NONROOT_STATE_DIR="$TMPDIR/state-mig-nonroot"
(
    export _START_SH_SOURCED=1
    source "$SCRIPT_DIR/start.sh"
    set +e
    mkdir -p "$NONROOT_STATE_DIR/state"
    export UPDATER_STATE_DIR="$NONROOT_STATE_DIR/state"
    chmod_called=0
    updater_path_is_symlink() { return 1; }
    updater_current_uid() { printf '%s\n' 0; }
    updater_directory_owner_uid() { printf '%s\n' 1000; }
    updater_directory_mode() { printf '%s\n' 755; }
    updater_chmod() { chmod_called=1; chmod "$@"; }
    updater_chown() { chown "$@"; }
    updater_sync_state_dir() { :; }
    updater_prepare_state_dir >/dev/null 2>&1
    rc=$?
    [[ "$rc" -ne 0 ]] && [[ "$chmod_called" -eq 0 ]]
)
[ "$?" -eq 0 ] && report 0 "M3-nonroot: non-root owner rejected without chmod" || report 1 "M3-nonroot"

# symlink state_dir rejected before chmod
SYMLINK_STATE_DIR="$TMPDIR/state-mig-symlink"
(
    export _START_SH_SOURCED=1
    source "$SCRIPT_DIR/start.sh"
    set +e
    mkdir -p "$SYMLINK_STATE_DIR/state"
    export UPDATER_STATE_DIR="$SYMLINK_STATE_DIR/state"
    chmod_called=0
    updater_path_is_symlink() { return 0; }
    updater_current_uid() { printf '%s\n' 0; }
    updater_directory_owner_uid() { printf '%s\n' 0; }
    updater_directory_mode() { printf '%s\n' 700; }
    updater_chmod() { chmod_called=1; chmod "$@"; }
    updater_chown() { chown "$@"; }
    updater_sync_state_dir() { :; }
    updater_prepare_state_dir >/dev/null 2>&1
    rc=$?
    [[ "$rc" -ne 0 ]] && [[ "$chmod_called" -eq 0 ]]
)
[ "$?" -eq 0 ] && report 0 "M4-symlink: symlink state_dir rejected before chmod" || report 1 "M4-symlink"

# --- trusted acquisition tests ---
ACQ_DIR="$TMPDIR/acquisition"
ACQ_STATE="$ACQ_DIR/state"
ACQ_BINARY="$ACQ_STATE/sakura-ai-updater"
mkdir -p "$ACQ_STATE"
export UPDATER_STATE_DIR="$ACQ_STATE" UPDATER_BINARY="$ACQ_BINARY"
export UPDATER_DEPLOYMENT_ENV_FILE="$VERSION_DIR/deployment.env"
export UPDATER_BACKEND_VERSION_FILE="$VERSION_DIR/backend_init.py"
FAKE_STATE_OWNER=0; FAKE_STATE_MODE=700
FAKE_BINARY_OWNER=0; FAKE_BINARY_MODE=700
printf '#!/bin/sh\nold-final-bytes\n' > "$ACQ_BINARY"
chmod 0700 "$ACQ_BINARY"
ACQ_OLD_HASH=$(sha256sum "$ACQ_BINARY" | cut -d' ' -f1)
FAKE_UID=1000
install_updater_binary >/dev/null 2>&1
[ "$?" -ne 0 ] && [ ! -e "$ACQ_STATE/.updater-download" ] && report 0 "A1: root gate before acquisition" || report 1 "A1"
FAKE_UID=0
ACQ_CALLS="$ACQ_DIR/calls.log"
: > "$ACQ_CALLS"
ACQ_DIGEST=$(printf '%s\n' 'new-final-bytes' | sha256sum | cut -d' ' -f1)
updater_curl() {
    echo "curl:$1 -> $2" >> "$ACQ_CALLS"
    case "$1" in
        */SHA256SUMS) printf '%s  sakura-ai-updater-linux-amd64\r\n' "$ACQ_DIGEST" > "$2" ;;
        *) printf '%s\n' 'new-final-bytes' > "$2" ;;
    esac
    [[ -n "${3:-}" ]] && : > "$3"
}
updater_sha256() { sha256sum -- "$1" | awk '{print $1}'; }
updater_uname_s() { printf '%s\n' Linux; }
updater_uname_m() { printf '%s\n' x86_64; }
cat > "$UPDATER_DEPLOYMENT_ENV_FILE" <<'EOF'
SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0
EOF
cat > "$UPDATER_BACKEND_VERSION_FILE" <<'EOF'
__version__ = "3.0.0"
EOF
updater_backend() { echo "BACKEND:$1" >> "$ACQ_CALLS"; return 0; }
install_updater_binary >"$ACQ_DIR/a2.out" 2>&1
acq_rc=$?
ACQ_NEW_HASH=$(sha256sum "$ACQ_BINARY" | cut -d' ' -f1)
[ "$acq_rc" -eq 0 ] && [ "$ACQ_OLD_HASH" != "$ACQ_NEW_HASH" ] \
    && ! find "$ACQ_STATE" -maxdepth 1 -name '.updater-*' | grep -q . \
    && report 0 "A2: checksum verified atomic acquisition" || { cat "$ACQ_DIR/a2.out" >&2; report 1 "A2: rc=$acq_rc"; }

printf '#!/bin/sh\nold-final-bytes-2\n' > "$ACQ_BINARY"
chmod 0700 "$ACQ_BINARY"
ACQ_OLD_HASH=$(sha256sum "$ACQ_BINARY" | cut -d' ' -f1)
updater_curl() { return 1; }
install_updater_binary >/dev/null 2>&1
acq_rc=$?
ACQ_NEW_HASH=$(sha256sum "$ACQ_BINARY" | cut -d' ' -f1)
[ "$acq_rc" -ne 0 ] && [ "$ACQ_OLD_HASH" = "$ACQ_NEW_HASH" ] \
    && ! find "$ACQ_STATE" -maxdepth 1 -name '.updater-*' | grep -q . \
    && report 0 "A3: pre-commit download failure preserves old bytes" || report 1 "A3"

updater_curl() {
    case "$1" in
        */SHA256SUMS) printf '%s  sakura-ai-updater-linux-amd64\n' "$ACQ_DIGEST" > "$2" ;;
        *) printf '%s\n' 'new-final-bytes' > "$2" ;;
    esac
    [[ -n "${3:-}" ]] && : > "$3"
}
SYNC_STATE_CALLS=0
updater_sync_state_dir() {
    SYNC_STATE_CALLS=$((SYNC_STATE_CALLS + 1))
    [[ "$SYNC_STATE_CALLS" -eq 1 ]]
}
: > "$ACQ_CALLS"
install_updater_binary >"$ACQ_DIR/post.out" 2>&1
acq_rc=$?
[ "$acq_rc" -ne 0 ] && grep -q 'new inode may already be installed' "$ACQ_DIR/post.out" \
    && ! grep -q '^BACKEND:' "$ACQ_CALLS" \
    && report 0 "A4: post-commit durability failure is fail-closed" || report 1 "A4"

# --- cmd_updater install acquisition wiring ---
rm -f "$UPDATER_BINARY"
export SAKURA_UPDATER_DEV=0
: > "$ACQ_CALLS"
install_updater_binary() {
    echo ACQUIRE >> "$ACQ_CALLS"
    printf '#!/bin/sh\n' > "$UPDATER_BINARY"
    chmod 0700 "$UPDATER_BINARY"
}
updater_backend() {
    echo "BACKEND:$1" >> "$ACQ_CALLS"
    return 0
}
cmd_updater_install >/dev/null 2>&1
acq_rc=$?
acquire_line=$(grep -n '^ACQUIRE$' "$ACQ_CALLS" | cut -d: -f1)
backend_line=$(grep -n '^BACKEND:install$' "$ACQ_CALLS" | cut -d: -f1)
[ "$acq_rc" -eq 0 ] && [ -n "$acquire_line" ] && [ -n "$backend_line" ] \
    && [ "$acquire_line" -lt "$backend_line" ] \
    && report 0 "A5: missing binary acquires before backend install" || report 1 "A5"

ensure_updater_running 2>/dev/null
! grep -q "CALLED:install" "$FAKE_LOG" && report 0 "S5: 已运行不重 install" || report 1 "S5"

# S6: is-running 失败 → ensure 调 install + start
export SAKURA_UPDATER_DEV=1
updater_backend() {
    [ "$1" = "is-running" ] && return 1
    echo "CALLED:$1" >> "$FAKE_LOG"
    return 0
}
: > "$FAKE_LOG"
ensure_updater_running 2>/dev/null
grep -q "CALLED:install" "$FAKE_LOG" && report 0 "S6: ensure 调 install" || report 1 "S6"
grep -q "CALLED:start" "$FAKE_LOG" && report 0 "S6: ensure 调 start" || report 1 "S6"

# --- cmd_updater 透传 ---
updater_backend() { echo "BACKEND:$@" >> "$FAKE_LOG"; }
: > "$FAKE_LOG"
cmd_updater status --extra-opt 2>/dev/null
grep -q "BACKEND:status" "$FAKE_LOG" && report 0 "S7: cmd_updater 透传 action" || report 1 "S7"

# S8: start 必须复用 ensure 路径，在 /run 被重启清空后重新执行 install bootstrap。
ensure_updater_running() { echo "ENSURE:$*" >> "$FAKE_LOG"; }
: > "$FAKE_LOG"
cmd_updater start --extra-opt 2>/dev/null
grep -q "ENSURE:--extra-opt" "$FAKE_LOG" \
    && report 0 "S8: cmd_updater start 复用 bootstrap ensure" || report 1 "S8"

# --- missing failure matrix: real lock contention ---
run_real_flock_busy_case() {
    local case_dir="$TMPDIR/real-flock-busy"
    (
        export _START_SH_SOURCED=1
        source "$SCRIPT_DIR/start.sh"
        set +e
        local state_dir="$case_dir/state"
        local binary="$state_dir/sakura-ai-updater"
        local child_out="$case_dir/install.out"
        local curl_log="$case_dir/curl.log"
        local child_rc_file="$case_dir/rc"
        mkdir -p "$state_dir"
        printf '#!/bin/sh\nold-lock-bytes\n' > "$binary"
        chmod 0700 "$binary"
        sha256sum "$binary" | cut -d' ' -f1 > "$case_dir/old.hash"
        export UPDATER_STATE_DIR="$state_dir" UPDATER_BINARY="$binary"
        updater_current_uid() { printf '%s\n' 0; }
        updater_binary_owner_uid() { printf '%s\n' 0; }
        updater_binary_mode() { printf '%s\n' 700; }
        updater_directory_owner_uid() { printf '%s\n' 0; }
        updater_directory_mode() { printf '%s\n' 700; }
        updater_sync_state_dir() { :; }
        : > "$curl_log"

        exec {held_fd}>>"$state_dir/install.lock"
        flock -n "$held_fd" || exit 1
        SECONDS=0
        bash -c '
            export _START_SH_SOURCED=1
            source "$1"
            set +e
            export UPDATER_STATE_DIR="$2" UPDATER_BINARY="$3"
            updater_current_uid() { printf "%s\\n" 0; }
            updater_binary_owner_uid() { printf "%s\\n" 0; }
            updater_binary_mode() { printf "%s\\n" 700; }
            updater_directory_owner_uid() { printf "%s\\n" 0; }
            updater_directory_mode() { printf "%s\\n" 700; }
            updater_sync_state_dir() { :; }
            updater_curl() { printf "curl-called\\n" >> "$5"; return 1; }
            install_updater_binary >"$4" 2>&1
            printf "%s\\n" "$?" > "$6"
        ' _ "$SCRIPT_DIR/start.sh" "$state_dir" "$binary" "$child_out" "$curl_log" "$child_rc_file"
        elapsed=$SECONDS
        exec {held_fd}>&-

        old_hash=$(cat "$case_dir/old.hash")
        new_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        child_rc=$(cat "$child_rc_file" 2>/dev/null)
        temp_paths=$(find "$state_dir" -maxdepth 1 -name '.updater-*' ! -name 'install.lock' -print)
        [[ "$child_rc" -ne 0 ]] && [ "$elapsed" -lt 3 ] \
            && [ "$old_hash" = "$new_hash" ] && [ -z "$temp_paths" ] \
            && [ ! -s "$curl_log" ]
    )
    case_rc=$?
    [ "$case_rc" -eq 0 ] \
        && report 0 "A6-real-flock-busy: busy lock fails immediately without curl or mutation" \
        || { cat "$case_dir/install.out" >&2 2>/dev/null || true; report 1 "A6-real-flock-busy"; }
}
run_real_flock_busy_case

# --- missing failure matrix: checksum validation ---
run_checksum_failure_case() {
    local case_name="$1"
    local case_dir="$TMPDIR/checksum-$case_name"
    (
        export _START_SH_SOURCED=1
        source "$SCRIPT_DIR/start.sh"
        set +e
        local state_dir="$case_dir/state"
        local binary="$state_dir/sakura-ai-updater"
        local calls_log="$case_dir/calls.log"
        local out="$case_dir/install.out"
        mkdir -p "$state_dir"
        printf '#!/bin/sh\nold-checksum-bytes\n' > "$binary"
        chmod 0700 "$binary"
        old_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        export UPDATER_STATE_DIR="$state_dir" UPDATER_BINARY="$binary"
        export UPDATER_DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
        export UPDATER_BACKEND_VERSION_FILE="$case_dir/backend_init.py"
        printf 'SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0\n' > "$UPDATER_DEPLOYMENT_ENV_FILE"
        printf '__version__ = "3.0.0"\n' > "$UPDATER_BACKEND_VERSION_FILE"
        updater_current_uid() { printf '%s\n' 0; }
        updater_binary_owner_uid() { printf '%s\n' 0; }
        updater_binary_mode() { printf '%s\n' 700; }
        updater_directory_owner_uid() { printf '%s\n' 0; }
        updater_directory_mode() { printf '%s\n' 700; }
        updater_sync_state_dir() { :; }
        updater_sync_temp() { :; }
        updater_sha256() { sha256sum -- "$1" | awk '{print $1}'; }
        updater_uname_s() { printf '%s\n' Linux; }
        updater_uname_m() { printf '%s\n' x86_64; }
        digest=$(printf '%s\n' new-checksum-bytes | sha256sum | cut -d' ' -f1)
        : > "$calls_log"
        updater_curl() {
            printf 'URL:%s\n' "$1" >> "$calls_log"
            if [[ "$1" == */SHA256SUMS ]]; then
                case "$case_name" in
                    target-entry-missing)
                        printf '%s  other-asset\n' "$digest" > "$2"
                        ;;
                    target-duplicate)
                        printf '%s  sakura-ai-updater-linux-amd64\n' "$digest" > "$2"
                        printf '%s  sakura-ai-updater-linux-amd64\n' "$digest" >> "$2"
                        ;;
                    malformed-non64)
                        printf '%063d  sakura-ai-updater-linux-amd64\n' 0 > "$2"
                        ;;
                    hash-mismatch)
                        printf '%064d  sakura-ai-updater-linux-amd64\n' 0 > "$2"
                        ;;
                esac
                [[ "$case_name" != checksum-download-failure ]]
                return
            fi
            printf '%s\n' new-checksum-bytes > "$2"
            [[ -z "${3:-}" ]] || : > "$3"
            return 0
        }
        updater_backend() { printf 'BACKEND:%s\n' "$1" >> "$calls_log"; return 0; }

        install_updater_binary >"$out" 2>&1
        rc=$?
        new_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        temp_paths=$(find "$state_dir" -maxdepth 1 -name '.updater-*' ! -name 'install.lock' -print)
        [[ "$rc" -ne 0 ]] && [ "$old_hash" = "$new_hash" ] \
            && [ -z "$temp_paths" ] \
            && ! grep -q '^BACKEND:\(install\|start\)$' "$calls_log" \
            && grep -q '/sakura-ai-updater-linux-amd64$' "$calls_log" \
            && grep -q '/SHA256SUMS$' "$calls_log"
    )
    case_rc=$?
    [ "$case_rc" -eq 0 ] \
        && report 0 "A7-$case_name: checksum failure preserves old binary and cleans temps" \
        || { cat "$case_dir/install.out" >&2 2>/dev/null || true; report 1 "A7-$case_name"; }
}
for checksum_case in checksum-download-failure target-entry-missing target-duplicate malformed-non64 hash-mismatch; do
    run_checksum_failure_case "$checksum_case"
done

# --- missing failure matrix: pre-commit guards ---
run_precommit_failure_case() {
    local case_name="$1"
    local case_dir="$TMPDIR/precommit-$case_name"
    (
        export _START_SH_SOURCED=1
        source "$SCRIPT_DIR/start.sh"
        set +e
        local state_dir="$case_dir/state"
        local binary="$state_dir/sakura-ai-updater"
        local calls_log="$case_dir/calls.log"
        local out="$case_dir/install.out"
        mkdir -p "$state_dir"
        printf '#!/bin/sh\nold-precommit-bytes\n' > "$binary"
        chmod 0700 "$binary"
        old_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        export UPDATER_STATE_DIR="$state_dir" UPDATER_BINARY="$binary"
        export UPDATER_DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
        export UPDATER_BACKEND_VERSION_FILE="$case_dir/backend_init.py"
        printf 'SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0\n' > "$UPDATER_DEPLOYMENT_ENV_FILE"
        printf '__version__ = "3.0.0"\n' > "$UPDATER_BACKEND_VERSION_FILE"
        updater_current_uid() { printf '%s\n' 0; }
        updater_binary_owner_uid() { printf '%s\n' 0; }
        updater_binary_mode() { printf '%s\n' 700; }
        updater_directory_owner_uid() { printf '%s\n' 0; }
        updater_directory_mode() { printf '%s\n' 700; }
        updater_sync_state_dir() { :; }
        updater_sha256() { sha256sum -- "$1" | awk '{print $1}'; }
        updater_uname_s() { printf '%s\n' Linux; }
        updater_uname_m() { printf '%s\n' x86_64; }
        digest=$(printf '%s\n' new-precommit-bytes | sha256sum | cut -d' ' -f1)
        : > "$calls_log"
        updater_curl() {
            printf 'URL:%s\n' "$1" >> "$calls_log"
            if [[ "$1" == */SHA256SUMS ]]; then
                printf '%s  sakura-ai-updater-linux-amd64\n' "$digest" > "$2"
            else
                printf '%s\n' new-precommit-bytes > "$2"
            fi
            [[ -z "${3:-}" ]] || : > "$3"
            return 0
        }
        updater_backend() { printf 'BACKEND:%s\n' "$1" >> "$calls_log"; return 0; }
        case "$case_name" in
            chmod-0700)
                updater_chmod() {
                    if [[ "$1" == 0600 ]]; then
                        printf 'initial-0600\n' >> "$calls_log"
                        chmod "$@"
                        return $?
                    fi
                    if [[ "$1" == 0700 && "${2:-}" == */.updater-download.* ]]; then
                        printf 'failed-0700\n' >> "$calls_log"
                        return 1
                    fi
                    chmod "$@"
                }
                ;;
            temp-safety)
                updater_binary_is_safe() {
                    local candidate="$1"
                    [[ -f "$candidate" && ! -L "$candidate" ]] || return 1
                    if [[ "$candidate" == */.updater-download.* ]]; then
                        printf 'temp-safety-check\n' >> "$calls_log"
                        return 1
                    fi
                    return 0
                }
                ;;
            temp-fsync)
                updater_binary_is_safe() {
                    local candidate="$1"
                    [[ -f "$candidate" && ! -L "$candidate" ]] || return 1
                    return 0
                }
                updater_sync_temp() {
                    printf 'temp-fsync\n' >> "$calls_log"
                    return 1
                }
                ;;
        esac

        install_updater_binary >"$out" 2>&1
        rc=$?
        new_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        temp_paths=$(find "$state_dir" -maxdepth 1 -name '.updater-*' ! -name 'install.lock' -print)
        [[ "$rc" -ne 0 ]] && [ "$old_hash" = "$new_hash" ] \
            && [ -z "$temp_paths" ] \
            && ! grep -q '^BACKEND:\(install\|start\)$' "$calls_log" \
            && grep -q '/sakura-ai-updater-linux-amd64$' "$calls_log" \
            && grep -q '/SHA256SUMS$' "$calls_log" \
            && case "$case_name" in
                chmod-0700) grep -q '^initial-0600$' "$calls_log" && grep -q '^failed-0700$' "$calls_log" ;;
                temp-safety) grep -q '^temp-safety-check$' "$calls_log" ;;
                temp-fsync) grep -q '^temp-fsync$' "$calls_log" ;;
            esac
    )
    case_rc=$?
    [ "$case_rc" -eq 0 ] \
        && report 0 "A8-$case_name: pre-commit failure preserves old binary and cleans temps" \
        || { cat "$case_dir/install.out" >&2 2>/dev/null || true; report 1 "A8-$case_name"; }
}
for precommit_case in chmod-0700 temp-safety temp-fsync; do
    run_precommit_failure_case "$precommit_case"
done

# --- missing failure matrix: post-commit final safety ---
run_postcommit_final_safety_failure_case() {
    local case_dir="$TMPDIR/postcommit-final-safety"
    (
        export _START_SH_SOURCED=1
        source "$SCRIPT_DIR/start.sh"
        set +e
        local state_dir="$case_dir/state"
        local binary="$state_dir/sakura-ai-updater"
        local calls_log="$case_dir/calls.log"
        local out="$case_dir/install.out"
        mkdir -p "$state_dir"
        printf '#!/bin/sh\nold-postcommit-bytes\n' > "$binary"
        chmod 0700 "$binary"
        old_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        export UPDATER_STATE_DIR="$state_dir" UPDATER_BINARY="$binary"
        export UPDATER_DEPLOYMENT_ENV_FILE="$case_dir/deployment.env"
        export UPDATER_BACKEND_VERSION_FILE="$case_dir/backend_init.py"
        printf 'SAKURA_DEPLOY_MODE=image
SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.0.0\n' > "$UPDATER_DEPLOYMENT_ENV_FILE"
        printf '__version__ = "3.0.0"\n' > "$UPDATER_BACKEND_VERSION_FILE"
        updater_current_uid() { printf '%s\n' 0; }
        updater_directory_owner_uid() { printf '%s\n' 0; }
        updater_directory_mode() { printf '%s\n' 700; }
        updater_sync_state_dir() { :; }
        updater_sync_temp() { :; }
        updater_sha256() { sha256sum -- "$1" | awk '{print $1}'; }
        updater_uname_s() { printf '%s\n' Linux; }
        updater_uname_m() { printf '%s\n' x86_64; }
        digest=$(printf '%s\n' new-postcommit-bytes | sha256sum | cut -d' ' -f1)
        : > "$calls_log"
        updater_curl() {
            printf 'URL:%s\n' "$1" >> "$calls_log"
            if [[ "$1" == */SHA256SUMS ]]; then
                printf '%s  sakura-ai-updater-linux-amd64\n' "$digest" > "$2"
            else
                printf '%s\n' new-postcommit-bytes > "$2"
            fi
            [[ -z "${3:-}" ]] || : > "$3"
            return 0
        }
        updater_backend() { printf 'BACKEND:%s\n' "$1" >> "$calls_log"; return 0; }
        final_safety_checks=0
        updater_binary_is_safe() {
            local candidate="$1"
            [[ -f "$candidate" && ! -L "$candidate" ]] || return 1
            if [[ "$candidate" == "$UPDATER_BINARY" ]]; then
                final_safety_checks=$((final_safety_checks + 1))
                [[ "$final_safety_checks" -lt 3 ]]
                return
            fi
            return 0
        }

        install_updater_binary >"$out" 2>&1
        rc=$?
        new_hash=$(sha256sum "$binary" | cut -d' ' -f1)
        temp_paths=$(find "$state_dir" -maxdepth 1 -name '.updater-*' ! -name 'install.lock' -print)
        [[ "$rc" -ne 0 ]] && [ "$old_hash" != "$new_hash" ] \
            && grep -q 'new inode may already be installed' "$out" \
            && ! grep -q 'old binary unchanged' "$out" \
            && [ -z "$temp_paths" ] \
            && ! grep -q '^BACKEND:\(install\|start\)$' "$calls_log" \
            && grep -q '/sakura-ai-updater-linux-amd64$' "$calls_log" \
            && grep -q '/SHA256SUMS$' "$calls_log"
    )
    case_rc=$?
    [ "$case_rc" -eq 0 ] \
        && report 0 "A9-postcommit-final-safety: installed inode is reported without backend calls" \
        || { cat "$case_dir/install.out" >&2 2>/dev/null || true; report 1 "A9-postcommit-final-safety"; }
}
run_postcommit_final_safety_failure_case

rm -rf "$TMPDIR"
echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
