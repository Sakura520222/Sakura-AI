"""Python 镜像更新策略契约 / Python image update policy contracts."""

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[1]


def test_dependabot_ignores_python_315_without_blocking_314_updates() -> None:
    config = yaml.safe_load(
        (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    )
    docker = next(
        update
        for update in config["updates"]
        if update["package-ecosystem"] == "docker" and update["directory"] == "/docker"
    )
    python_ignores = [
        rule for rule in docker["ignore"] if rule["dependency-name"] == "python"
    ]
    assert python_ignores == [{"dependency-name": "python", "versions": [">=3.15"]}]


@pytest.mark.parametrize(
    "filename", ["Dockerfile", "Dockerfile.agent-sandbox", "Dockerfile.sandboxd"]
)
def test_docker_python_images_stay_on_314(filename: str) -> None:
    source = (ROOT / "docker" / filename).read_text(encoding="utf-8")
    versions = re.findall(r"^FROM python:(\d+\.\d+)(?=[-.@\s])", source, re.MULTILINE)
    assert versions
    assert set(versions) == {"3.14"}
