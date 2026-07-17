from types import SimpleNamespace

import pytest

from backend.services import sakura_agent_base as agent_base_module
from backend.services.sakura_agent_base import SakuraAgentBase
from backend.services.sakura_consolidation_agent import SakuraConsolidationAgent


@pytest.mark.asyncio
async def test_consolidation_agent_starts_conversation_with_user_task_message():
    captured = {}

    class Client:
        async def call_with_retry(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            captured["model"] = kwargs["model"]
            captured["role"] = kwargs["role"]
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(content="done", tool_calls=None),
                    )
                ]
            )

    agent = SakuraConsolidationAgent()
    agent._api_client = Client()
    agent._default_model = "test-model"

    changes = await agent.consolidate_file(
        repo=object(),
        repo_full_name="owner/repo",
        sakura_ref="main",
        target_file="SAKURA.md",
        new_reflection_files=["2026-05-31_PR372_adca028.md"],
        total_reflections=383,
        max_chars=5000,
        languages="Python: 1",
        model="test-model",
        max_iterations=1,
    )

    assert changes == {}
    messages = captured["messages"]
    assert [message["role"] for message in messages[:2]] == ["system", "user"]
    assert "SAKURA.md" in messages[1]["content"]
    assert "开始" in messages[1]["content"]
    assert captured["model"] == ""
    assert captured["role"] == "main"


@pytest.mark.asyncio
async def test_agent_conversation_does_not_report_max_iterations_after_llm_error(
    monkeypatch,
):
    logs = {"errors": [], "warnings": []}

    class Logger:
        def error(self, message, *args, **kwargs):
            logs["errors"].append(message.format(*args))

        def warning(self, message, *args, **kwargs):
            logs["warnings"].append(message.format(*args))

        def info(self, message, *args, **kwargs):
            pass

    class Client:
        async def call_with_retry(self, **kwargs):
            raise RuntimeError("upstream request failed")

    class Agent(SakuraAgentBase):
        log_prefix = "[test-agent]"

        def _ensure_client(self):
            return None

        def _get_tools(self):
            return []

    monkeypatch.setattr(agent_base_module, "logger", Logger())
    agent = Agent()
    agent._api_client = Client()

    await agent._run_agent_conversation(
        system_prompt="system instructions",
        model="test-model",
        max_iterations=50,
        initial_user_message="start",
    )

    assert any("LLM 调用失败 (iteration 0)" in error for error in logs["errors"])
    assert not any("达到最大迭代次数" in warning for warning in logs["warnings"])
