"""Reconcile React copy with both existing YAML catalogs and compile its subset."""

import argparse
import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# WebUI 2.0 messages (managed by scripts/sync_webui_translations.py)\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--migrate-templates", action="store_true")
    options = parser.parse_args()
    if options.migrate_templates and not options.write:
        parser.error("--migrate-templates requires --write")
    command = ["node", str(ROOT / "web/scripts/extract-messages.mjs")]
    if options.migrate_templates:
        command.append("--migrate-templates")
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8")
    messages = json.loads(result.stdout)
    catalogs = {}
    version_messages = {}
    pending = []
    for language, source in (("zh-CN", "zh"), ("en", "en")):
        path = ROOT / "backend/webui/translations" / f"{language}.yaml"
        current = path.read_text(encoding="utf-8")
        parsed = yaml.safe_load(current)
        existing = parsed.get("webui_v2", {})
        version_messages[language] = {key: value for key, value in parsed.get("version_manager", {}).items() if isinstance(value, str)}
        if existing and MARKER not in current:
            raise ValueError("The existing WebUI namespace must be migrated explicitly")
        catalog = {item["key"]: existing.get(item["key"], item[source]) for item in messages}
        if not all(isinstance(value, str) for value in catalog.values()):
            raise ValueError(f"{language} WebUI translations must be text")
        body = current.partition(MARKER)[0].rstrip() + "\n\n"
        expected = body + MARKER + yaml.safe_dump({"webui_v2": catalog}, allow_unicode=True, sort_keys=False, width=1000)
        pending.append((path, current, expected))
        catalogs[language] = catalog
    compiled = {
        item["sourceKey"]: [catalogs["zh-CN"][item["key"]], catalogs["en"][item["key"]]]
        for item in messages
    }
    path = ROOT / "web/src/app/messages.json"
    expected = json.dumps(compiled, ensure_ascii=False, indent=2) + "\n"
    pending.append((path, path.read_text(encoding="utf-8") if path.exists() else "", expected))
    path = ROOT / "web/src/features/version/messages.json"
    expected = json.dumps(version_messages, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    pending.append((path, path.read_text(encoding="utf-8") if path.exists() else "", expected))
    changed = [item for item in pending if item[1] != item[2]]
    if options.write:
        for path, _, content in changed:
            path.write_text(content, encoding="utf-8", newline="\n")
        print(f"Synchronized {len(messages)} bilingual WebUI messages; changed {len(changed)} files.")
    elif changed:
        raise SystemExit("WebUI translations are out of date. Run uv run python -m scripts.sync_webui_translations --write")
    else:
        print(f"Verified {len(messages)} bilingual WebUI messages.")


if __name__ == "__main__":
    main()
