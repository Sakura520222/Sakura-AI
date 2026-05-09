"""Redis compatibility checks."""

from backend.core import redis as redis_module


def test_warn_if_getdel_unsupported_logs_once(monkeypatch):
    warnings: list[str] = []

    class FakeLogger:
        def warning(self, message, *args):
            warnings.append(message.format(*args))

        def debug(self, message, *args):
            pass

    monkeypatch.setattr(redis_module, "logger", FakeLogger())
    monkeypatch.setattr(redis_module, "_getdel_version_warning_logged", False)

    redis_module._warn_if_getdel_unsupported("6.0.16")
    redis_module._warn_if_getdel_unsupported("6.0.16")

    assert len(warnings) == 1
    assert "Redis Server 6.2+" in warnings[0]


def test_warn_if_getdel_unsupported_accepts_supported_versions(monkeypatch):
    warnings: list[str] = []

    class FakeLogger:
        def warning(self, message, *args):
            warnings.append(message.format(*args))

        def debug(self, message, *args):
            pass

    monkeypatch.setattr(redis_module, "logger", FakeLogger())
    monkeypatch.setattr(redis_module, "_getdel_version_warning_logged", False)

    redis_module._warn_if_getdel_unsupported("6.2.0")
    redis_module._warn_if_getdel_unsupported("7.2.4")

    assert warnings == []
