"""reasoning_content 支持判定与工具循环回传测试。

Issue #529：`_append_assistant_tool_turn` 曾向
`is_model_supports_reasoning_content("")` 传入空字符串导致判定恒为
False，且判定函数的前缀列表未覆盖目录中声明 reasoning_content=True
的现役模型（deepseek-v4 / glm / qwen / kimi 系列），思考轨迹在多轮
工具链中被整体丢弃。
"""

from types import SimpleNamespace

import pytest

from backend.core.config import StrategyConfig
from backend.services.ai_reviewer.result_parser import ReviewResultParser
from backend.services.ai_reviewer.reviewer import AIReviewer
from backend.services.ai_reviewer.token_tracker import TokenTracker

VALID_REVIEW = """<SAKURA_REVIEW>
<VERSION>1</VERSION>
<SCORE>8</SCORE>
<DECISION>approve</DECISION>
<DECISION_REASON>
No blocking defects were found.
</DECISION_REASON>
<SUMMARY>
The incremental change is safe.
</SUMMARY>
<FINDINGS>
</FINDINGS>
</SAKURA_REVIEW>"""


@pytest.mark.parametrize(
    "model_name",
    [
        # 内置目录中声明 reasoning_content=True 的现役模型
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "glm-4.7",
        "glm-5.2",
        "qwen3.6-flash",
        "kimi-k2.7-code",
        # 大小写不敏感
        "DeepSeek-V4-Flash",
        # 目录未覆盖的历史/自定义模型：前缀回退仍识别
        "deepseek-r1",
        "deepseek-reasoner",
        "deepseek-r1-lite",
        "deepseek-r1-zero",
        "deepseek-r1-0528",
    ],
)
def test_model_supports_reasoning_content(model_name):
    assert StrategyConfig().is_model_supports_reasoning_content(model_name)


@pytest.mark.parametrize(
    "model_name",
    [
        "",
        # 内置目录中未声明 reasoning_content 的模型
        "gpt-5.6-sol",
        "claude-fable-5",
        "mistral-small-2603",
        # 已弃用的非推理模型（目录外，前缀不匹配）
        "deepseek-chat",
        "test-model",
    ],
)
def test_model_does_not_support_reasoning_content(model_name):
    assert not StrategyConfig().is_model_supports_reasoning_content(model_name)


class _ToolLoopFakeClient:
    """两轮响应：第一轮带 tool_calls + reasoning_content，第二轮交付审查。"""

    def __init__(
        self,
        served_by: str | None,
        role_model: str = "test-model",
        served_capabilities: SimpleNamespace | None = None,
    ):
        self._served_by = served_by
        self._role_model = role_model
        self._served_capabilities = served_capabilities
        self.calls = []

    async def resolve_role_model_context(self, role):
        assert role == "main"
        return self._role_model, 100_000

    async def call_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            tool_call = SimpleNamespace(
                id="call_1",
                type="function",
                function=SimpleNamespace(name="get_file_diff", arguments="{}"),
            )
            message = SimpleNamespace(
                content="checking diff",
                tool_calls=[tool_call],
                reasoning_content="thinking about the diff",
            )
            choice = SimpleNamespace(message=message)
            usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
            meta_kwargs = {"served_by": self._served_by or ""}
            if self._served_capabilities is not None:
                meta_kwargs["served_capabilities"] = self._served_capabilities
            meta = SimpleNamespace(**meta_kwargs)
            return SimpleNamespace(choices=[choice], usage=usage, meta=meta)
        message = SimpleNamespace(content=VALID_REVIEW, tool_calls=[])
        choice = SimpleNamespace(message=message)
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=20)
        return SimpleNamespace(choices=[choice], usage=usage)


class _NoopToolHandler:
    async def handle_tool_call(self, tool_call, repo, pr):
        return {"ok": True}


def _build_reviewer(fake_client) -> AIReviewer:
    reviewer = AIReviewer.__new__(AIReviewer)
    reviewer.api_client = fake_client
    reviewer.result_parser = ReviewResultParser()
    reviewer.tool_handler = _NoopToolHandler()
    reviewer.model_context_mgr = SimpleNamespace(
        calculate_safe_context=lambda model, threshold: 100_000
    )
    reviewer.enable_compression = False
    reviewer.context_compressor = SimpleNamespace(
        estimate_messages_tokens=lambda msgs: 10
    )
    return reviewer


def _patch_strategy_config(monkeypatch):
    strategy_config = SimpleNamespace(
        get_context_enhancement_config=dict,
        is_model_supports_reasoning_content=StrategyConfig()
        .is_model_supports_reasoning_content,
    )
    monkeypatch.setattr(
        "backend.services.ai_reviewer.reviewer.get_strategy_config",
        lambda: strategy_config,
    )


async def _run_single_tool_round(reviewer) -> list[dict]:
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "initial evidence"},
    ]
    await reviewer._run_tool_loop(
        messages=messages,
        system_prompt="system",
        strategy="standard",
        enabled_tools=[],
        repo=None,
        pr=None,
        tracker=TokenTracker(),
        context={},
    )
    return messages


@pytest.mark.asyncio
async def test_tool_loop_replays_reasoning_content_for_served_model(monkeypatch):
    """served_by 指向声明 reasoning_content 的模型时，思考轨迹必须保留。"""
    _patch_strategy_config(monkeypatch)
    fake_client = _ToolLoopFakeClient(served_by="deepseek/deepseek-v4-flash")
    reviewer = _build_reviewer(fake_client)

    messages = await _run_single_tool_round(reviewer)

    assistant_turns = [
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_turns, "工具调用轮次的 assistant 消息必须进入历史"
    assert assistant_turns[0]["reasoning_content"] == "thinking about the diff"


@pytest.mark.asyncio
async def test_tool_loop_replays_reasoning_content_for_role_model_without_served_by(
    monkeypatch,
):
    """响应缺少 served_by 时，回退到角色首选模型名判定（不再硬编码空串）。"""
    _patch_strategy_config(monkeypatch)
    fake_client = _ToolLoopFakeClient(
        served_by=None,
        role_model="deepseek-v4-flash",
    )
    reviewer = _build_reviewer(fake_client)

    messages = await _run_single_tool_round(reviewer)

    assistant_turns = [
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_turns[0]["reasoning_content"] == "thinking about the diff"


@pytest.mark.asyncio
async def test_tool_loop_drops_reasoning_content_for_unsupported_model(monkeypatch):
    """served_by 指向不支持 reasoning_content 的模型时，不回传该字段。"""
    _patch_strategy_config(monkeypatch)
    fake_client = _ToolLoopFakeClient(served_by="openai/gpt-5.6-sol")
    reviewer = _build_reviewer(fake_client)

    messages = await _run_single_tool_round(reviewer)

    assistant_turns = [
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert "reasoning_content" not in assistant_turns[0]


@pytest.mark.asyncio
async def test_tool_loop_prefers_served_capabilities_over_model_name(monkeypatch):
    """winner 有效能力（含 ai_model_override 覆盖）优先于模型名判定。

    管理员为 deepseek-v4-flash 关闭 reasoning_content 后，即使模型名判定
    为支持，也不得回传该字段。
    """
    _patch_strategy_config(monkeypatch)
    fake_client = _ToolLoopFakeClient(
        served_by="deepseek/deepseek-v4-flash",
        served_capabilities=SimpleNamespace(reasoning_content=False),
    )
    reviewer = _build_reviewer(fake_client)

    messages = await _run_single_tool_round(reviewer)

    assistant_turns = [
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert "reasoning_content" not in assistant_turns[0]


@pytest.mark.asyncio
async def test_tool_loop_uses_served_capabilities_for_custom_model(monkeypatch):
    """winner 能力声明 reasoning_content=True 时，目录外自定义模型也回传。"""
    _patch_strategy_config(monkeypatch)
    fake_client = _ToolLoopFakeClient(
        served_by="custom/my-private-model",
        served_capabilities=SimpleNamespace(reasoning_content=True),
    )
    reviewer = _build_reviewer(fake_client)

    messages = await _run_single_tool_round(reviewer)

    assistant_turns = [
        m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")
    ]
    assert assistant_turns[0]["reasoning_content"] == "thinking about the diff"


@pytest.mark.asyncio
async def test_tool_loop_syncs_served_model_to_context_compressor(monkeypatch):
    """实际 winner 模型名须同步给压缩器，避免压缩回退清理误剥保留字段。"""
    _patch_strategy_config(monkeypatch)
    fake_client = _ToolLoopFakeClient(served_by="deepseek/deepseek-v4-flash")
    reviewer = _build_reviewer(fake_client)

    await _run_single_tool_round(reviewer)

    assert reviewer.context_compressor.model == "deepseek-v4-flash"
