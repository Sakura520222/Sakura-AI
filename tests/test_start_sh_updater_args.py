from pathlib import Path


def test_updater_lifecycle_forwards_host_paths_without_update_commands():
    script = Path("start.sh").read_text(encoding="utf-8")
    assert '--compose-file "$COMPOSE_FILE"' in script
    assert '--deployment-env "$UPDATER_DEPLOYMENT_ENV_FILE"' in script
    # Lifecycle wiring only; orchestration must remain in the host updater.
    assert "start.sh update apply" not in script
    assert "start.sh update check" not in script
    assert (
        'UPDATER_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" && pwd)"'
        in script
    )
    assert (
        'UPDATER_SOURCE_COMPOSE_FILE="$UPDATER_PROJECT_ROOT/docker/docker-compose.yml"'
        in script
    )
    assert (
        'UPDATER_PROD_COMPOSE_FILE="$UPDATER_PROJECT_ROOT/docker/docker-compose.prod.yml"'
        in script
    )
    assert "$UPDATER_PROJECT_ROOT/$DEPLOYMENT_ENV_FILE" in script
    assert "updater_socket_listener_responds" in script
    assert "refusing duplicate start" in script
