"""Production Compose database credential contract."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
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
    assert web["env_file"] == [
        "${SAKURA_COMPOSE_SERVICE_ENV_FILE:-../.deploy/deployment.env}"
    ]
    text = COMPOSE.read_text(encoding="utf-8")
    assert "SAKURA_DB_PASSWORD must be set" in text
    assert "MYSQL_PASSWORD: sakura-ai" not in text


def test_first_deploy_compose_uses_pending_service_env_file(tmp_path: Path):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose is unavailable")

    compose_dir = tmp_path / "docker"
    deploy_dir = tmp_path / ".deploy"
    compose_dir.mkdir()
    deploy_dir.mkdir()
    compose_file = compose_dir / "docker-compose.prod.yml"
    shutil.copy2(COMPOSE, compose_file)
    pending = deploy_dir / ".deployment.env.pending-test"
    pending.write_text(
        "\n".join(
            (
                "SAKURA_DEPLOY_MODE=image",
                "COMPOSE_PROJECT_NAME=sakura-ai",
                f"SAKURA_DB_PASSWORD={'0' * 64}",
                "SAKURA_AI_IMAGE=ghcr.io/sakura520222/sakura-ai:v3.2.0",
                f"SAKURA_SANDBOX_WORKSPACE_ROOT={tmp_path / 'workplace'}",
                "",
            )
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "SAKURA_COMPOSE_SERVICE_ENV_FILE": str(pending),
    }
    result = subprocess.run(
        [
            docker,
            "compose",
            "--env-file",
            str(pending),
            "--project-name",
            "sakura-ai",
            "-f",
            str(compose_file),
            "config",
            "--format",
            "json",
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert not (deploy_dir / "deployment.env").exists()
    assert result.returncode == 0, result.stderr
