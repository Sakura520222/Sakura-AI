"""Bootstrap development mode tests."""

import json

import pytest

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


def test_connection_config_write_failure_preserves_previous_file(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "connection.json"
    monkeypatch.setenv("SAKURA_CONNECTION_CONFIG_PATH", str(config_path))
    clear_bootstrap_cache()
    config_path.write_text(
        json.dumps(
            {"database_url": "mysql+asyncmy://old/db", "setup_completed": True}
        ),
        encoding="utf-8",
    )

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr("backend.core.bootstrap.os.replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_connection_config(
            "mysql+asyncmy://new/db",
            setup_completed=True,
        )

    assert read_connection_config()["database_url"] == "mysql+asyncmy://old/db"
    assert list(tmp_path.glob(".connection.json.*.tmp")) == []
