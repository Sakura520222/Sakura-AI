"""Bootstrap development mode tests."""

from backend.core.bootstrap import (
    clear_bootstrap_cache,
    get_connection_config_path,
    read_connection_config,
    write_connection_config,
)


def test_connection_config_path_can_be_overridden(monkeypatch, tmp_path):
    dev_config = tmp_path / "connection.dev.json"
    monkeypatch.setenv("SAKURA_CONNECTION_CONFIG_PATH", str(dev_config))
    clear_bootstrap_cache()

    assert get_connection_config_path() == dev_config

    write_connection_config(
        "mysql+aiomysql://user:pass@localhost:3306/sakura_dev",
        setup_completed=True,
    )

    assert dev_config.exists()
    assert read_connection_config()["setup_completed"] is True
