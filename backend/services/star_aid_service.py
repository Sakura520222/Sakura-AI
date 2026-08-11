"""仓库互助业务服务 / Star-aid business service.

负责成员状态、仓库同步与展示选择、加入/退出计划，以及共享的 star 执行与
幂等审计逻辑。GitHub 协议交互委托给 ``star_aid_github_service``，AI 摘要
委托给 ``star_aid_summary_service``（Task 5）。

核心规则（见 docs/plans/2026-06-28-repository-star-aid.md 第 10 节）：

- ``banned`` 用户不能加入、不能自动 star，但可查看页面。
- 自动 star 跳过 actor 自己 owner 的仓库。
- 自动 star 只处理 ``is_displayed=True`` 且 ``disabled_by_admin=False`` 的仓库。
- 已 star 的仓库记录 ``already_done``，不重复调用 GitHub。
- ``manual_star`` 与自动 star 共用幂等逻辑，trigger 不同。
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select

from backend.core.config import get_dynamic_config
from backend.models.star_aid_models import (
    ACTION_MANUAL_STAR,
    ACTION_STAR,
    ACTION_STATUS_ALREADY_DONE,
    ACTION_STATUS_FAILED,
    ACTION_STATUS_RATE_LIMITED,
    ACTION_STATUS_REAUTH_REQUIRED,
    ACTION_STATUS_SKIPPED,
    ACTION_STATUS_SUCCESS,
    ACTION_UNSTAR,
    MEMBER_STATUS_ACTIVE,
    MEMBER_STATUS_BANNED,
    MEMBER_STATUS_LEFT,
    TRIGGER_EXIT_CLEANUP,
    StarAidActionLog,
    StarAidMember,
    StarAidRepository,
)
from backend.services import star_aid_github_service as gh

# ========== 配置 / 辅助 ==========


async def is_feature_enabled() -> bool:
    return bool(await get_dynamic_config("star_aid_enabled"))


async def is_auto_star_enabled() -> bool:
    return bool(await get_dynamic_config("star_aid_auto_star_enabled"))


def _now() -> datetime:
    return datetime.utcnow()


def random_schedule_delay_minutes(min_interval: int, max_interval: int) -> int:
    """返回配置区间内的随机调度延迟分钟数。"""
    lo = max(1, min(int(min_interval), int(max_interval)))
    hi = max(lo, int(min_interval), int(max_interval))
    return random.randint(lo, hi)


def repository_can_be_displayed(repo: StarAidRepository) -> bool:
    """仅公开且未归档的仓库允许展示。"""
    return bool(repo.is_public) and not bool(repo.is_archived)


def repository_can_receive_star(repo: StarAidRepository) -> bool:
    """自动/手动 star 目标必须公开、展示中且未被禁用。"""
    return (
        repository_can_be_displayed(repo)
        and bool(repo.is_displayed)
        and not bool(repo.disabled_by_admin)
    )


def repo_daily_limit_allows(limit: int, *, current_count: int) -> bool:
    """检查仓库每日新增自动 star 上限是否允许继续。"""
    return int(limit) > 0 and int(current_count) < int(limit)


def _naive_now() -> datetime:
    """与 MySQL TIMESTAMP（naive）比较用的本地 UTC naive 时间。"""
    return datetime.utcnow()


def compute_activity_score(
    stars: int | None,
    pushed_at: datetime | None,
    forks: int | None = 0,
) -> float:
    """活跃度评分（见计划文档 6.5）。"""
    star_score = min(int(stars or 0), 5000) * 0.5
    recent = 0
    if pushed_at is not None:
        try:
            days = (_naive_now() - pushed_at).total_seconds() / 86400
        except TypeError:
            days = 99999
        if days <= 30:
            recent = 100
        elif days <= 90:
            recent = 60
        elif days <= 180:
            recent = 30
    fork_score = min(int(forks or 0), 1000) * 0.2
    return star_score + recent * 0.3 + fork_score


async def get_member(session, user_id: int) -> StarAidMember | None:
    result = await session.execute(
        select(StarAidMember).where(StarAidMember.user_id == int(user_id))
    )
    return result.scalar_one_or_none()


async def _credential_status(session, user_id: int) -> str:
    """判断凭据状态：none / reauth / authorized（不主动刷新 token）。"""
    cred = await gh.get_credential(session, int(user_id))
    if cred is None or cred.revoked_at is not None:
        return "none"
    now = _now()
    access_expired = (
        cred.access_token_expires_at is not None and cred.access_token_expires_at <= now
    )
    if not access_expired:
        return "authorized"
    # access 过期，看能否刷新
    if not cred.encrypted_refresh_token:
        return "reauth"
    if (
        cred.refresh_token_expires_at is not None
        and cred.refresh_token_expires_at <= now
    ):
        return "reauth"
    return "authorized"  # 可自动刷新


def _parse_full_name(full_name: str) -> tuple[str, str]:
    parts = full_name.split("/", 1)
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def _repo_to_dict(repo: StarAidRepository, *, score: float | None = None) -> dict:
    return {
        "id": repo.id,
        "full_name": repo.full_name,
        "owner_login": repo.owner_login,
        "repo_name": repo.repo_name,
        "html_url": repo.html_url,
        "description": repo.description,
        "topics": json.loads(repo.topics_json) if repo.topics_json else [],
        "primary_language": repo.primary_language,
        "stargazers_count": repo.stargazers_count or 0,
        "pushed_at": repo.pushed_at,
        "ai_summary": repo.ai_summary,
        "ai_summary_status": repo.ai_summary_status,
        "ai_summary_language": repo.ai_summary_language,
        "is_displayed": repo.is_displayed,
        "disabled_by_admin": repo.disabled_by_admin,
        "activity_score": score
        if score is not None
        else compute_activity_score(repo.stargazers_count, repo.pushed_at),
    }


# ========== 页面状态 ==========


async def get_page_state(session, user: dict) -> dict:
    """聚合仓库互助页面所需的全部状态。"""
    user_id = int(user["user_id"])
    role = user.get("role", "user")

    feature_enabled = await is_feature_enabled()
    auto_star_enabled = await is_auto_star_enabled()

    member = await get_member(session, user_id)
    cred_status = await _credential_status(session, user_id)

    # 本用户的候选仓库（refresh 入库）
    avail_result = await session.execute(
        select(StarAidRepository)
        .where(StarAidRepository.owner_user_id == user_id)
        .order_by(StarAidRepository.stargazers_count.desc())
    )
    available_repos = [r for r in avail_result.scalars().all()]

    # 全局展示池（所有参与者展示、未被管理员禁用）
    pub_result = await session.execute(
        select(StarAidRepository)
        .where(
            StarAidRepository.is_displayed.is_(True),
            StarAidRepository.disabled_by_admin.is_(False),
            StarAidRepository.is_public.is_(True),
            StarAidRepository.is_archived.is_(False),
        )
        .order_by(StarAidRepository.stargazers_count.desc())
    )
    public_repos_raw = list(pub_result.scalars().all())
    public_repos = [_repo_to_dict(r) for r in public_repos_raw]
    # 按活跃度排序（stars 相近时活跃度高的优先）
    public_repos.sort(key=lambda r: r["activity_score"], reverse=True)

    available_dicts = [_repo_to_dict(r) for r in available_repos]
    displayed_dicts = [r for r in available_dicts if r["is_displayed"]]

    # 今日用量
    daily_used = member.daily_star_used if member else 0
    daily_limit = (
        member.daily_star_limit
        if member
        else int(await get_dynamic_config("star_aid_user_daily_limit"))
    )

    # 管理员可见的成员/仓库列表
    admin_members = None
    admin_repositories = None
    if role in ("admin", "super_admin"):
        admin_members = await get_admin_members(session)
        admin_repositories = await get_admin_repositories(session)

    return {
        "feature_enabled": feature_enabled,
        "auto_star_enabled": auto_star_enabled,
        "is_admin": role in ("admin", "super_admin"),
        "is_super_admin": role == "super_admin",
        "member": _member_to_dict(member) if member else None,
        "credential_status": cred_status,
        "daily_star_used": daily_used,
        "daily_star_limit": daily_limit,
        "available_repos": available_dicts,
        "displayed_repos": displayed_dicts,
        "public_repos": public_repos,
        "admin_members": admin_members,
        "admin_repositories": admin_repositories,
    }


def _member_to_dict(member: StarAidMember) -> dict:
    return {
        "user_id": member.user_id,
        "github_username": member.github_username,
        "status": member.status,
        "joined_at": member.joined_at,
        "auto_star_enabled": member.auto_star_enabled,
        "daily_star_limit": member.daily_star_limit,
        "daily_star_used": member.daily_star_used,
        "next_scheduled_at": member.next_scheduled_at,
    }


async def get_admin_members(session) -> list[dict]:
    """管理员视角的成员列表（排除已退出）。"""
    result = await session.execute(
        select(StarAidMember)
        .where(StarAidMember.status != MEMBER_STATUS_LEFT)
        .order_by(StarAidMember.status, StarAidMember.joined_at.desc())
    )
    members = result.scalars().all()
    return [
        {
            "user_id": m.user_id,
            "github_username": m.github_username,
            "status": m.status,
            "auto_star_enabled": m.auto_star_enabled,
            "daily_star_used": m.daily_star_used,
            "daily_star_limit": m.daily_star_limit,
            "joined_at": m.joined_at,
            "banned_at": m.banned_at,
        }
        for m in members
    ]


async def get_admin_repositories(session) -> list[dict]:
    """管理员视角的仓库列表（含已禁用仓库）。"""
    result = await session.execute(
        select(StarAidRepository).order_by(
            StarAidRepository.disabled_by_admin.desc(),
            StarAidRepository.stargazers_count.desc(),
        )
    )
    return [_repo_to_dict(repo) for repo in result.scalars().all()]


# ========== 仓库同步与选择 ==========


async def refresh_available_repositories(session, user_id: int) -> dict:
    """从 GitHub App user token 同步当前用户可见的公开仓库候选列表。

    Returns:
        {"success": bool, "synced": int, "message": str}
    """
    user_id = int(user_id)
    token, result = await gh.get_effective_access_token(session, user_id)
    if token is None:
        return {
            "success": False,
            "synced": 0,
            "message": "reauth_required" if result.reauth_required else "no_token",
        }

    repos = await gh.list_user_public_repositories(token)
    synced_repo_ids: set[int] = set()
    synced = 0
    for r in repos:
        full_name = r.get("full_name") or ""
        repo_id = r.get("id")
        if repo_id is not None:
            try:
                synced_repo_ids.add(int(repo_id))
            except TypeError, ValueError:
                pass
        if not full_name:
            continue
        owner, name = _parse_full_name(full_name)
        topics = r.get("topics") or []
        pushed_raw = r.get("pushed_at")
        pushed_at = _parse_github_timestamp(pushed_raw)

        # 优先按 repo_id 查找（仓库重命名时 full_name 变、repo_id 不变），
        # 再按 full_name 兜底；避免重命名后插入撞 repo_id 唯一约束。
        repo_row = None
        if repo_id is not None:
            by_id = await session.execute(
                select(StarAidRepository).where(
                    StarAidRepository.repo_id == int(repo_id)
                )
            )
            repo_row = by_id.scalar_one_or_none()
        if repo_row is None:
            by_name = await session.execute(
                select(StarAidRepository).where(
                    StarAidRepository.full_name == full_name
                )
            )
            repo_row = by_name.scalar_one_or_none()

        if repo_row is None:
            repo_row = StarAidRepository(
                owner_user_id=user_id,
                repo_id=repo_id,
                full_name=full_name,
                owner_login=owner,
                repo_name=name,
                html_url=r.get("html_url"),
                description=r.get("description"),
                topics_json=json.dumps(topics) if topics else None,
                primary_language=(r.get("language") or None),
                stargazers_count=r.get("stargazers_count", 0) or 0,
                pushed_at=pushed_at,
                is_public=not (r.get("private")),
                is_archived=bool(r.get("archived")),
                is_displayed=False,  # 候选，待用户选择
            )
            session.add(repo_row)
        else:
            # 刷新 metadata；full_name/repo_name 可能因重命名变化
            repo_row.repo_id = repo_id or repo_row.repo_id
            repo_row.full_name = full_name
            repo_row.owner_login = owner or repo_row.owner_login
            repo_row.repo_name = name or repo_row.repo_name
            repo_row.html_url = r.get("html_url") or repo_row.html_url
            repo_row.description = r.get("description") or repo_row.description
            repo_row.topics_json = (
                json.dumps(topics) if topics else repo_row.topics_json
            )
            repo_row.primary_language = r.get("language") or repo_row.primary_language
            repo_row.stargazers_count = (
                r.get("stargazers_count", repo_row.stargazers_count) or 0
            )
            repo_row.is_public = not bool(r.get("private"))
            repo_row.is_archived = bool(r.get("archived"))
            if not repository_can_be_displayed(repo_row):
                repo_row.is_displayed = False
            repo_row.pushed_at = pushed_at or repo_row.pushed_at
        synced += 1

    await session.flush()

    # 清理：该 owner 本次同步缺失的仓库（改为 private/删除/失权）移出展示池，
    # 避免页面和调度器继续曝光 stale 仓库。
    owner_result = await session.execute(
        select(StarAidRepository).where(StarAidRepository.owner_user_id == user_id)
    )
    hidden = 0
    for repo in owner_result.scalars().all():
        still_visible = repo.repo_id in synced_repo_ids
        if not still_visible and repo.is_displayed:
            repo.is_displayed = False
            repo.is_public = False
            hidden += 1
    await session.flush()
    logger.info(
        "star_aid repos synced: user_id={}, count={}, hidden={}",
        user_id,
        synced,
        hidden,
    )
    return {"success": True, "synced": synced, "message": "ok"}


def _parse_github_timestamp(raw) -> datetime | None:
    """解析 GitHub ISO8601 时间戳（带 Z）为 naive UTC datetime。"""
    if not raw:
        return None
    try:
        # GitHub 格式："2024-01-01T12:00:00Z"
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError, TypeError:
        return None


async def select_repositories(
    session, user_id: int, selected_full_names: list[str]
) -> int:
    """更新当前用户的展示仓库选择。

    - 选中的（本用户 owner 候选）：``is_displayed=True``
    - 未选中且本用户 owner 的：``is_displayed=False``
    - 别人 owner 的仓库不受影响。

    Returns:
        本次设为展示的仓库数量。
    """
    user_id = int(user_id)
    # 只有 active 成员能修改展示仓库；未加入/已退出/被封禁用户不得写入公开池
    member = await get_member(session, user_id)
    if member is None or member.status != MEMBER_STATUS_ACTIVE:
        return 0
    selected_set = {fn for fn in selected_full_names if fn}

    result = await session.execute(
        select(StarAidRepository).where(StarAidRepository.owner_user_id == user_id)
    )
    rows = result.scalars().all()
    displayed_count = 0
    for row in rows:
        should_display = row.full_name in selected_set
        if should_display and not repository_can_be_displayed(row):
            should_display = False
        row.is_displayed = should_display
        if should_display:
            displayed_count += 1
    await session.flush()
    return displayed_count


# ========== 加入 / 退出计划 ==========


async def join_plan(
    session, user_id: int, github_username: str, selected_full_names: list[str]
) -> dict:
    """加入（或重新激活）互助计划，并设置展示仓库。"""
    user_id = int(user_id)
    if not await is_feature_enabled():
        return {"success": False, "message": "feature_disabled"}

    member = await get_member(session, user_id)
    if member and member.status == MEMBER_STATUS_BANNED:
        return {"success": False, "message": "banned"}

    # 加入前必须有可用 GitHub App user token，否则进入互助池也只能收 star、无法贡献
    token, _ = await gh.get_effective_access_token(session, user_id)
    if token is None:
        return {"success": False, "message": "reauth_required"}

    min_interval = int(await get_dynamic_config("star_aid_min_interval_minutes"))
    max_interval = int(await get_dynamic_config("star_aid_max_interval_minutes"))
    daily_limit = int(await get_dynamic_config("star_aid_user_daily_limit"))
    now = _now()

    if member is None:
        member = StarAidMember(user_id=user_id, github_username=github_username)
        session.add(member)

    member.github_username = github_username
    member.status = MEMBER_STATUS_ACTIVE
    member.joined_at = now
    member.left_at = None
    member.auto_star_enabled = True
    member.daily_star_limit = daily_limit
    # 加入时重置今日用量
    member.daily_star_used = 0
    member.last_daily_reset_at = now
    member.last_scheduled_at = None
    # 随机首次调度时间
    delay = random_schedule_delay_minutes(min_interval, max_interval)
    member.next_scheduled_at = now + timedelta(minutes=delay)

    # 设置展示仓库
    displayed = await select_repositories(session, user_id, selected_full_names)

    await session.flush()
    logger.info("star_aid join: user_id={}, displayed={}", user_id, displayed)
    return {"success": True, "message": "joined", "displayed": displayed}


async def leave_plan(session, user_id: int, *, unstar_created: bool = False) -> dict:
    """退出计划：停止自动 star 并取消本用户仓库展示。

    Args:
        unstar_created: 为 True 时批量取消此前由本功能创建的 star；
            失败不阻塞退出，只记录 action log。

    Returns:
        ``{"success": bool, "message": str, "unstar": dict | None}``
    """
    user_id = int(user_id)
    member = await get_member(session, user_id)
    if member is None or member.status == MEMBER_STATUS_LEFT:
        return {"success": False, "message": "not_joined"}
    # 被封禁成员不能通过 leave 清除封禁状态（绕过管理员封禁）
    if member.status == MEMBER_STATUS_BANNED:
        return {"success": False, "message": "banned"}

    now = _now()
    member.status = MEMBER_STATUS_LEFT
    member.left_at = now
    member.auto_star_enabled = False
    member.next_scheduled_at = None

    # 取消本用户仓库的展示
    result = await session.execute(
        select(StarAidRepository).where(StarAidRepository.owner_user_id == user_id)
    )
    for row in result.scalars().all():
        row.is_displayed = False

    unstar_summary = None
    if unstar_created:
        unstar_summary = await _unstar_created_repos(session, user_id)

    await session.flush()
    logger.info("star_aid leave: user_id={}, unstar={}", user_id, unstar_summary)
    return {"success": True, "message": "left", "unstar": unstar_summary}


async def _unstar_created_repos(session, user_id: int) -> dict:
    """退出时批量取消本功能创建的 star。

    仅对 ``action_log.created_star=True`` 的记录取消；失败不抛异常，记录
    failed 日志，不阻塞退出状态。
    """
    token, _ = await gh.get_effective_access_token(session, int(user_id))
    if token is None:
        logger.warning("star_aid exit unstar skipped (no token): user_id={}", user_id)
        return {"attempted": 0, "succeeded": 0, "failed": 0, "reason": "no_token"}

    logs_result = await session.execute(
        select(StarAidActionLog).where(
            StarAidActionLog.actor_user_id == int(user_id),
            StarAidActionLog.action.in_([ACTION_STAR, ACTION_MANUAL_STAR]),
            StarAidActionLog.created_star.is_(True),
        )
    )
    logs = list(logs_result.scalars().all())
    succeeded = 0
    failed = 0
    for log in logs:
        repo = await session.get(StarAidRepository, log.target_repository_id)
        if repo is None:
            continue
        owner, name = _parse_full_name(repo.full_name)
        if not owner or not name:
            continue
        result = await gh.unstar_repository(token, owner, name)
        if result.success:
            succeeded += 1
            log.created_star = False
            status = ACTION_STATUS_SUCCESS
            error_code = None
            error_message = None
        else:
            failed += 1
            status = ACTION_STATUS_FAILED
            error_code = result.error_code
            error_message = result.error_message
        await _upsert_action_log(
            session,
            actor_user_id=int(user_id),
            target_repository_id=repo.id,
            action=ACTION_UNSTAR,
            trigger=TRIGGER_EXIT_CLEANUP,
            status=status,
            github_status_code=result.status_code,
            error_code=error_code,
            error_message=error_message,
        )
    logger.info(
        "star_aid exit unstar: user_id={}, succeeded={}, failed={}",
        user_id,
        succeeded,
        failed,
    )
    return {
        "attempted": len(logs),
        "succeeded": succeeded,
        "failed": failed,
    }


# ========== 管理 / Administration ==========


async def ban_member(
    session, admin_user_id: int, member_user_id: int, reason: str = ""
) -> dict:
    """管理员封禁成员：停止自动 star 并标记 banned。"""
    member = await get_member(session, member_user_id)
    if member is None:
        return {"success": False, "message": "not_found"}
    member.status = MEMBER_STATUS_BANNED
    member.banned_at = _now()
    member.banned_by_user_id = int(admin_user_id)
    member.ban_reason = reason or None
    member.auto_star_enabled = False
    member.next_scheduled_at = None
    # 封禁成员的展示仓库移出公开池（不再曝光、不再接收自动 star）
    repo_result = await session.execute(
        select(StarAidRepository).where(
            StarAidRepository.owner_user_id == int(member_user_id)
        )
    )
    for repo in repo_result.scalars().all():
        repo.is_displayed = False
    await session.flush()
    logger.info("star_aid ban: member={}, admin={}", member_user_id, admin_user_id)
    return {"success": True}


async def unban_member(session, member_user_id: int) -> dict:
    """解除封禁：状态回到 left，用户可重新主动加入。"""
    member = await get_member(session, member_user_id)
    if member is None:
        return {"success": False, "message": "not_found"}
    member.status = MEMBER_STATUS_LEFT
    member.banned_at = None
    member.banned_by_user_id = None
    member.ban_reason = None
    member.next_scheduled_at = None
    await session.flush()
    logger.info("star_aid unban: member={}", member_user_id)
    return {"success": True}


async def set_repository_disabled(
    session, repository_id: int, *, disabled: bool, reason: str = ""
) -> dict:
    """管理员启用/禁用展示仓库。"""
    repo = await session.get(StarAidRepository, int(repository_id))
    if repo is None:
        return {"success": False, "message": "not_found"}
    repo.disabled_by_admin = bool(disabled)
    repo.disabled_reason = reason or None
    if disabled:
        repo.is_displayed = False
    await session.flush()
    logger.info(
        "star_aid repo disabled change: repo={}, disabled={}",
        repo.full_name,
        disabled,
    )
    return {"success": True}


# ========== 共享 star 执行 + 幂等审计 ==========


async def _upsert_action_log(
    session,
    *,
    actor_user_id: int,
    target_repository_id: int,
    action: str,
    trigger: str,
    status: str,
    github_status_code: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    created_star: bool = False,
    github_request_id: str | None = None,
) -> None:
    """幂等写入 action log：同 (actor, repo, action) 只保留最终状态。"""
    result = await session.execute(
        select(StarAidActionLog).where(
            StarAidActionLog.actor_user_id == int(actor_user_id),
            StarAidActionLog.target_repository_id == int(target_repository_id),
            StarAidActionLog.action == action,
        )
    )
    log = result.scalar_one_or_none()
    if log is None:
        log = StarAidActionLog(
            actor_user_id=int(actor_user_id),
            target_repository_id=int(target_repository_id),
            action=action,
            trigger=trigger,
            status=status,
        )
        session.add(log)
    log.trigger = trigger
    log.status = status
    log.github_status_code = github_status_code
    log.error_code = error_code
    log.error_message = error_message
    # 保留历史 created_star=True：只在本次明确成功创建时升级，
    # 不用默认 False 覆盖已有记录（否则 already_done 会把 created_star 改回 False，
    # 导致退出时漏 unstar）。取消 star 时由调用方单独置 False。
    if created_star:
        log.created_star = True
    log.github_request_id = github_request_id
    await session.flush()


async def perform_star(
    session,
    *,
    actor_user_id: int,
    repository_id: int,
    trigger: str,
    enforce_daily_limit: bool = False,
) -> dict:
    """执行一次 star 并记录幂等审计日志。

    Args:
        enforce_daily_limit: 自动 star 传 True（检查并递增每日用量）；
            手动 star 传 False（不受自动配额限制）。

    Returns:
        ``{"status": str, "created_star": bool, "reauth_required": bool,
        "rate_limited": bool, "rate_limit_reset_at": datetime|None}``
    """
    action = ACTION_MANUAL_STAR if trigger == "manual" else ACTION_STAR

    repo_result = await session.execute(
        select(StarAidRepository).where(StarAidRepository.id == int(repository_id))
    )
    repo = repo_result.scalar_one_or_none()
    if repo is None:
        await _upsert_action_log(
            session,
            actor_user_id=actor_user_id,
            target_repository_id=int(repository_id),
            action=action,
            trigger=trigger,
            status=ACTION_STATUS_SKIPPED,
            error_code="repo_not_found",
        )
        return {"status": "skipped", "created_star": False}

    # 自动/手动 star 都只能作用于合法展示仓库。
    if not repository_can_receive_star(repo):
        await _upsert_action_log(
            session,
            actor_user_id=actor_user_id,
            target_repository_id=repo.id,
            action=action,
            trigger=trigger,
            status=ACTION_STATUS_SKIPPED,
            error_code="repo_not_displayable",
        )
        return {"status": "skipped", "created_star": False}

    # 自动 star 跳过自己的仓库
    if enforce_daily_limit and repo.owner_user_id == int(actor_user_id):
        await _upsert_action_log(
            session,
            actor_user_id=actor_user_id,
            target_repository_id=repo.id,
            action=action,
            trigger=trigger,
            status=ACTION_STATUS_SKIPPED,
            error_code="own_repo",
        )
        return {"status": "skipped", "created_star": False}

    # 每日用量上限（仅自动 star）
    if enforce_daily_limit:
        member = await get_member(session, actor_user_id)
        if member is None:
            return {"status": "skipped", "created_star": False}
        if int(member.daily_star_limit or 0) <= 0:
            return {"status": "skipped", "created_star": False}
        if member.daily_star_used >= member.daily_star_limit:
            await _upsert_action_log(
                session,
                actor_user_id=actor_user_id,
                target_repository_id=repo.id,
                action=action,
                trigger=trigger,
                status=ACTION_STATUS_SKIPPED,
                error_code="user_daily_limit",
            )
            return {"status": "skipped", "created_star": False}

    # 获取有效 token（自动刷新）
    token, token_result = await gh.get_effective_access_token(
        session, int(actor_user_id)
    )
    if token is None:
        await _upsert_action_log(
            session,
            actor_user_id=actor_user_id,
            target_repository_id=repo.id,
            action=action,
            trigger=trigger,
            status=ACTION_STATUS_REAUTH_REQUIRED,
            error_code=token_result.error_code or "no_token",
        )
        return {
            "status": "reauth_required",
            "created_star": False,
            "reauth_required": True,
        }

    owner = repo.owner_login or ""
    name = repo.repo_name or ""
    if not owner or not name:
        owner, name = _parse_full_name(repo.full_name)

    # 先检查是否已 star（避免重复调用）
    check = await gh.is_starred(token, owner, name)
    if check.success:
        await _upsert_action_log(
            session,
            actor_user_id=actor_user_id,
            target_repository_id=repo.id,
            action=action,
            trigger=trigger,
            status=ACTION_STATUS_ALREADY_DONE,
            github_status_code=check.status_code,
        )
        return {"status": "already_done", "created_star": False}

    result = await gh.star_repository(token, owner, name)
    if result.success:
        if enforce_daily_limit:
            member = await get_member(session, actor_user_id)
            if member:
                member.daily_star_used = int(member.daily_star_used or 0) + 1
        await _upsert_action_log(
            session,
            actor_user_id=actor_user_id,
            target_repository_id=repo.id,
            action=action,
            trigger=trigger,
            status=ACTION_STATUS_SUCCESS,
            github_status_code=result.status_code,
            created_star=True,
        )
        logger.info(
            "star_aid star ok: actor={}, repo={}", actor_user_id, repo.full_name
        )
        return {"status": "success", "created_star": True}

    # 失败分支
    status = ACTION_STATUS_FAILED
    if result.reauth_required:
        await gh.mark_reauth_required(session, int(actor_user_id))
        status = ACTION_STATUS_REAUTH_REQUIRED
    elif result.error_code == "rate_limited":
        status = ACTION_STATUS_RATE_LIMITED

    await _upsert_action_log(
        session,
        actor_user_id=actor_user_id,
        target_repository_id=repo.id,
        action=action,
        trigger=trigger,
        status=status,
        github_status_code=result.status_code,
        error_code=result.error_code,
        error_message=result.error_message,
    )
    return {
        "status": status,
        "created_star": False,
        "reauth_required": result.reauth_required,
        "rate_limited": result.error_code == "rate_limited",
        "rate_limit_reset_at": result.rate_limit_reset_at,
    }
