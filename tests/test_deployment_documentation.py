"""Deployment documentation contracts for the Host Updater bootstrap path."""

from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_recommended_readme_deployment_bootstraps_host_updater() -> None:
    readme_zh = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")

    for text in (readme_zh, readme_en):
        assert "bash -s -- --prod" in text
        assert "raw.githubusercontent.com/Sakura520222/Sakura-AI/main/start.sh" in text
        assert "Host Updater" in text
        assert "https://sakura520222.github.io/Sakura-AI/DEPLOYMENT.html" in text

    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    assert "/opt/sakura-ai" in guide
    assert "SAKURA_INSTALL_ROOT" in guide
    assert "sudo ./start.sh --prod" in guide


def test_deployment_guide_documents_manual_update_and_trusted_root_path() -> None:
    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert "它不会无人值守安装更新" in guide
    assert "现有 Curl + Compose 部署启用 WebUI 更新" not in guide
    assert "root-owned、group/other 不可写的目录链" in guide
    assert "逐级 `lstat` 并 fail-closed" in guide
    assert "sudo ./start.sh updater start" in guide
    assert "Host Updater 当前仅支持 Linux `amd64`/`arm64` 宿主机" in guide


def test_updater_sync_documents_health_as_a_manual_prerequisite() -> None:
    readme_zh = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    for text in (readme_zh, readme_en):
        assert "https://sakura520222.github.io/Sakura-AI/DEPLOYMENT.html" in text
    assert "它本身**不提供应用健康门禁**" in guide
    assert (
        "直接使用 WebUI 更新时，确认上述响应中的 `version` 等于目标版本后，可手动同步 updater"
        in guide
    )
    assert "通过 `start.sh` 更新则会在应用更新前自动同步宿主 updater" in guide
    assert "无需查询 `/health`" in guide
