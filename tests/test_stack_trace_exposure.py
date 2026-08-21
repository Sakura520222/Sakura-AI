"""回归测试：异常路径不向外部响应泄漏堆栈/内部信息，但完整记录到日志。

覆盖 CodeQL py/stack-trace-exposure（CWE-209）修复，并守护 loguru 日志契约——
``logger.exception`` / ``logger.opt(exception=True).warning`` 必须在 except 块中保留
完整 traceback；stdlib 风格的 ``exc_info=True`` 在 loguru 中不生效（会丢失堆栈）。
"""

import io
import json
from unittest.mock import AsyncMock, Mock

import pytest
from loguru import logger


class _FakeUserConfigDb:
    """user_config 端点所需的最小 AsyncSession。

    ValueError 在 db.execute 之前抛出（validate 阶段），因此只需 rollback。
    """

    def __init__(self):
        self.rolled_back = False

    async def rollback(self):
        self.rolled_back = True


@pytest.fixture
def loguru_capture():
    """捕获 loguru 日志到 StringIO，供断言 traceback 与消息内容。"""
    buf = io.StringIO()
    handler_id = logger.add(
        buf,
        format="{level}|{message}|{exception}",
        level="DEBUG",
        catch=False,
    )
    yield buf
    logger.remove(handler_id)


@pytest.mark.asyncio
async def test_user_config_invalid_value_sanitizes_response_and_logs_traceback(
    loguru_capture,
):
    """ValueError 路径：响应脱敏并含受控 key 名，日志保留完整 traceback。

    覆盖 backend/api/v1/user_config.py 的 ``logger.opt(exception=True).warning`` 修复——
    若回退为 ``exc_info=True``，loguru 不记录 traceback，本测试会失败。
    """
    from backend.api.v1.schemas import UserConfigUpdateRequest
    from backend.api.v1.user_config import update_user_config

    body = UserConfigUpdateRequest(configs={"output_language": "invalid-value"})
    db = _FakeUserConfigDb()

    response = await update_user_config(
        body,
        user={"user_id": 1, "sub": "user-1"},
        db=db,
    )

    data = json.loads(response.body)
    log_output = loguru_capture.getvalue()

    # 响应脱敏：不含 exception 的精确校验消息，但含受控 key 名引导用户修正
    assert response.status_code == 400
    assert data["success"] is False
    assert "output_language" in data["error"]
    assert "仅允许为空、zh-CN 或 en" not in data["error"]

    # 异常路径触发资源清理
    assert db.rolled_back is True

    # loguru 必须记录完整 traceback（回归守护点）
    assert "Traceback" in log_output
    assert "ValueError" in log_output
    assert "output_language 仅允许为空、zh-CN 或 en" in log_output


@pytest.mark.asyncio
async def test_chromadb_connection_failure_sanitizes_response_and_logs_traceback(
    monkeypatch,
    loguru_capture,
):
    """Exception 路径：响应返回固定脱敏消息，日志保留含敏感细节的完整 traceback。

    覆盖 backend/webui/routes/vector_db.py 的 ``logger.exception`` 修复（CodeQL 告警 37）——
    若回退为 ``exc_info=True``，loguru 不记录 traceback，本测试会失败。
    """
    from backend.webui.routes import vector_db

    sensitive_detail = "chromadb internal error: host=10.0.0.1 token=secret-token"
    monkeypatch.setattr(
        vector_db,
        "get_vector_store",
        Mock(side_effect=RuntimeError(sensitive_detail)),
    )

    result = await vector_db.test_chromadb(
        request=None,
        user={"sub": "admin"},
        _csrf="token",
        user_prefs={},
    )

    log_output = loguru_capture.getvalue()

    # 响应脱敏：固定通用消息，不含任何内部细节
    assert result["success"] is False
    assert result["message"] == "ChromaDB 连接测试失败，请检查服务状态"
    assert "secret-token" not in result["message"]
    assert "10.0.0.1" not in result["message"]
    assert "chromadb internal" not in result["message"]

    # loguru 必须保留完整 traceback 与原始异常细节，供服务端排查
    assert "Traceback" in log_output
    assert "RuntimeError" in log_output
    assert sensitive_detail in log_output


@pytest.mark.asyncio
async def test_user_config_unexpected_error_logs_traceback_and_sanitizes(
    loguru_capture,
):
    """Exception（非 ValueError）路径：同样记录完整 traceback 并脱敏响应。

    守护 user_config.py 同一函数内 except Exception 分支——该行原为
    ``logger.error(..., exc_info=True)``（loguru 不生效），与紧邻的 ValueError 分支
    属同一修复范围，不应遗漏。
    """
    from backend.api.v1.schemas import UserConfigUpdateRequest
    from backend.api.v1.user_config import update_user_config

    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=RuntimeError("db connection lost: pool exhausted, host=internal-db")
    )

    body = UserConfigUpdateRequest(configs={"output_language": "en"})

    response = await update_user_config(
        body,
        user={"user_id": 1, "sub": "user-1"},
        db=db,
    )

    data = json.loads(response.body)
    log_output = loguru_capture.getvalue()

    # 响应脱敏：固定通用消息，不含 DB 内部细节
    assert response.status_code == 400
    assert data["success"] is False
    assert data["error"] == "更新用户配置失败"
    assert "internal-db" not in data["error"]
    assert "pool exhausted" not in data["error"]

    # 异常路径触发资源清理
    assert db.rollback.await_count == 1

    # loguru 必须记录完整 traceback（回归守护点）
    assert "Traceback" in log_output
    assert "RuntimeError" in log_output
    assert "db connection lost" in log_output
