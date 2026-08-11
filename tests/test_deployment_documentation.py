"""Deployment documentation contracts for the Host Updater bootstrap path."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_recommended_readme_deployment_bootstraps_host_updater() -> None:
    readme_zh = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")

    for text in (readme_zh, readme_en):
        assert "docker/docker-compose.prod.yml" in text
        assert "raw.githubusercontent.com/Sakura520222/Sakura-AI/main/start.sh" in text
        assert "sudo ./start.sh --prod" in text


def test_deployment_guide_documents_manual_update_and_existing_install_upgrade() -> None:
    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "它不会无人值守安装更新" in guide
    assert "现有 Curl + Compose 部署启用 WebUI 更新" in guide
    assert "sudo ./start.sh updater install" in guide
    assert "sudo ./start.sh updater start" in guide
    assert "Host Updater 当前仅支持 Linux `amd64`/`arm64` 宿主机" in guide
