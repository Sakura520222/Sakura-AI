from pathlib import Path


def test_updater_lifecycle_forwards_host_paths_without_update_commands():
    script = Path("start.sh").read_text(encoding="utf-8")
    assert '--compose-file "$COMPOSE_FILE"' in script
    assert '--deployment-env "$UPDATER_DEPLOYMENT_ENV_FILE"' in script
    # Lifecycle wiring only; orchestration must remain in the host updater.
    assert "start.sh update apply" not in script
    assert "start.sh update check" not in script
    assert 'UPDATER_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in script
    assert "updater_socket_health_payload" in script
    assert "refusing duplicate start" in script
