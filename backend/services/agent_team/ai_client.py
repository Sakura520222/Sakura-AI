"""Agent 专家团队专用 AI 配置与客户端工厂"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.config import get_dynamic_config, get_settings
from backend.services.ai_reviewer.api_client import AIApiClient

_UNCONFIGURED_MODEL_VALUE = ""


async def resolve_agent_team_max_iterations(
    task_max_iterations: int | None = None,
) -> int:
    """读取 Agent 单任务最大迭代轮数（公共辅助函数）。"""
    fallback = get_settings().agent_team_max_iterations_per_task
    raw_value = await get_dynamic_config("agent_team_max_iterations_per_task")
    effective_fallback = (
        task_max_iterations if task_max_iterations is not None else fallback
    )
    try:
        value = int(raw_value if raw_value is not None else effective_fallback)
    except (TypeError, ValueError):
        value = fallback
    return max(1, value)


async def resolve_agent_team_bool_config(key: str, fallback: bool) -> bool:
    """读取布尔动态配置，保留显式 False（公共辅助函数）。"""
    value = await get_dynamic_config(key)
    if value is None:
        return fallback
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "是"}


@dataclass(frozen=True)
class AgentTeamAIConfig:
    """Agent 专家团队专用 AI 配置快照（不包含明文 API Key 展示用途）。"""

    provider: str
    api_base: str
    api_key: str = field(repr=False)
    model: str
    review_model: str
    summary_model: str
    temperature: float
    max_tokens: int
    timeout_seconds: int

    def validate(self) -> None:
        """验证执行所需的 Agent AI 配置。"""
        missing = []
        if not self.api_base:
            missing.append("agent_team_api_base 或 openai_api_base")
        if not self.api_key:
            missing.append("agent_team_api_key 或 openai_api_key")
        if not self.model:
            missing.append("agent_team_model 或 openai_model")
        if not self.review_model:
            missing.append("agent_team_review_model 或 agent_team_model/openai_model")
        if missing:
            raise ValueError("Agent 专家团队 AI 配置不完整: " + ", ".join(missing))

    def safe_snapshot(self) -> dict[str, Any]:
        """返回可持久化的脱敏配置快照。"""
        return {
            "provider": self.provider,
            "api_base": self.api_base,
            "api_key_set": bool(self.api_key),
            "model": self.model,
            "review_model": self.review_model,
            "summary_model": self.summary_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
        }

    def as_safe_dict(self) -> dict[str, Any]:
        """返回安全字典，避免调用方误把明文 api_key 序列化。"""
        return self.safe_snapshot()

    def __getstate__(self) -> dict[str, Any]:
        """pickle 等隐式序列化时仅暴露脱敏快照。"""
        return self.safe_snapshot()

    def __setstate__(self, state: dict[str, Any]) -> None:
        """从脱敏 pickle 状态重建对象。

        state 来自 safe_snapshot()，不含明文 api_key；这里有意使用
        object.__setattr__ 恢复 frozen dataclass，并始终将 api_key 置空。
        """
        object.__setattr__(self, "provider", state.get("provider", ""))
        object.__setattr__(self, "api_base", state.get("api_base", ""))
        object.__setattr__(self, "api_key", "")
        object.__setattr__(self, "model", state.get("model", ""))
        object.__setattr__(self, "review_model", state.get("review_model", ""))
        object.__setattr__(self, "summary_model", state.get("summary_model", ""))
        object.__setattr__(self, "temperature", state.get("temperature", 0.0))
        object.__setattr__(self, "max_tokens", state.get("max_tokens", 0))
        object.__setattr__(self, "timeout_seconds", state.get("timeout_seconds", 0))


def _value_or(value: Any, default: Any) -> Any:
    """仅在值为 None 时使用默认值，保留显式 0/False/空字符串。"""
    return default if value is None else value


async def _config_value(key: str, default: Any = "") -> Any:
    return _value_or(await get_dynamic_config(key), default)


def _settings_value(settings: Any, key: str, default: Any = "") -> Any:
    return _value_or(getattr(settings, key, None), default)


def _model_or_fallback(value: Any, fallback: str) -> str:
    """将空字符串视为未配置，并使用更明确的模型 fallback。"""
    return str(fallback if value == _UNCONFIGURED_MODEL_VALUE else value)


async def load_agent_team_ai_config() -> AgentTeamAIConfig:
    """从动态配置加载 Agent 专家团队 AI 配置。

    选择“复用主 AI”时使用主 AI/辅助模型配置；否则使用 Agent 独立配置。
    """
    settings = get_settings()

    selected_provider = str(await _config_value("agent_team_model_provider", "main"))
    use_main_ai = selected_provider == "main"

    if use_main_ai:
        provider = str(
            await _config_value(
                "ai_provider", _settings_value(settings, "ai_provider", "openai")
            )
        )
        api_base = str(
            await _config_value(
                "openai_api_base", _settings_value(settings, "openai_api_base", "")
            )
        )
        api_key = str(
            await _config_value(
                "openai_api_key", _settings_value(settings, "openai_api_key", "")
            )
        )
        model = str(
            await _config_value(
                "openai_model", _settings_value(settings, "openai_model", "")
            )
        )
        review_model = model
        summary_value = await _config_value(
            "summary_model", _settings_value(settings, "summary_model", "")
        )
        summary_model = _model_or_fallback(summary_value, review_model or model)
    else:
        provider = selected_provider
        api_base = str(await _config_value("agent_team_api_base", ""))
        api_key = str(await _config_value("agent_team_api_key", ""))
        model = str(await _config_value("agent_team_model", ""))
        review_value = await _config_value("agent_team_review_model", "")
        review_model = _model_or_fallback(review_value, model)
        summary_value = await _config_value("agent_team_summary_model", "")
        summary_model = _model_or_fallback(summary_value, review_model or model)

    temperature = await _config_value("agent_team_temperature", 0.2)
    max_tokens = await _config_value("agent_team_max_tokens", 8192)
    timeout_seconds = await _config_value("agent_team_timeout_seconds", 600)

    return AgentTeamAIConfig(
        provider=provider,
        api_base=api_base,
        api_key=api_key,
        model=model,
        review_model=review_model,
        summary_model=summary_model,
        temperature=float(temperature),
        max_tokens=int(max_tokens),
        timeout_seconds=int(timeout_seconds),
    )


async def create_agent_team_client(
    validate: bool = True,
) -> tuple[AIApiClient, AgentTeamAIConfig]:
    """创建 Agent 专家团队专用 AI 客户端。"""
    config = await load_agent_team_ai_config()
    if validate:
        config.validate()
    return AIApiClient(base_url=config.api_base, api_key=config.api_key), config


async def create_agent_team_summary_client(
    fallback_config: AgentTeamAIConfig | None = None,
) -> tuple[AIApiClient, str, AgentTeamAIConfig]:
    """创建 Agent 上下文压缩用辅助 AI 客户端。"""
    settings = get_settings()
    config = fallback_config or await load_agent_team_ai_config()
    summary_base = str(await _config_value("summary_api_base", settings.summary_api_base))
    summary_key = str(await _config_value("summary_api_key", settings.summary_api_key))
    summary_model = str(await _config_value("summary_model", settings.summary_model))

    if summary_base or summary_key or summary_model:
        return (
            AIApiClient(
                base_url=summary_base or config.api_base,
                api_key=summary_key or config.api_key,
            ),
            summary_model or config.summary_model or config.model,
            config,
        )

    return AIApiClient(base_url=config.api_base, api_key=config.api_key), (
        config.summary_model or config.model
    ), config
