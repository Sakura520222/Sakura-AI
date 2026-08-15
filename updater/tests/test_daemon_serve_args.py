"""Daemon child argv compatibility and explicit host path tests."""

from sakura_ai_updater.backends.daemon import DaemonBackend


def test_serve_args_append_compose_and_deployment_paths(tmp_path):
    compose_file = str(tmp_path / "docker-compose.prod.yml")
    deployment_env = str(tmp_path / "deployment.env")
    backend = DaemonBackend(
        state_dir=str(tmp_path / "state"),
        socket_path=str(tmp_path / "updater.sock"),
        compose_file=compose_file,
        deployment_env=deployment_env,
    )
    args = backend._serve_args()
    assert args[:2] == ["--serve", "--socket-path"]
    assert args[-4:] == [
        "--compose-file",
        compose_file,
        "--deployment-env",
        deployment_env,
    ]


def test_serve_args_freeze_relative_paths_before_checkout_replacement(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    backend = DaemonBackend(
        state_dir=".deploy/updater",
        socket_path="run/updater.sock",
        compose_file="docker/docker-compose.prod.yml",
        deployment_env=".deploy/deployment.env",
    )
    args = backend._serve_args()
    assert backend.state_dir == str(tmp_path / ".deploy" / "updater")
    assert backend.socket_path == str(tmp_path / "run" / "updater.sock")
    assert args[args.index("--lock-path") + 1] == str(
        tmp_path / ".deploy" / "updater" / "updater.lock"
    )
    assert args[args.index("--compose-file") + 1] == str(
        tmp_path / "docker" / "docker-compose.prod.yml"
    )
    assert args[args.index("--deployment-env") + 1] == str(
        tmp_path / ".deploy" / "deployment.env"
    )
