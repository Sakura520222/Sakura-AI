#!/usr/bin/env bash
set -euo pipefail

# /app/config is a persistent volume because Setup writes connection.json and
# super-admin WebUI routes edit strategies.yaml/labels.yaml.  The image keeps
# immutable defaults outside that volume; merge only adds new default keys and
# never overwrites an existing user value.
config_dir="${SAKURA_CONFIG_DIR:-/app/config}"
defaults_dir="${SAKURA_DEFAULT_CONFIG_DIR:-/app/config-defaults}"
merge_script="${SAKURA_CONFIG_MERGE_SCRIPT:-/app/scripts/merge_packaged_config.py}"
python_bin="${SAKURA_CONFIG_PYTHON:-python}"

mkdir -p "$config_dir"
"$python_bin" "$merge_script" --config-dir "$config_dir" --defaults-dir "$defaults_dir"

if (( $# == 0 )); then
    echo "Sakura AI entrypoint requires an application command" >&2
    exit 64
fi
exec "$@"
