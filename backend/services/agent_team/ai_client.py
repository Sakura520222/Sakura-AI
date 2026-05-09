"""Agent 专家团队专用 AI 配置与客户端工厂"""

from dataclasses import dataclass
from typing import Any

from backend.core.config import get_dynamic_config
from backend.services.ai_reviewer.api_client import AIApiClient


@dataclass(frozen=True)
class AgentTeamAIConfig:
    """Agent 专家团队专用 AI 配置快照（不包含明文 API Key 展示用途）。"""

    provider: str
    api_base: str
    api_key: str
    model: str
    review_model: str
    summary_model: str
    temperature: float
    max_tokens: int
    timeout_seconds: int

    def validate(self) -> None:
        """验证执行所需的专用 AI 配置。"""
        missing = []
        if not self.api_base:
            missing.append("agent_team_api_base")
        if not self.api_key:
            missing.append("agent_team_api_key")
        if not self.model:
            missing.append("agent_team_model")
        if not self.review_model:
            missing.append("agent_team_review_model")
        if missing:
            raise ValueError("Agent 专家团队专用 AI 配置不完整: " + ", ".join(missing))

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


async def load_agent_team_ai_config() -> AgentTeamAIConfig:
    """从动态配置加载 Agent 专家团队专用 AI 配置。"""
    return AgentTeamAIConfig(
        provider=str(await get_dynamic_config("agent_team_model_provider") or "openai"),
        api_base=str(await get_dynamic_config("agent_team_api_base") or ""),
        api_key=str(await get_dynamic_config("agent_team_api_key") or ""),
        model=str(await get_dynamic_config("agent_team_model") or ""),
        review_model=str(await get_dynamic_config("agent_team_review_model") or ""),
        summary_model=str(await get_dynamic_config("agent_team_summary_model") or ""),
        temperature=float(await get_dynamic_config("agent_team_temperature") or 0.2),
        max_tokens=int(await get_dynamic_config("agent_team_max_tokens") or 8192),
        timeout_seconds=int(await get_dynamic_config("agent_team_timeout_seconds") or 600),
    )


async def create_agent_team_client(validate: bool = True) -> tuple[AIApiClient, AgentTeamAIConfig]:
    """创建 Agent 专家团队专用 AI 客户端。"""
    config = await load_agent_team_ai_config()
    if validate:
        config.validate()
    return AIApiClient(base_url=config.api_base, api_key=config.api_key), config
