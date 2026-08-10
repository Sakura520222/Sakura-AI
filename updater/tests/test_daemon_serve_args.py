"""Daemon child argv compatibility and explicit host path tests."""

from sakura_ai_updater.backends.daemon import DaemonBackend


def test_serve_args_append_compose_and_deployment_paths(tmp_path):
    backend = DaemonBackend(
        state_dir=str(tmp_path / "state"),
        socket_path=str(tmp_path / "updater.sock"),
        compose_file="/etc/sakura/docker-compose.prod.yml",
        deployment_env="/etc/sakura/.deploy/deployment.env",
    )
    args = backend._serve_args()
    assert args[:2] == ["--serve", "--socket-path"]
    assert args[-4:] == [
        "--compose-file",
        "/etc/sakura/docker-compose.prod.yml",
        "--deployment-env",
        "/etc/sakura/.deploy/deployment.env",
    ]
