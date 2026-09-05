"""Redis compatibility checks."""

from types import SimpleNamespace

import pytest
from redis.exceptions import ResponseError

from backend.core import redis as redis_module
from backend.services import telegram_binding_service


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
    assert "Lua GET+DEL" in warnings[0]


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


@pytest.mark.asyncio
async def test_atomic_getdel_uses_native_command_on_supported_server():
    class FakeRedis:
        def __init__(self):
            self.commands = []

        async def execute_command(self, *args):
            self.commands.append(args)
            return "payload"

        async def eval(self, *_args):
            raise AssertionError("native GETDEL should not use Lua fallback")

    client = FakeRedis()

    assert await redis_module.atomic_getdel(client, "key") == "payload"
    assert client.commands == [("GETDEL", "key")]


@pytest.mark.asyncio
async def test_atomic_getdel_uses_atomic_lua_for_unknown_getdel():
    class FakeRedis:
        def __init__(self):
            self.commands = []
            self.eval_calls = []

        async def execute_command(self, *args):
            self.commands.append(args)
            raise ResponseError("ERR unknown command 'GETDEL', with args beginning with:")

        async def eval(self, *args):
            self.eval_calls.append(args)
            return "payload"

    client = FakeRedis()

    assert await redis_module.atomic_getdel(client, "key") == "payload"
    assert client.commands == [("GETDEL", "key")]
    assert len(client.eval_calls) == 1
    assert client.eval_calls[0][1:] == (1, "key")
    assert "GET" in client.eval_calls[0][0]
    assert "DEL" in client.eval_calls[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ResponseError("NOPERM this user has no permissions to run the 'GETDEL' command"),
        ResponseError("ERR unknown command 'EVAL', with args beginning with:"),
        ResponseError("ERR wrong number of arguments for 'GETDEL' command"),
    ],
)
async def test_atomic_getdel_does_not_use_lua_for_non_unknown_getdel_errors(error):
    class FakeRedis:
        def __init__(self):
            self.eval_calls = 0

        async def execute_command(self, *_args):
            raise error

        async def eval(self, *_args):
            self.eval_calls += 1
            return "must not run"

    client = FakeRedis()

    with pytest.raises(ResponseError, match="GETDEL|EVAL|NOPERM"):
        await redis_module.atomic_getdel(client, "key")
    assert client.eval_calls == 0


@pytest.mark.asyncio
async def test_binding_token_redis_lua_fallback_consumes_once(monkeypatch):
    calls = []
    values = {}

    class FakeRedis:
        async def setex(self, key, _ttl, value):
            values[key] = value
            calls.append(("setex", key))

        async def execute_command(self, *args):
            calls.append(("command", args))
            raise ResponseError("ERR unknown command 'GETDEL'")

        async def eval(self, *args):
            calls.append(("eval", args))
            return values.pop(args[-1], None)

    client = FakeRedis()
    async def fake_get_redis():
        return client

    monkeypatch.setattr(
        telegram_binding_service,
        "get_settings",
        lambda: SimpleNamespace(
            telegram_bind_token_expire_seconds=300,
            telegram_bot_username="sakura_test_bot",
        ),
    )
    monkeypatch.setattr(telegram_binding_service, "get_async_redis", fake_get_redis)
    monkeypatch.setattr(
        telegram_binding_service,
        "_token_digest",
        lambda _token: "digest",
    )
    telegram_binding_service._binding_fallback.clear()

    binding = await telegram_binding_service.create_telegram_binding_token(42)

    assert await telegram_binding_service.consume_telegram_binding_token(binding.token) == 42
    assert await telegram_binding_service.consume_telegram_binding_token(binding.token) is None
    assert [kind for kind, _args in calls] == ["setex", "command", "eval", "command", "eval"]


@pytest.mark.asyncio
async def test_binding_token_invalid_payload_is_consumed_once(monkeypatch):
    token = "tg_bind_" + "B" * 32
    values = ["not-json", None]

    class FakeRedis:
        async def execute_command(self, *_args):
            raise ResponseError("ERR unknown command 'GETDEL'")

        async def eval(self, *_args):
            return values.pop(0)

    async def fake_get_redis():
        return FakeRedis()

    monkeypatch.setattr(telegram_binding_service, "get_async_redis", fake_get_redis)
    monkeypatch.setattr(
        telegram_binding_service,
        "_token_digest",
        lambda _token: "digest-invalid",
    )
    telegram_binding_service._binding_fallback.clear()

    assert await telegram_binding_service.consume_telegram_binding_token(token) is None
    assert await telegram_binding_service.consume_telegram_binding_token(token) is None
