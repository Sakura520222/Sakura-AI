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

# fake binary 和 fake python 写 argv 到日志
FAKE_LOG="$TMPDIR/fake_calls.log"

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
: > "$FAKE_LOG"
updater_backend status --state-dir test --socket-path test 2>/dev/null
grep -q "^PYTHON:" "$FAKE_LOG" && report 0 "S4: 不可执行 binary 回退 dev" || report 1 "S4"

# --- ensure_updater_running 测试 ---

# S5: is-running 成功 → ensure 不调 install/start
updater_backend() {
    [ "$1" = "is-running" ] && return 0 || { echo "CALLED:$1" >> "$FAKE_LOG"; return 0; }
}
: > "$FAKE_LOG"
ensure_updater_running 2>/dev/null
! grep -q "CALLED:install" "$FAKE_LOG" && report 0 "S5: 已运行不重 install" || report 1 "S5"

# S6: is-running 失败 → ensure 调 install + start
unset -f updater_backend  # 恢复原始函数
# 用 source 中的原函数 + 覆盖
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

rm -rf "$TMPDIR"
echo ""
echo "结果: $pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
