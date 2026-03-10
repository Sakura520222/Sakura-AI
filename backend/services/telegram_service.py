"""Telegram Bot 服务层"""

from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.telegram_models import (
    TelegramUser,
    RepoSubscription,
    QuotaUsageLog,
    UserRole,
)
from backend.core.config import get_settings

settings = get_settings()


class TelegramService:
    """Telegram Bot 服务类"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def is_super_admin(self, telegram_id: int) -> bool:
        """检查是否为超级管理员"""
        return telegram_id in settings.telegram_admin_ids_list

    async def get_user_by_telegram_id(self, telegram_id: int) -> Optional[TelegramUser]:
        """通过 Telegram ID 获取用户"""
        result = await self.session.execute(
            select(TelegramUser).where(TelegramUser.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_user_by_github_username(
        self, github_username: str
    ) -> Optional[TelegramUser]:
        """通过 GitHub 用户名获取用户"""
        # 先从数据库中查找
        result = await self.session.execute(
            select(TelegramUser).where(
                and_(
                    TelegramUser.github_username == github_username,
                    TelegramUser.is_active,
                )
            )
        )
        user = result.scalar_one_or_none()

        # 如果找到用户，直接返回
        if user:
            return user

        # 如果没有找到，返回 None
        return None

    async def is_authorized_repo(self, repo_name: str) -> bool:
        """检查仓库是否已授权"""
        result = await self.session.execute(
            select(RepoSubscription).where(
                and_(
                    RepoSubscription.repo_name == repo_name, RepoSubscription.is_active
                )
            )
        )
        return result.scalar_one_or_none() is not None

    async def check_and_consume_quota(
        self, github_username: str, repo_name: str, pr_number: int
    ) -> Tuple[bool, str]:
        """检查并消耗配额（原子操作，避免并发竞态条件）

        使用数据库原子UPDATE操作，一次性完成检查和递增，
        完全避免"Check-Then-Act"竞态条件。

        Returns:
            (是否允许, 拒绝原因)
        """
        from sqlalchemy import update

        user = await self.get_user_by_github_username(github_username)
        if not user:
            return False, "用户未注册"

        # 管理员和超级管理员不受配额限制
        if user.role in ["admin", "super_admin"]:
            return True, ""

        # 重置过期配额
        await self._reset_expired_quotas(user)

        # 使用原子UPDATE操作检查并消耗配额
        # 这个操作是原子的：只有当所有配额都未超限时才会执行递增
        # 注意：MySQL 不支持 RETURNING 子句，所以分两步执行
        stmt = (
            update(TelegramUser)
            .where(
                and_(
                    TelegramUser.id == user.id,
                    TelegramUser.daily_used < TelegramUser.daily_quota,
                    TelegramUser.weekly_used < TelegramUser.weekly_quota,
                    TelegramUser.monthly_used < TelegramUser.monthly_quota,
                )
            )
            .values(
                daily_used=TelegramUser.daily_used + 1,
                weekly_used=TelegramUser.weekly_used + 1,
                monthly_used=TelegramUser.monthly_used + 1,
            )
        )

        result = await self.session.execute(stmt)
        
        # 检查是否影响了行数（如果 rowcount == 0 说明配额已用完）
        if result.rowcount == 0:
            # 重新读取用户信息以确定具体哪个配额已用完
            await self.session.refresh(user)

            if user.daily_used >= user.daily_quota:
                return False, f"每日配额已用完 ({user.daily_used}/{user.daily_quota})"
            elif user.weekly_used >= user.weekly_quota:
                return False, f"每周配额已用完 ({user.weekly_used}/{user.weekly_quota})"
            elif user.monthly_used >= user.monthly_quota:
                return (
                    False,
                    f"每月配额已用完 ({user.monthly_used}/{user.monthly_quota})",
                )
            else:
                return False, "配额已用完"

        # 记录日志
        log = QuotaUsageLog(
            telegram_user_id=user.id,
            repo_name=repo_name,
            pr_number=pr_number,
            usage_type="daily",  # 记录为每日使用（字符串）
        )
        self.session.add(log)

        await self.session.commit()
        return True, ""

    async def _reset_expired_quotas(self, user: TelegramUser):
        """重置过期的配额"""
        now = datetime.utcnow()

        # 每日重置（每天 00:00）
        if user.last_reset_daily is None or user.last_reset_daily.date() < now.date():
            user.daily_used = 0
            user.last_reset_daily = now

        # 每周重置（每周一 00:00）
        if user.last_reset_weekly is None:
            user.weekly_used = 0
            user.last_reset_weekly = now
        else:
            # 检查是否跨周
            if user.last_reset_weekly.date() < now.date():
                # 获取本周一
                week_start = now - timedelta(days=now.weekday())
                week_start = week_start.replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                if user.last_reset_weekly < week_start:
                    user.weekly_used = 0
                    user.last_reset_weekly = now

        # 每月重置（每月1日 00:00）
        if user.last_reset_monthly is None:
            user.monthly_used = 0
            user.last_reset_monthly = now
        else:
            # 检查是否跨月
            if (
                user.last_reset_monthly.month != now.month
                or user.last_reset_monthly.year != now.year
            ):
                user.monthly_used = 0
                user.last_reset_monthly = now

        await self.session.commit()

    async def add_user(
        self,
        telegram_id: int,
        github_username: str,
        role: UserRole = UserRole.USER,
        daily_quota: int = 10,
        weekly_quota: int = 50,
        monthly_quota: int = 200,
    ) -> Tuple[bool, str]:
        """添加用户"""
        # 检查是否已存在
        existing = await self.get_user_by_telegram_id(telegram_id)
        if existing:
            return False, "用户已存在"

        # 如果是超级管理员，自动设置角色为 super_admin
        if await self.is_super_admin(telegram_id):
            role = UserRole.SUPER_ADMIN

        # 将枚举转换为字符串值
        role_value = role.value if hasattr(role, "value") else role

        user = TelegramUser(
            telegram_id=telegram_id,
            github_username=github_username,
            role=role_value,  # 使用字符串值
            daily_quota=daily_quota,
            weekly_quota=weekly_quota,
            monthly_quota=monthly_quota,
        )
        self.session.add(user)
        await self.session.commit()
        return True, "用户添加成功"

    async def remove_user(self, github_username: str) -> Tuple[bool, str]:
        """移除用户"""
        user = await self.get_user_by_github_username(github_username)
        if not user:
            return False, "用户不存在"

        await self.session.delete(user)
        await self.session.commit()
        return True, "用户已移除"

    async def add_repo(self, repo_name: str, added_by: int) -> Tuple[bool, str]:
        """添加仓库到白名单"""
        # 检查是否已存在
        result = await self.session.execute(
            select(RepoSubscription).where(RepoSubscription.repo_name == repo_name)
        )
        existing = result.scalar_one_or_none()

        if existing:
            if not existing.is_active:
                existing.is_active = True
                existing.added_by = added_by
                await self.session.commit()
                return True, "仓库已重新激活"
            return False, "仓库已存在"

        repo = RepoSubscription(repo_name=repo_name, added_by=added_by)
        self.session.add(repo)
        await self.session.commit()
        return True, "仓库添加成功"

    async def remove_repo(self, repo_name: str) -> Tuple[bool, str]:
        """移除仓库（软删除）"""
        result = await self.session.execute(
            select(RepoSubscription).where(RepoSubscription.repo_name == repo_name)
        )
        repo = result.scalar_one_or_none()

        if not repo:
            return False, "仓库不存在"

        repo.is_active = False
        await self.session.commit()
        return True, "仓库已移除"

    async def set_user_quota(
        self, github_username: str, quota_type: str, limit: int
    ) -> Tuple[bool, str]:
        """设置用户配额"""
        user = await self.get_user_by_github_username(github_username)
        if not user:
            return False, "用户不存在"

        if quota_type == "daily":
            user.daily_quota = limit
        elif quota_type == "weekly":
            user.weekly_quota = limit
        elif quota_type == "monthly":
            user.monthly_quota = limit
        else:
            return False, "无效的配额类型"

        await self.session.commit()
        return True, f"配额已更新: {quota_type} = {limit}"

    async def get_user_quota_info(self, github_username: str) -> Optional[dict]:
        """获取用户配额信息"""
        user = await self.get_user_by_github_username(github_username)
        if not user:
            return None

        await self._reset_expired_quotas(user)

        return {
            "github_username": user.github_username,
            "role": user.role,  # 现在是 String 类型，不需要 .value
            "daily": {"used": user.daily_used, "limit": user.daily_quota},
            "weekly": {"used": user.weekly_used, "limit": user.weekly_quota},
            "monthly": {"used": user.monthly_used, "limit": user.monthly_quota},
        }

    async def list_all_users(self) -> List[TelegramUser]:
        """列出所有用户"""
        result = await self.session.execute(
            select(TelegramUser).where(TelegramUser.is_active)
        )
        return result.scalars().all()

    async def list_all_repos(self) -> List[RepoSubscription]:
        """列出所有仓库"""
        result = await self.session.execute(
            select(RepoSubscription).where(RepoSubscription.is_active)
        )
        return result.scalars().all()

    async def get_recent_reviews(self, limit: int = 10) -> List[dict]:
        """获取最近的审查记录"""
        from backend.models.database import PRReview

        result = await self.session.execute(
            select(PRReview).order_by(PRReview.created_at.desc()).limit(limit)
        )
        reviews = result.scalars().all()

        return [
            {
                "repo": f"{r.repo_owner}/{r.repo_name}",
                "pr_number": r.pr_id,
                "author": r.author,
                "title": r.title,
                "score": r.overall_score,
                "status": r.status,  # 现在是 String 类型，不需要 .value
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for r in reviews
        ]
