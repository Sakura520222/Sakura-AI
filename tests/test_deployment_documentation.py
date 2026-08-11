"""Deployment documentation contracts for the Host Updater bootstrap path."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_recommended_readme_deployment_bootstraps_host_updater() -> None:
    readme_zh = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")

    for text in (readme_zh, readme_en):
        assert "/opt/sakura-ai" in text
        assert "sudo install -d -o root -g root -m 0755" in text
        assert "docker/docker-compose.prod.yml" in text
        assert "raw.githubusercontent.com/Sakura520222/Sakura-AI/main/start.sh" in text
        assert "sudo ./start.sh --prod" in text


def test_deployment_guide_documents_manual_update_and_trusted_root_path() -> None:
    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "它不会无人值守安装更新" in guide
    assert "现有 Curl + Compose 部署启用 WebUI 更新" not in guide
    assert "root-owned、group/other 不可写的目录链" in guide
    assert "逐级 `lstat` 并 fail-closed" in guide
    assert "sudo ./start.sh updater start" in guide
    assert "Host Updater 当前仅支持 Linux `amd64`/`arm64` 宿主机" in guide
