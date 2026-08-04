"""协议信封修复循环 helper 的单元测试。"""

import pytest

import backend.services.protocol_repair as pr_module
from backend.services.protocol_repair import run_protocol_repair_loop


class _FakeAIClient:
    """假 api_client：记录调用次数与返回内容。"""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def call_with_retry(self, *, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        response = self._responses.pop(0)

        class _Choice:
            class message:
                content = response

        class _Resp:
            choices = [_Choice()]

        return _Resp()


class _Tracker:
    def accumulate(self, response):
        pass


def _make_parse(results):
    """返回一个解析函数：按顺序消费 results（字符串=成功返回 dict；异常=抛出）。"""
    iterator = iter(results)

    def _parse(text):
        item = next(iterator)
        if isinstance(item, Exception):
            raise item
        return item

    return _parse


@pytest.mark.asyncio
async def test_local_parse_success_skips_model_call():
    """本地解析 final_text 成功时，零模型调用、零 SSE、直接返回。"""
    parse_fn = _make_parse([{"decision": "approve"}])
    api_client = _FakeAIClient(responses=[])
    tracker = _Tracker()

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=ValueError,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="<SAKURA_REVIEW>...valid...</SAKURA_REVIEW>",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=tracker,
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert result == {"decision": "approve"}
    assert api_client.calls == []


@pytest.mark.asyncio
async def test_first_round_repair_succeeds():
    """本地解析失败、第 1 轮模型修复成功。"""

    class _ProtocolErr(ValueError):
        pass

    parse_fn = _make_parse(
        [
            _ProtocolErr("missing DESCRIPTION"),
            {"decision": "approve"},
        ]
    )
    api_client = _FakeAIClient(responses=["<SAKURA_REVIEW>...fixed...</SAKURA_REVIEW>"])
    tracker = _Tracker()

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="<SAKURA_REVIEW>...broken...</SAKURA_REVIEW>",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=tracker,
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert result == {"decision": "approve"}
    assert len(api_client.calls) == 1
    user_msgs = [m for m in api_client.calls[0]["messages"] if m["role"] == "user"]
    assert any("missing DESCRIPTION" in m["content"] for m in user_msgs)
    assert len(api_client.calls[0]["messages"]) == 3


@pytest.mark.asyncio
async def test_all_rounds_fail_degrades():
    """3 轮模型修复全失败 → 降级。"""

    class _ProtocolErr(ValueError):
        pass

    parse_fn = _make_parse(
        [
            _ProtocolErr("err-0"),
            _ProtocolErr("err-1"),
            _ProtocolErr("err-2"),
            _ProtocolErr("err-3"),
        ]
    )
    api_client = _FakeAIClient(responses=["resp-1", "resp-2", "resp-3"])
    tracker = _Tracker()

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=tracker,
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert result == {"fallback": "err-3"}
    assert len(api_client.calls) == 3
    assert len(api_client.calls[2]["messages"]) == 7
    round3_users = [m for m in api_client.calls[2]["messages"] if m["role"] == "user"]
    assert "err-0" in round3_users[0]["content"]
    assert "err-1" in round3_users[1]["content"]
    assert "err-2" in round3_users[2]["content"]


@pytest.mark.asyncio
async def test_model_call_exception_degrades_immediately():
    """模型调用本身抛异常 → 立即降级，不继续剩余轮次。"""

    class _ProtocolErr(ValueError):
        pass

    class _BoomClient:
        async def call_with_retry(self, *, messages, **kwargs):
            raise RuntimeError("network down")

    parse_fn = _make_parse([_ProtocolErr("err-0")])

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken",
        repair_instruction="REPAIR_BASE",
        api_client=_BoomClient(),
        tracker=_Tracker(),
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert "network down" in result["fallback"]


@pytest.mark.asyncio
async def test_event_callback_emits_user_and_assistant_each_round():
    """每轮 event_callback 收到 user 修复指令 + assistant 修复输出。"""

    class _ProtocolErr(ValueError):
        pass

    events = []

    async def _capture(event_type, data):
        events.append((event_type, data["role"], data["content"]))

    parse_fn = _make_parse(
        [
            _ProtocolErr("err-0"),
            {"ok": True},
        ]
    )
    api_client = _FakeAIClient(responses=["fixed-response"])

    await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=_Tracker(),
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
        event_callback=_capture,
    )

    assert ("message", "user", events[0][2]) == events[0]
    assert "err-0" in events[0][2]
    assert ("message", "assistant", "fixed-response") == events[1]


@pytest.mark.asyncio
async def test_on_repaired_hook_called_on_success():
    """on_repaired 钩子在解析成功时被调用。"""

    class _ProtocolErr(ValueError):
        pass

    hook_calls = []

    async def _hook(original_text, repaired_text, result):
        hook_calls.append((original_text, repaired_text, result))

    parse_fn = _make_parse([{"decision": "approve"}])

    await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="<valid>",
        repair_instruction="REPAIR_BASE",
        api_client=_FakeAIClient([]),
        tracker=_Tracker(),
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
        on_repaired=_hook,
    )

    assert len(hook_calls) == 1
    assert hook_calls[0][0] == "<valid>"  # original
    assert hook_calls[0][1] == "<valid>"  # repaired（快路径下相同）
    assert hook_calls[0][2] == {"decision": "approve"}


@pytest.mark.asyncio
async def test_on_repaired_hook_receives_original_and_repaired():
    """修复成功时钩子收到原始失败文本与修复后文本。"""

    class _ProtocolErr(ValueError):
        pass

    hook_calls = []

    async def _hook(original_text, repaired_text, result):
        hook_calls.append((original_text, repaired_text, result))

    parse_fn = _make_parse([_ProtocolErr("err-0"), {"ok": True}])
    api_client = _FakeAIClient(responses=["fixed-text"])

    await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken-original",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=_Tracker(),
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
        on_repaired=_hook,
    )

    assert len(hook_calls) == 1
    assert hook_calls[0][0] == "broken-original"  # original
    assert hook_calls[0][1] == "fixed-text"  # repaired
    assert hook_calls[0][2] == {"ok": True}


@pytest.mark.asyncio
async def test_sse_progress_events_emitted(monkeypatch):
    """每轮修复与降级都通过 publish_event 推送 SSE 进度事件。"""

    class _ProtocolErr(ValueError):
        pass

    published = []

    async def _fake_publish(event_type, data, channel="webui:events"):
        published.append({"event_type": event_type, "data": data, "channel": channel})

    monkeypatch.setattr(pr_module, "publish_event", _fake_publish)

    parse_fn = _make_parse(
        [
            _ProtocolErr("err-0"),
            _ProtocolErr("err-1"),
            _ProtocolErr("err-2"),
            _ProtocolErr("err-3"),
        ]
    )
    api_client = _FakeAIClient(responses=["r1", "r2", "r3"])

    await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=_Tracker(),
        max_attempts=3,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert len(published) == 4
    assert published[0]["channel"] == "review:protocol_repair"
    assert published[0]["data"]["attempt"] == 1
    assert published[0]["data"]["max_attempts"] == 3
    assert published[0]["data"]["outcome"] == "attempting"
    assert published[-1]["data"]["outcome"] == "degraded"
    assert published[-1]["data"]["side"] == "review"


@pytest.mark.asyncio
async def test_max_attempts_zero_skips_loop_and_degrades():
    """max_attempts=0 时循环范围为空，直接降级，零模型调用。"""

    class _ProtocolErr(ValueError):
        pass

    parse_fn = _make_parse([_ProtocolErr("err-0")])
    api_client = _FakeAIClient(responses=[])

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=_Tracker(),
        max_attempts=0,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert result == {"fallback": "err-0"}
    assert api_client.calls == []  # 零模型调用


@pytest.mark.asyncio
async def test_max_attempts_one_single_repair_attempt():
    """max_attempts=1 只尝试一次修复，失败即降级。"""

    class _ProtocolErr(ValueError):
        pass

    parse_fn = _make_parse(
        [
            _ProtocolErr("err-0"),
            _ProtocolErr("err-1"),
        ]
    )
    api_client = _FakeAIClient(responses=["resp-1"])

    result = await run_protocol_repair_loop(
        parse_fn=parse_fn,
        error_type=_ProtocolErr,
        base_messages=[{"role": "system", "content": "sys"}],
        final_text="broken",
        repair_instruction="REPAIR_BASE",
        api_client=api_client,
        tracker=_Tracker(),
        max_attempts=1,
        fallback_result_fn=lambda e: {"fallback": str(e)},
        log_label="审查",
        sse_channel="review:protocol_repair",
    )

    assert result == {"fallback": "err-1"}
    assert len(api_client.calls) == 1  # 只调一次模型
    # 累积消息：system + assistant(final) + user(修复指令) = 3 条
    assert len(api_client.calls[0]["messages"]) == 3
