"""仓库互助 AI 摘要服务 / Star-aid AI summary service.

为展示仓库生成简短 AI 摘要。所有摘要请求统一通过 summary 角色解析。

刷新策略（见计划文档第 12 节）：

- README sha 未变化且已有摘要时不重复调用 AI。
- 总结失败时状态置 ``failed``，页面回退展示 GitHub description。
- 新仓库展示后可异步触发；也支持页面按钮手动刷新。

README 原文不会展示给用户；传给 AI 时按 ``star_aid_summary_readme_budget``
控制输入预算，避免大 README 超出模型上下文。``star_aid_summary_max_tokens``
控制输出预算，思考模型可调大以避免 content 为空。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from loguru import logger
from backend.services.ai_reviewer.api_client import AIApiClient
from sqlalchemy import select

from backend.core.config import get_dynamic_config, get_settings
from backend.models import database as db_module
from backend.models.star_aid_models import (
    SUMMARY_FAILED,
    SUMMARY_READY,
    StarAidRepository,
)
from backend.services import star_aid_github_service as gh

_SUMMARY_MAX_TOKENS = 16000


def prepare_readme_for_prompt(readme_text: str | None, *, budget: int = 6000) -> str:
    """把 README 原文准备为 AI 输入。

    这是传给 LLM 的输入预算控制，不是面向最终用户的展示
    截断——用户在页面上看到的是 AI 生成的摘要，而非 README 原文。``budget``
    来自配置项 ``star_aid_summary_readme_budget``，``0`` 表示不限制。
    """
    text = readme_text or ""
    budget = int(budget)
    if budget <= 0 or len(text) <= budget:
        return text
    return text[:budget]


def apply_summary_failure(
    repo: StarAidRepository, exc: Exception, now: datetime
) -> None:
    """记录摘要失败，不截断错误文本，由页面自行折叠/展示。"""
    repo.ai_summary_status = SUMMARY_FAILED
    repo.ai_summary_error = str(exc)
    repo.ai_summary_updated_at = now


def _parse_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split("/", 1)
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _get_summary_client() -> tuple[AIApiClient, str]:
    """获取角色驱动的摘要 client，不读取旧扁平 provider 配置。"""
    return AIApiClient(), ""


async def _get_user_language(session, user_id: int) -> str | None:
    """查询用户的 WebUIConfig.language 偏好语言；无记录或非 zh-CN/en 返回 None。"""
    from backend.models.database import WebUIConfig

    result = await session.execute(
        select(WebUIConfig.language).where(WebUIConfig.user_id == int(user_id))
    )
    row = result.first()
    if row and row[0]:
        lang = str(row[0])
        return lang if lang in ("zh-CN", "en") else None
    return None


async def _resolve_summary_language(session, owner_user_id: int | None = None) -> str:
    """摘要语言优先级：

    1. ``star_aid_summary_language`` 配置（全局覆盖）
    2. 仓库 owner 的偏好语言（``WebUIConfig.language``）——摘要跟仓库走
    3. 系统默认语言 ``default_language``
    4. 兜底 ``zh-CN``
    """
    configured = await get_dynamic_config("star_aid_summary_language")
    if configured:
        return str(configured)
    if owner_user_id is not None:
        owner_lang = await _get_user_language(session, owner_user_id)
        if owner_lang:
            return owner_lang
    settings = get_settings()
    lang = settings.default_language or "zh-CN"
    return lang if lang in ("zh-CN", "en") else "zh-CN"


def _language_label(lang: str) -> str:
    return "简体中文" if lang == "zh-CN" else "English"


def _build_prompt(
    *,
    full_name: str,
    description: str,
    topics: list[str],
    primary_language: str,
    readme_excerpt: str,
    lang: str,
) -> str:
    topics_str = ", ".join(topics) if topics else "-"
    return (
        "你是 GitHub 仓库摘要助手。请根据以下信息为仓库生成一段简短摘要。\n"
        "要求：\n"
        "- 长度 80-160 字\n"
        f"- 使用{_language_label(lang)}输出\n"
        "- 概括仓库用途、主要功能、技术栈或亮点\n"
        "- 只输出纯文本摘要，不要 Markdown 标题、不要列表前缀、不要复述仓库地址\n\n"
        f"仓库：{full_name}\n"
        f"描述：{description or '-'}\n"
        f"Topics：{topics_str}\n"
        f"主要语言：{primary_language or '-'}\n"
        f"README（节选）：\n{readme_excerpt or '-'}\n"
    )


async def generate_summary(
    *,
    full_name: str,
    description: str,
    topics: list[str],
    primary_language: str,
    readme_excerpt: str,
    lang: str,
    max_tokens: int = _SUMMARY_MAX_TOKENS,
) -> str:
    """调用 AI 生成摘要文本。

    只读取 ``message.content`` 作为摘要——思考模型的 ``reasoning_content``
    是中间推理过程，不是最终摘要，绝不作为摘要返回。若 ``content`` 为空，
    由上层 ``refresh_repository_summary`` 重试或标记 failed。
    """
    client, _ = _get_summary_client()
    prompt = _build_prompt(
        full_name=full_name,
        description=description,
        topics=topics,
        primary_language=primary_language,
        readme_excerpt=readme_excerpt,
        lang=lang,
    )
    resp = await client.call_with_retry(
        model="",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=int(max_tokens),
        role="summary",
    )
    if not resp.choices:
        logger.warning(
            "star_aid summary no choices: usage={}",
            getattr(resp, "usage", None),
        )
        return ""
    choice = resp.choices[0]
    message = choice.message
    content = (message.content or "").strip()
    reasoning = getattr(message, "reasoning_content", None) or ""
    finish_reason = getattr(choice, "finish_reason", None)
    if not content:
        # 诊断：记录空 content 的完整响应元信息，便于定位模型/API 行为
        logger.warning(
            "star_aid summary empty content: finish={}, "
            "content_len=0, reasoning_len={}, usage={}",
            finish_reason,
            len(reasoning),
            getattr(resp, "usage", None),
        )
    else:
        logger.info(
            "star_aid summary ai ok: finish={}, content_len={}",
            finish_reason,
            len(content),
        )
    return content


async def refresh_repository_summary(
    session, repository_id: int, *, force: bool = False
) -> dict:
    """刷新单个仓库的 AI 摘要。

    Returns:
        ``{"status": "ready"|"failed"|"skipped"|"disabled"|"no_token", ...}``
    """
    if not bool(await get_dynamic_config("star_aid_summary_enabled")) and not force:
        return {"status": "disabled"}

    result = await session.execute(
        select(StarAidRepository).where(StarAidRepository.id == int(repository_id))
    )
    repo = result.scalar_one_or_none()
    if repo is None:
        return {"status": "not_found"}

    owner, name = _parse_full_name(repo.full_name)
    if not owner or not name:
        return {"status": "invalid_repo"}

    # 取 README（用 owner 的 user token）
    token, _ = await gh.get_effective_access_token(session, repo.owner_user_id)
    readme_sha: str | None = None
    readme_text: str | None = None
    if token:
        readme_sha, readme_text = await gh.get_readme(token, owner, name)

    # sha 未变且已有摘要且非强制 → 跳过
    if (
        not force
        and readme_sha is not None
        and readme_sha == repo.readme_sha
        and repo.ai_summary
        and repo.ai_summary_status == SUMMARY_READY
    ):
        return {"status": "skipped"}

    now = datetime.utcnow()
    lang = await _resolve_summary_language(session, repo.owner_user_id)
    topics = json.loads(repo.topics_json) if repo.topics_json else []

    # README 输入预算（模型 context 限制），0 表示不限
    budget_raw = await get_dynamic_config("star_aid_summary_readme_budget")
    budget = int(budget_raw) if budget_raw not in (None, "") else 6000
    readme_input = prepare_readme_for_prompt(readme_text, budget=budget)
    if readme_sha is not None:
        repo.readme_sha = readme_sha

    # 摘要输出 token 预算（思考模型需要更大值，否则 reasoning 占满后 content 为空）
    max_tokens_raw = await get_dynamic_config("star_aid_summary_max_tokens")
    max_tokens = int(max_tokens_raw) if max_tokens_raw not in (None, "") else 16000

    gen_kwargs = {
        "full_name": repo.full_name,
        "description": repo.description or "",
        "topics": topics,
        "primary_language": repo.primary_language or "",
        "readme_excerpt": readme_input,
        "lang": lang,
        "max_tokens": max_tokens,
    }
    try:
        summary = await generate_summary(**gen_kwargs)
        if not summary:
            # 模型偶发返回空内容，重试一次
            summary = await generate_summary(**gen_kwargs)
    except Exception as exc:
        apply_summary_failure(repo, exc, now)
        await session.flush()
        logger.warning(
            "star_aid summary failed: repo={}, error={}", repo.full_name, exc
        )
        return {"status": "failed", "error": str(exc)}

    if not summary:
        repo.ai_summary_status = SUMMARY_FAILED
        repo.ai_summary_error = "empty_summary"
        repo.ai_summary_updated_at = now
        await session.flush()
        logger.warning("star_aid summary empty after retry: repo={}", repo.full_name)
        return {"status": "failed", "error": "empty_summary"}

    repo.ai_summary = summary
    repo.ai_summary_status = SUMMARY_READY
    repo.ai_summary_language = lang
    repo.ai_summary_error = None
    repo.ai_summary_updated_at = now
    await session.flush()
    logger.info("star_aid summary ready: repo={}", repo.full_name)
    return {"status": "ready"}


def trigger_summary_refresh(repository_id: int) -> None:
    """异步触发摘要刷新（fire-and-forget，内部新开 DB session）。"""
    asyncio.create_task(_refresh_in_background(int(repository_id)))


async def _refresh_in_background(repository_id: int) -> None:
    try:
        async with db_module.async_session() as session:
            await refresh_repository_summary(session, repository_id)
            await session.commit()
    except Exception as exc:
        logger.error(
            "star_aid background summary error: repo_id={}, error={}",
            repository_id,
            exc,
        )
