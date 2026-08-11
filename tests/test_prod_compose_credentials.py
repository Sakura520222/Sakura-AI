"""Production Compose database credential contract."""

from __future__ import annotations

from pathlib import Path

import yaml

COMPOSE = Path("docker/docker-compose.prod.yml")


def _compose() -> dict:
    with COMPOSE.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_web_and_mysql_use_the_same_required_secret_variable():
    compose = _compose()
    assert "name" not in compose
    services = compose["services"]
    web_environment = services["web"]["environment"]
    mysql_environment = services["mysql"]["environment"]

    required = "${SAKURA_DB_PASSWORD:?SAKURA_DB_PASSWORD must be set}"
    assert required in web_environment["DATABASE_URL"]
    assert mysql_environment["MYSQL_PASSWORD"] == required
    assert "sakura-ai@mysql" not in web_environment["DATABASE_URL"]
    assert mysql_environment["MYSQL_PASSWORD"] != "sakura-ai"


def test_production_compose_keeps_runtime_env_file_contract():
    web = _compose()["services"]["web"]
    assert web["env_file"] == ["../.deploy/deployment.env"]
    text = COMPOSE.read_text(encoding="utf-8")
    assert "SAKURA_DB_PASSWORD must be set" in text
    assert "MYSQL_PASSWORD: sakura-ai" not in text
