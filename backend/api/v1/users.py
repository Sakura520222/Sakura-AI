"""API v1 用户管理端点"""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from loguru import logger
from sqlalchemy import String, desc, func, or_, select, type_coerce
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.v1.deps import require_api_admin, require_api_super_admin
from backend.api.v1.responses import (
    error_response,
    paginated_response,
    success_response,
)
from backend.api.v1.schemas import (
    UserCreateRequest,
    UserInfoUpdateRequest,
    UserIssueQuotaUpdateRequest,
    UserQuotaUpdateRequest,
    UserResponse,
    UserRoleUpdateRequest,
)
from backend.core.config import get_settings
from backend.models.telegram_models import QuotaUsageLog, TelegramUser
from backend.services.quota_service import QuotaService
from backend.services.user_role_policy import (
    can_toggle_user_status,
    can_update_user_role,
)
from backend.webui.deps import get_db, paginate
from backend.webui.helpers.admin_log import log_admin_action

router = APIRouter(prefix="/users", tags=["Users"])


def _serialize_quota_usage_log(log: QuotaUsageLog) -> dict:
    """序列化配额使用日志。

    ``quota_type`` 是 ``usage_type`` 的别名，用于兼容前端展示。
    ``used_count`` 固定为 1，因为每条日志代表一次使用。
    """
    usage_type = log.usage_type
    return {
        "id": log.id,
        "quota_type": usage_type,
        "usage_type": usage_type,
        "usage_category": log.usage_category,
        "repo_name": log.repo_name,
        "pr_number": log.pr_number,
        "used_count": 1,
        "created_at": log.created_at.isoformat() if log.created_at else None,
    }


def _validate_user_input(telegram_id: int, github_username: str) -> str | None:
    """校验用户输入（不修改原值），返回错误信息或 None"""
    if telegram_id <= 0:
        return "Telegram ID 必须为正整数"
    if not github_username.strip():
        return "GitHub 用户名不能为空"
    return None


@router.get("")
async def list_users(
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
    search: str = Query("", description="搜索关键词"),
    role: str = Query("", description="按角色过滤"),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """用户列表（管理员）"""
    query = select(TelegramUser)
    count_query = select(func.count(TelegramUser.id))

    if search:
        escaped = search.replace("%", r"\%").replace("_", r"\_")
        search_filter = or_(
            TelegramUser.github_username.ilike(f"%{escaped}%", escape="\\"),
            type_coerce(TelegramUser.telegram_id, String).ilike(
                f"%{escaped}%", escape="\\"
            ),
        )
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if role:
        query = query.where(TelegramUser.role == role)
        count_query = count_query.where(TelegramUser.role == role)

    query = query.order_by(desc(TelegramUser.created_at))

    users, total, total_pages, page = await paginate(
        db, query, count_query, page, per_page
    )

    items = [
        UserResponse.model_validate(u, from_attributes=True).model_dump(mode="json")
        for u in users
    ]
    return paginated_response(items, total, page, total_pages, per_page)


@router.post("")
async def create_user(
    body: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """创建用户（超级管理员）"""
    if body.role not in ("user", "admin", "super_admin"):
        return error_response("无效的角色值")

    err = _validate_user_input(body.telegram_id, body.github_username)
    if err:
        return error_response(err)
    body.github_username = body.github_username.strip()

    for q in (
        body.daily_quota,
        body.weekly_quota,
        body.monthly_quota,
        body.issue_daily_quota,
        body.issue_weekly_quota,
        body.issue_monthly_quota,
    ):
        if q < 0:
            return error_response("配额值不能为负数")

    # 唯一性检查
    if (
        await db.execute(
            select(TelegramUser).where(TelegramUser.telegram_id == body.telegram_id)
        )
    ).scalar_one_or_none():
        return error_response(f"Telegram ID {body.telegram_id} 已存在")

    if (
        await db.execute(
            select(TelegramUser).where(
                TelegramUser.github_username == body.github_username
            )
        )
    ).scalar_one_or_none():
        return error_response(f"GitHub 用户名 {body.github_username} 已被使用")

    # 超级管理员自动检测
    auto_super_admin = False
    settings = get_settings()
    role = body.role
    if body.telegram_id in settings.telegram_admin_ids_list:
        role = "super_admin"
        auto_super_admin = True

    new_user = TelegramUser(
        telegram_id=body.telegram_id,
        github_username=body.github_username,
        role=role,
        daily_quota=body.daily_quota,
        weekly_quota=body.weekly_quota,
        monthly_quota=body.monthly_quota,
        issue_daily_quota=body.issue_daily_quota,
        issue_weekly_quota=body.issue_weekly_quota,
        issue_monthly_quota=body.issue_monthly_quota,
        is_active=True,
    )
    try:
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
    except IntegrityError as e:
        logger.error(f"用户创建失败（数据库冲突）: {e}")
        await db.rollback()
        return error_response("用户创建失败（可能已存在重复）")
    except Exception as e:
        logger.error(f"用户创建失败: {e}")
        await db.rollback()
        return error_response("用户创建失败")

    logger.info(
        f"API 创建用户: telegram_id={body.telegram_id}, github={body.github_username}, role={role}, by={user['sub']}"
    )
    await log_admin_action(
        db,
        user["user_id"],
        "user_add",
        "user",
        str(new_user.id),
        {
            "telegram_id": body.telegram_id,
            "github_username": body.github_username,
            "role": role,
        },
    )

    msg = f"用户 {body.github_username} 已成功添加"
    if auto_super_admin:
        msg += "（已自动提升为超级管理员）"

    data = UserResponse.model_validate(new_user, from_attributes=True).model_dump(
        mode="json"
    )
    return success_response(data=data, message=msg)


@router.get("/{user_id}")
async def get_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """用户详情"""
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    await QuotaService(db).reset_user_quotas_if_expired(target)

    data = UserResponse.model_validate(target, from_attributes=True).model_dump(
        mode="json"
    )

    # 配额使用历史（最近 20 条）
    logs_result = await db.execute(
        select(QuotaUsageLog)
        .where(QuotaUsageLog.telegram_user_id == user_id)
        .order_by(desc(QuotaUsageLog.created_at))
        .limit(20)
    )
    logs = logs_result.scalars().all()
    data["usage_logs"] = [_serialize_quota_usage_log(log) for log in logs]

    return success_response(data=data)


@router.patch("/{user_id}/role")
async def update_user_role(
    user_id: int,
    body: UserRoleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """修改用户角色"""
    if body.role not in ("user", "admin", "super_admin"):
        return error_response("无效的角色值")

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    # 权限保护
    if not can_update_user_role(user["role"], target.role, body.role):
        return error_response("权限不足，无法修改此用户的角色", status_code=403)

    old_role = target.role
    target.role = body.role
    await db.commit()

    logger.info(
        f"API 用户角色变更: {target.github_username}, {old_role}->{body.role}, by={user['sub']}"
    )
    await log_admin_action(
        db,
        user["user_id"],
        "user_role",
        "user",
        str(user_id),
        {"old_role": old_role, "new_role": body.role},
    )
    return success_response(message=f"用户角色已更改为 {body.role}")


@router.patch("/{user_id}/quota")
async def update_user_quota(
    user_id: int,
    body: UserQuotaUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """修改用户 PR 配额"""
    if body.daily_quota < 0 or body.weekly_quota < 0 or body.monthly_quota < 0:
        return error_response("配额值不能为负数")

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    old = {
        "daily": target.daily_quota,
        "weekly": target.weekly_quota,
        "monthly": target.monthly_quota,
    }
    target.daily_quota = body.daily_quota
    target.weekly_quota = body.weekly_quota
    target.monthly_quota = body.monthly_quota
    await db.commit()

    logger.info(f"API 用户配额变更: {target.github_username}, by={user['sub']}")
    await log_admin_action(
        db,
        user["user_id"],
        "user_quota",
        "user",
        str(user_id),
        {
            "old": old,
            "new": {
                "daily": body.daily_quota,
                "weekly": body.weekly_quota,
                "monthly": body.monthly_quota,
            },
        },
    )
    return success_response(message="用户配额已更新")


@router.patch("/{user_id}/issue-quota")
async def update_user_issue_quota(
    user_id: int,
    body: UserIssueQuotaUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """修改用户 Issue 配额"""
    if (
        body.issue_daily_quota < 0
        or body.issue_weekly_quota < 0
        or body.issue_monthly_quota < 0
    ):
        return error_response("配额值不能为负数")

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    target.issue_daily_quota = body.issue_daily_quota
    target.issue_weekly_quota = body.issue_weekly_quota
    target.issue_monthly_quota = body.issue_monthly_quota
    await db.commit()

    logger.info(f"API 用户 Issue 配额变更: {target.github_username}, by={user['sub']}")
    await log_admin_action(
        db, user["user_id"], "user_issue_quota", "user", str(user_id), {}
    )
    return success_response(message="Issue 配额已更新")


@router.post("/{user_id}/toggle")
async def toggle_user_status(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_admin),
):
    """启用/禁用用户"""
    if user_id == user["user_id"]:
        return error_response("不能禁用自己", status_code=400)

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    if not can_toggle_user_status(user["role"], target.role):
        return error_response("权限不足，无法修改此用户状态", status_code=403)

    target.is_active = not target.is_active
    await db.commit()

    status = "启用" if target.is_active else "禁用"
    logger.info(
        f"API 用户状态变更: {target.github_username}, {status}, by={user['sub']}"
    )
    await log_admin_action(
        db,
        user["user_id"],
        "user_toggle",
        "user",
        str(user_id),
        {"is_active": target.is_active},
    )
    return success_response(message=f"用户 {target.github_username} 已{status}")


@router.delete("/{user_id}")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """删除用户（超级管理员）"""
    if user_id == user["user_id"]:
        return error_response("不能删除自己", status_code=400)

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    github = target.github_username
    try:
        await db.delete(target)
        await db.commit()
    except Exception as e:
        logger.error(f"API 用户删除失败: {e}")
        await db.rollback()
        return error_response("用户删除失败")

    logger.info(f"API 用户已删除: id={user_id}, github={github}, by={user['sub']}")
    await log_admin_action(
        db,
        user["user_id"],
        "user_delete",
        "user",
        str(user_id),
        {"github_username": github},
    )
    return success_response(message=f"用户 {github} 已删除")


@router.patch("/{user_id}/info")
async def update_user_info(
    user_id: int,
    body: UserInfoUpdateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """修改用户基本信息（超级管理员）"""
    err = _validate_user_input(body.telegram_id, body.github_username)
    if err:
        return error_response(err)
    body.github_username = body.github_username.strip()

    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    # 唯一性检查（排除自身）
    if (
        await db.execute(
            select(TelegramUser).where(
                TelegramUser.telegram_id == body.telegram_id, TelegramUser.id != user_id
            )
        )
    ).scalar_one_or_none():
        return error_response(f"Telegram ID {body.telegram_id} 已被其他用户使用")

    if (
        await db.execute(
            select(TelegramUser).where(
                TelegramUser.github_username == body.github_username,
                TelegramUser.id != user_id,
            )
        )
    ).scalar_one_or_none():
        return error_response(f"GitHub 用户名 {body.github_username} 已被其他用户使用")

    old_github = target.github_username
    old_tg = target.telegram_id
    target.telegram_id = body.telegram_id
    target.github_username = body.github_username
    await db.commit()

    logger.info(
        f"API 用户信息变更: id={user_id}, {old_github}->{body.github_username}, by={user['sub']}"
    )
    await log_admin_action(
        db,
        user["user_id"],
        "user_info",
        "user",
        str(user_id),
        {
            "old_telegram_id": old_tg,
            "new_telegram_id": body.telegram_id,
            "old_github_username": old_github,
            "new_github_username": body.github_username,
        },
    )
    return success_response(message="用户基本信息已更新")


@router.post("/{user_id}/reset-quota")
async def reset_user_quota(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(require_api_super_admin),
):
    """重置用户配额使用量（超级管理员）"""
    result = await db.execute(select(TelegramUser).where(TelegramUser.id == user_id))
    target = result.scalar_one_or_none()
    if not target:
        return error_response("用户不存在", status_code=404)

    now = datetime.now(UTC)
    old_used = {
        "daily": target.daily_used,
        "weekly": target.weekly_used,
        "monthly": target.monthly_used,
        "issue_daily": target.issue_daily_used,
        "issue_weekly": target.issue_weekly_used,
        "issue_monthly": target.issue_monthly_used,
    }

    for field in (
        "daily_used",
        "weekly_used",
        "monthly_used",
        "issue_daily_used",
        "issue_weekly_used",
        "issue_monthly_used",
    ):
        setattr(target, field, 0)
    for field in (
        "last_reset_daily",
        "last_reset_weekly",
        "last_reset_monthly",
        "last_reset_issue_daily",
        "last_reset_issue_weekly",
        "last_reset_issue_monthly",
    ):
        setattr(target, field, now)
    await db.commit()

    logger.info(f"API 用户配额重置: {target.github_username}, by={user['sub']}")
    await log_admin_action(
        db,
        user["user_id"],
        "user_reset_quota",
        "user",
        str(user_id),
        {"old_used": old_used},
    )
    return success_response(message=f"用户 {target.github_username} 的配额使用量已重置")
