"""CLI path forwarding tests for host orchestration injection."""

from sakura_ai_updater import __main__ as main_mod


def test_serve_parser_forwards_explicit_host_paths(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        main_mod, "serve", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    state_dir = str(tmp_path / "state")
    main_mod.main(
        [
            "--serve",
            "--state-dir",
            state_dir,
            "--compose-file",
            "/etc/sakura/docker-compose.prod.yml",
            "--deployment-env",
            "/etc/sakura/.deploy/deployment.env",
            "--health-url",
            "http://127.0.0.1:8000/health",
            "--disk-space-threshold",
            "123",
        ]
    )
    assert calls[0][1]["compose_file"].endswith("docker-compose.prod.yml")
    assert calls[0][1]["deployment_env"].endswith("deployment.env")
    assert calls[0][1]["health_url"].endswith("/health")
    assert calls[0][1]["disk_space_threshold"] == 123


def test_serve_injects_orchestrator_when_host_paths_are_present(monkeypatch, tmp_path):
    """The production path must not silently fall back to status-only IPC."""
    import asyncio
    import sys
    import types

    fake_locks = types.ModuleType("sakura_ai_updater.locks")
    fake_locks.LockBusyError = RuntimeError
    fake_locks.acquire_process_lock = lambda path: object()
    fake_locks.release_process_lock = lambda fd: None
    monkeypatch.setitem(sys.modules, "sakura_ai_updater.locks", fake_locks)
    monkeypatch.setattr(main_mod, "prepare_socket_path", lambda path: None)
    monkeypatch.setattr(main_mod, "cleanup_owned_socket", lambda path: None)
    monkeypatch.setattr(main_mod, "load_state", lambda path: object())
    monkeypatch.setattr(
        main_mod, "reconcile_interrupted_job", lambda store: (store, False)
    )

    class _Listener:
        def close(self):
            pass

    monkeypatch.setattr(main_mod, "bind_socket_listener", lambda *a, **k: _Listener())
    captured = {}

    def _create_app(state_path, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(main_mod, "create_app", _create_app)
    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = lambda app, **kwargs: object()

    class _Server:
        def __init__(self, config):
            pass

        async def serve(self, sockets=None):
            pass

    fake_uvicorn.Server = _Server
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    real_run = asyncio.run
    monkeypatch.setattr(main_mod.asyncio, "run", lambda awaitable: real_run(awaitable))

    main_mod.serve(
        "updater.sock",
        str(tmp_path / "state.json"),
        "updater.lock",
        compose_file="/etc/sakura/docker-compose.prod.yml",
        deployment_env="/etc/sakura/.deploy/deployment.env",
    )
    assert captured["orchestrator"] is not None
