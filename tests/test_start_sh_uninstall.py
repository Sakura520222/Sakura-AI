"""Structural contracts for the start.sh uninstall submenu and main menu order.

Covers the design in
docs/superpowers/specs/2026-08-28-start-sh-uninstall-subcommands-design.md:
menu lifecycle ordering, the uninstall submenu, the unified UNINSTALL
confirmation word, and full-uninstall image removal ordering.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

# Menu order follows the service lifecycle: control, status, build/deploy,
# images, administration.
MAIN_MENU_ORDER = (
    "启动服务 (自动检测构建)",
    "停止服务",
    "查看构建/运行状态",
    "查看服务容器状态",
    "Agent sandboxd 状态",
    "强制重建镜像并启动",
    "生产镜像部署",
    "附加到构建日志",
    "停止正在进行的构建",
    "更新镜像 (当前频道)",
    "切换镜像频道 (正式/开发)",
    "Updater daemon 管理",
    "卸载 Sakura AI",
)


def test_main_menu_order_follows_lifecycle_groups():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    # Menu entries appear in ascending order inside render_main_menu.
    positions = [
        script.index(f'ui_line "  ${{BOLD}}[{number}]${{RESET}} {label}"')
        for number, label in enumerate(MAIN_MENU_ORDER, start=1)
    ]
    assert positions == sorted(positions)
    assert 'ui_line "  ${BOLD}[0]${RESET} 退出"' in script


def test_main_menu_actions_map_to_reordered_entries():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    expected = (
        "1)  menu_run do_start false ;;",
        "2)  menu_run do_down ;;",
        "3)  menu_run cmd_status ;;",
        "4)  menu_run do_ps ;;",
        "5)  menu_run cmd_sandbox status ;;",
        "6)  menu_run do_start true ;;",
        "7)  menu_run do_start false true ;;",
        "8)  menu_run cmd_attach ;;",
        "9)  menu_run cmd_stop ;;",
        "10) menu_run cmd_update_image ;;",
        "11) menu_run cmd_switch_channel ;;",
        "12) updater_menu_loop ;;",
        "13) uninstall_menu_loop ;;",
    )
    for entry in expected:
        assert entry in script


def test_uninstall_submenu_offers_standard_and_full_levels():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "render_uninstall_menu" in script
    assert "uninstall_menu_loop" in script
    assert "标准卸载 (保留数据卷和部署状态，可重新部署)" in script
    assert "完全卸载 (删除数据卷、镜像和部署状态)" in script
    assert "1) menu_run cmd_uninstall ;;" in script
    assert "2) menu_run cmd_uninstall --purge ;;" in script
    assert "0) return 0 ;;" in script


def test_uninstall_confirmation_uses_single_uninstall_word():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    # Both modes share the single confirmation word; the legacy per-mode
    # "PURGE SAKURA-AI" prompt must stay removed.
    assert "输入 'UNINSTALL' 继续: " in script
    assert "PURGE SAKURA-AI" not in script
    assert 'expected="PURGE' not in script
    assert 'expected="UNINSTALL"' not in script


def test_full_uninstall_removes_compose_stack_images():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    # --rmi all only joins the purge branch so a standard uninstall keeps
    # the stack images for a later redeployment.
    assert "compose_cmd+=(--volumes --rmi all)" in script


def test_full_uninstall_purges_images_by_repository_before_state():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    purge_images = script.index("purge_sakura_images() {")
    cmd_uninstall = script.index("cmd_uninstall() {")
    body = script[cmd_uninstall:]
    # Image removal must still happen before the deployment state is purged.
    assert "purge_sakura_images || return $?" in body
    assert body.index("purge_sakura_images || return $?") < body.index(
        "purge_sakura_deployment_state || return $?"
    )
    # The loop enumerates local images by repository prefix: digest pulls
    # leave untagged images only attributable via RepoDigests, and updater
    # releases keep old version tags behind that --rmi all never sees.
    loop_body = script[purge_images: script.index("purge_sakura_deployment_state() {")]
    assert "docker image ls --format '{{.ID}} {{.Repository}} {{.Tag}}'" in loop_body
    assert "ghcr.io/sakura520222/sakura-ai-*) ;;" in loop_body
    assert "{{join .RepoDigests \" \"}}" in loop_body
    assert 'docker rmi -f "$id"' in loop_body


def test_uninstall_help_documents_purge_semantics():
    script = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert "卸载服务；默认保留数据，--purge 完全卸载（含数据卷/镜像/部署状态）" in script
    assert "完全卸载：删除数据卷、镜像和 .deploy 状态" in script


def test_readme_documents_two_uninstall_levels():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    assert "标准卸载：保留数据，可重新部署" in readme
    assert "完全卸载：永久删除数据卷、镜像与部署状态" in readme
    assert "Standard uninstall: preserve data for a later redeployment" in readme_en
    assert "permanently delete volumes, images, and deployment state" in readme_en


def test_start_script_is_bash_valid_when_bash_is_available():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable on this host")
    result = subprocess.run(
        [bash, "-n", "start.sh"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
