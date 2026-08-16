#!/usr/bin/env bash
set -euo pipefail

# /app/config is a persistent volume because Setup writes connection.json and
# legacy deployments keep strategies.yaml/labels.yaml there.  Strategy/label
# configuration now lives in the app_config table; the merge script keeps its
# machinery but manages no files today (a future packaged YAML can opt in),
# so a leftover yaml in the volume is left untouched.
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
