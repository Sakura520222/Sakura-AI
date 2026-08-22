#!/usr/bin/env bash
set -euo pipefail

# This script is executed inside the pinned native build image:
# python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52
BUILD_IMAGE='python:3.14-slim-bookworm@sha256:23c59390fc717bf09f9336908199a0ae75d9c4264bf296123f94ad772fea3b52'

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "$script_dir/../.." && pwd)
output_dir=${1:-"$repo_root/dist/updater"}

python_version=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
if [[ "$python_version" != "3.14" ]]; then
    printf 'build requires Python 3.14, found %s\n' "$python_version" >&2
    exit 1
fi

glibc_version=$(ldd --version 2>&1 | sed -n '1p' | grep -oE '[0-9]+\.[0-9]+' | tail -n 1)
if [[ "$glibc_version" != "2.36" ]]; then
    printf 'build requires glibc 2.36, found %s\n' "${glibc_version:-unknown}" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends binutils build-essential file

cd -- "$repo_root"
python -m pip install --no-cache-dir -r ./updater/build/requirements-build.txt
python -m pip install --no-cache-dir -e ./updater

work_dir=$(mktemp -d)
cleanup() {
    rm -rf -- "$work_dir"
}
trap cleanup EXIT
install -d -m 0700 "$work_dir/dist" "$work_dir/work" "$work_dir/smoke/state" "$work_dir/smoke/tmp"
export PYINSTALLER_CONFIG_DIR="$work_dir/.pyinstaller"

python -m PyInstaller \
    --clean \
    --noconfirm \
    --distpath "$work_dir/dist" \
    --workpath "$work_dir/work" \
    ./updater/build/sakura-ai-updater.spec

generated_binary="$work_dir/dist/sakura-ai-updater"
[[ -f "$generated_binary" && ! -L "$generated_binary" && -x "$generated_binary" ]] || {
    printf 'PyInstaller did not produce onefile executable: %s\n' "$generated_binary" >&2
    exit 1
}

case "$(uname -m)" in
    x86_64|amd64)
        asset_name='sakura-ai-updater-linux-amd64'
        ;;
    aarch64|arm64)
        asset_name='sakura-ai-updater-linux-arm64'
        ;;
    *)
        printf 'unsupported build architecture: %s\n' "$(uname -m)" >&2
        exit 1
        ;;
esac

rm -rf -- "$output_dir"
install -d -m 0755 "$output_dir"
final_binary="$output_dir/$asset_name"
install -m 0755 "$generated_binary" "$final_binary"

# Outer onefile ELF/bootloader static compatibility gate. The checker does not
# inspect the embedded CArchive; old-glibc native construction remains primary.
file "$final_binary"
python ./updater/build/check_glibc.py "$final_binary"

# Build-container smoke proves the produced binary can start its CLI and emit
# valid status JSON. The fresh pinned runtime helper is the authoritative
# lifecycle compatibility check and runs outside this build container.
smoke_dir="$work_dir/smoke"
export TMPDIR="$smoke_dir/tmp"
"$final_binary" --version
status_json=$("$final_binary" backend status \
    --state-dir "$smoke_dir/state" \
    --socket-path "$smoke_dir/updater.sock")
printf '%s\n' "$status_json" | python -c 'import json, sys; json.load(sys.stdin)'

if [[ "$(find "$output_dir" -mindepth 1 -maxdepth 1 | wc -l)" != "1" ]]; then
    printf 'build output must contain exactly one binary: %s\n' "$output_dir" >&2
    exit 1
fi
printf 'built %s\n' "$final_binary"
