#!/usr/bin/env bash
set -euo pipefail

binary=${1:?usage: run-fresh-runtime-smoke.sh /absolute/path/to/binary}
[[ "$binary" = /* && -f "$binary" && ! -L "$binary" && -x "$binary" ]] || {
    printf 'fresh runtime requires an absolute regular executable: %s\n' "$binary" >&2
    exit 2
}

smoke_root=/run/sakura-ai-smoke
state_dir=$smoke_root/state
runtime_tmp=$smoke_root/tmp
socket_path=/run/sakura-ai/updater.sock
mounted_binary=$binary
installed_binary=/usr/local/libexec/sakura-ai-updater
common_args=()

cleanup() {
    set +e
    if [[ -x "$installed_binary" && -d "$state_dir" ]]; then
        "$installed_binary" backend stop "${common_args[@]}" >/dev/null 2>&1 || true
    fi
    rm -f -- "$socket_path"
    rmdir /run/sakura-ai 2>/dev/null || true
    rm -rf -- "$smoke_root"
}
trap cleanup EXIT

# bullseye-slim is intentionally minimal. Package failures are harness
# infrastructure failures, not binary compatibility evidence.
if ! apt-get update; then
    printf 'fresh runtime infrastructure failure: apt-get update failed\n' >&2
    exit 1
fi
if ! apt-get install -y --no-install-recommends curl passwd; then
    printf 'fresh runtime infrastructure failure: apt-get install curl passwd failed\n' >&2
    exit 1
fi

install -d -m 0700 "$state_dir" "$runtime_tmp"
export TMPDIR="$runtime_tmp"
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
"$installed_binary" backend status "${common_args[@]}" >/dev/null
"$installed_binary" backend is-running "${common_args[@]}"

ready=0
for _attempt in $(seq 1 50); do
    if curl --unix-socket "$socket_path" http://localhost/v1/health --silent --show-error --fail >/dev/null; then
        ready=1
        break
    fi
    sleep 0.1
done
if [[ "$ready" != "1" ]]; then
    printf 'fresh runtime UDS health did not become ready\n' >&2
    exit 1
fi
if ! curl --unix-socket "$socket_path" http://localhost/v1/health --silent --show-error --fail >/dev/null; then
    printf 'fresh runtime UDS health probe failed\n' >&2
    exit 1
fi

"$installed_binary" backend stop "${common_args[@]}"
if "$installed_binary" backend is-running "${common_args[@]}"; then
    printf 'backend remained running after stop\n' >&2
    exit 1
else
    stopped_rc=$?
    if [[ "$stopped_rc" -ne 1 ]]; then
        printf 'backend is-running after stop returned %s, expected 1\n' "$stopped_rc" >&2
        exit 1
    fi
fi
