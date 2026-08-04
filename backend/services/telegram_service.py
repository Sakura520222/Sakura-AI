"""Telegram Bot 服务层"""

from loguru import logger
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.models.telegram_models import (
    QuotaUsageLog,
    RepoSubscription,
    TelegramUser,
    UserRepoSubscription,
    UserRole,
)
from backend.services.payment_service import PaymentService, is_payment_enabled
from backend.services.quota_service import QuotaService

settings = get_settings()


class TelegramService:
    """Telegram Bot 服务类"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def is_super_admin(self, telegram_id: int) -> bool:
        """检查是否为超级管理员"""
        return telegram_id in settings.telegram_admin_ids_list

    async def get_user_by_telegram_id(self, telegram_id: int) -> TelegramUser | None:
        """通过 Telegram ID 获取用户"""
        result = await self.session.execute(
            select(TelegramUser).where(TelegramUser.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_user_by_github_username(
        self, github_username: str
    ) -> TelegramUser | None:
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
    ) -> tuple[bool, str]:
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
        # 转换为小写进行比较，支持大小写不敏感（与 webhook.py 保持一致）
        role_lower = user.role.lower().strip() if user.role else ""
        if role_lower in ["admin", "super_admin"]:
            logger.info(
                f"管理员/超级管理员跳过配额检查: {github_username} (role: {user.role})"
            )
            return True, ""

        # 重置过期配额
        if await is_payment_enabled():
            await PaymentService(self.session).expire_due_subscriptions(user.id)
        await QuotaService(self.session).reset_user_quotas_if_expired(
            user, include_pr=True, include_issue=False
        )

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

    async def check_and_consume_issue_quota(
        self, github_username: str, repo_name: str, issue_number: int
    ):
        """检查并消费 Issue 分析配额（独立于 PR 审查配额）"""
        from sqlalchemy import update
        from sqlalchemy.sql import and_

        user = await self.get_user_by_github_username(github_username)
        if not user:
            return False, "用户未注册"

        role_lower = user.role.lower().strip() if user.role else ""
        if role_lower in ["admin", "super_admin"]:
            return True, ""

        if await is_payment_enabled():
            await PaymentService(self.session).expire_due_subscriptions(user.id)
        await QuotaService(self.session).reset_user_quotas_if_expired(
            user, include_pr=False, include_issue=True
        )

        stmt = (
            update(TelegramUser)
            .where(
                and_(
                    TelegramUser.id == user.id,
                    TelegramUser.issue_daily_used < TelegramUser.issue_daily_quota,
                    TelegramUser.issue_weekly_used < TelegramUser.issue_weekly_quota,
                    TelegramUser.issue_monthly_used < TelegramUser.issue_monthly_quota,
                )
            )
            .values(
                issue_daily_used=TelegramUser.issue_daily_used + 1,
                issue_weekly_used=TelegramUser.issue_weekly_used + 1,
                issue_monthly_used=TelegramUser.issue_monthly_used + 1,
            )
        )

        result = await self.session.execute(stmt)

        if result.rowcount == 0:
            await self.session.refresh(user)

            if user.issue_daily_used >= user.issue_daily_quota:
                return (
                    False,
                    f"Issue 每日配额已用完 ({user.issue_daily_used}/{user.issue_daily_quota})",
                )
            elif user.issue_weekly_used >= user.issue_weekly_quota:
                return (
                    False,
                    f"Issue 每周配额已用完 ({user.issue_weekly_used}/{user.issue_weekly_quota})",
                )
            elif user.issue_monthly_used >= user.issue_monthly_quota:
                return (
                    False,
                    f"Issue 每月配额已用完 ({user.issue_monthly_used}/{user.issue_monthly_quota})",
                )
            else:
                return False, "Issue 配额已用完"

        log = QuotaUsageLog(
            telegram_user_id=user.id,
            repo_name=repo_name,
            pr_number=issue_number,
            usage_type="daily",
            usage_category="issue_analysis",
        )
        self.session.add(log)

        await self.session.commit()
        return True, ""

    async def check_and_consume_agent_quota(
        self, github_username: str, repo_name: str = "", task_id: int = 0
    ):
        """检查并消费 Agent 配额"""
        from sqlalchemy import update
        from sqlalchemy.sql import and_

        user = await self.get_user_by_github_username(github_username)
        if not user:
            return False, "用户未注册"

        role_lower = user.role.lower().strip() if user.role else ""
        if role_lower in ["admin", "super_admin"]:
            return True, ""

        if await is_payment_enabled():
            await PaymentService(self.session).expire_due_subscriptions(user.id)
        await QuotaService(self.session).reset_user_quotas_if_expired(
            user, include_pr=False, include_issue=False, include_agent=True
        )

        stmt = (
            update(TelegramUser)
            .where(
                and_(
                    TelegramUser.id == user.id,
                    TelegramUser.agent_daily_used < TelegramUser.agent_daily_quota,
                    TelegramUser.agent_weekly_used < TelegramUser.agent_weekly_quota,
                    TelegramUser.agent_monthly_used < TelegramUser.agent_monthly_quota,
                )
            )
            .values(
                agent_daily_used=TelegramUser.agent_daily_used + 1,
                agent_weekly_used=TelegramUser.agent_weekly_used + 1,
                agent_monthly_used=TelegramUser.agent_monthly_used + 1,
            )
        )

        result = await self.session.execute(stmt)

        if result.rowcount == 0:
            await self.session.refresh(user)

            if user.agent_daily_used >= user.agent_daily_quota:
                return (
                    False,
                    f"Agent 每日配额已用完 ({user.agent_daily_used}/{user.agent_daily_quota})",
                )
            elif user.agent_weekly_used >= user.agent_weekly_quota:
                return (
                    False,
                    f"Agent 每周配额已用完 ({user.agent_weekly_used}/{user.agent_weekly_quota})",
                )
            elif user.agent_monthly_used >= user.agent_monthly_quota:
                return (
                    False,
                    f"Agent 每月配额已用完 ({user.agent_monthly_used}/{user.agent_monthly_quota})",
                )
            else:
                return False, "Agent 配额已用完"

        log = QuotaUsageLog(
            telegram_user_id=user.id,
            repo_name=repo_name,
            pr_number=task_id,
            usage_type="daily",
            usage_category="agent",
        )
        self.session.add(log)

        await self.session.commit()
        return True, ""

    async def add_user(
        self,
        telegram_id: int,
        github_username: str,
        role: UserRole = UserRole.USER,
        daily_quota: int = 10,
        weekly_quota: int = 50,
        monthly_quota: int = 200,
    ) -> tuple[bool, str]:
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

    async def remove_user(self, github_username: str) -> tuple[bool, str]:
        """移除用户"""
        user = await self.get_user_by_github_username(github_username)
        if not user:
            return False, "用户不存在"

        await self.session.delete(user)
        await self.session.commit()
        return True, "用户已移除"

    async def add_repo(self, repo_name: str, added_by: int) -> tuple[bool, str]:
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

    async def remove_repo(self, repo_name: str) -> tuple[bool, str]:
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
    ) -> tuple[bool, str]:
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
        elif quota_type == "issue_daily":
            user.issue_daily_quota = limit
        elif quota_type == "issue_weekly":
            user.issue_weekly_quota = limit
        elif quota_type == "issue_monthly":
            user.issue_monthly_quota = limit
        elif quota_type == "agent_daily":
            user.agent_daily_quota = limit
        elif quota_type == "agent_weekly":
            user.agent_weekly_quota = limit
        elif quota_type == "agent_monthly":
            user.agent_monthly_quota = limit
        else:
            return False, "无效的配额类型"

        await self.session.commit()
        return True, f"配额已更新: {quota_type} = {limit}"

    async def get_user_quota_info(self, github_username: str) -> dict | None:
        """获取用户配额信息"""
        user = await self.get_user_by_github_username(github_username)
        if not user:
            return None

        if await is_payment_enabled():
            await PaymentService(self.session).expire_due_subscriptions(user.id)
        # 查询配额用于展示时同时重置 PR 和 Issue，避免 Telegram /myquota 显示跨日旧值；
        # 消耗路径仍只重置对应类型，避免不必要写入。
        await QuotaService(self.session).reset_user_quotas_if_expired(user)

        return {
            "github_username": user.github_username,
            "role": user.role,  # 现在是 String 类型，不需要 .value
            "daily": {"used": user.daily_used, "limit": user.daily_quota},
            "weekly": {"used": user.weekly_used, "limit": user.weekly_quota},
            "monthly": {"used": user.monthly_used, "limit": user.monthly_quota},
            "issue_daily": {
                "used": user.issue_daily_used,
                "limit": user.issue_daily_quota,
            },
            "issue_weekly": {
                "used": user.issue_weekly_used,
                "limit": user.issue_weekly_quota,
            },
            "issue_monthly": {
                "used": user.issue_monthly_used,
                "limit": user.issue_monthly_quota,
            },
            "agent_daily": {
                "used": user.agent_daily_used,
                "limit": user.agent_daily_quota,
            },
            "agent_weekly": {
                "used": user.agent_weekly_used,
                "limit": user.agent_weekly_quota,
            },
            "agent_monthly": {
                "used": user.agent_monthly_used,
                "limit": user.agent_monthly_quota,
            },
        }

    async def list_all_users(
        self, *, refresh_expired_quotas: bool = False
    ) -> list[TelegramUser]:
        """列出所有用户。

        refresh_expired_quotas=True 时会执行 6 条批量 UPDATE 刷新过期配额，适合需要
        展示配额数值的管理员列表；纯统计场景保持只读，避免查询命令触发写操作。
        """
        if refresh_expired_quotas:
            await QuotaService(self.session).reset_all_expired_quotas_atomic()
            self.session.expire_all()

        result = await self.session.execute(
            select(TelegramUser).where(TelegramUser.is_active)
        )
        return result.scalars().all()

    async def list_all_repos(self) -> list[RepoSubscription]:
        """列出所有仓库"""
        result = await self.session.execute(
            select(RepoSubscription).where(RepoSubscription.is_active)
        )
        return result.scalars().all()

    async def get_recent_reviews(self, limit: int = 10) -> list[dict]:
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

    async def register_user(
        self, telegram_id: int, github_username: str
    ) -> tuple[bool, str]:
        """用户自注册（配额为默认值的 register_quota_multiplier 倍）"""
        # 检查 telegram_id 是否已存在
        existing_by_id = await self.get_user_by_telegram_id(telegram_id)
        if existing_by_id:
            return (
                False,
                f"该 Telegram 账号已注册（GitHub: {existing_by_id.github_username}）",
            )

        # 检查 github_username 是否已被占用
        existing_by_github = await self.get_user_by_github_username(github_username)
        if existing_by_github:
            return False, f"GitHub 用户名 {github_username} 已被其他账号绑定"

        # 如果是超级管理员，自动设置角色为 super_admin，使用管理员配额
        if await self.is_super_admin(telegram_id):
            role = UserRole.SUPER_ADMIN
            user = TelegramUser(
                telegram_id=telegram_id,
                github_username=github_username,
                role=role.value,
                daily_quota=settings.init_admin_daily_quota,
                weekly_quota=settings.init_admin_weekly_quota,
                monthly_quota=settings.init_admin_monthly_quota,
                # 管理员 Issue/Agent 配额复用管理员 PR 初始配额
                issue_daily_quota=settings.init_admin_daily_quota,
                issue_weekly_quota=settings.init_admin_weekly_quota,
                issue_monthly_quota=settings.init_admin_monthly_quota,
                agent_daily_quota=settings.init_admin_agent_daily_quota,
                agent_weekly_quota=settings.init_admin_agent_weekly_quota,
                agent_monthly_quota=settings.init_admin_agent_monthly_quota,
            )
        else:
            role = UserRole.USER
            multiplier = settings.register_quota_multiplier
            user = TelegramUser(
                telegram_id=telegram_id,
                github_username=github_username,
                role=role.value,
                daily_quota=max(1, int(settings.init_user_daily_quota * multiplier)),
                weekly_quota=max(1, int(settings.init_user_weekly_quota * multiplier)),
                monthly_quota=max(
                    1, int(settings.init_user_monthly_quota * multiplier)
                ),
                issue_daily_quota=max(
                    1, int(settings.init_user_issue_daily_quota * multiplier)
                ),
                issue_weekly_quota=max(
                    1, int(settings.init_user_issue_weekly_quota * multiplier)
                ),
                issue_monthly_quota=max(
                    1, int(settings.init_user_issue_monthly_quota * multiplier)
                ),
                agent_daily_quota=max(
                    1, int(settings.init_user_agent_daily_quota * multiplier)
                ),
                agent_weekly_quota=max(
                    1, int(settings.init_user_agent_weekly_quota * multiplier)
                ),
                agent_monthly_quota=max(
                    1, int(settings.init_user_agent_monthly_quota * multiplier)
                ),
            )
        try:
            self.session.add(user)
            await self.session.commit()
        except Exception as e:
            await self.session.rollback()
            logger.error(f"用户注册失败: {e}", exc_info=True)
            return False, f"注册失败: {e!s}"

        if role == UserRole.SUPER_ADMIN:
            quota_info = (
                f"\n📊 管理员配额:\n"
                f"  PR: {user.daily_quota}/{user.weekly_quota}/{user.monthly_quota}（日/周/月）\n"
                f"  Issue: {user.issue_daily_quota}/{user.issue_weekly_quota}/{user.issue_monthly_quota}（日/周/月）\n"
                f"  Agent: {user.agent_daily_quota}/{user.agent_weekly_quota}/{user.agent_monthly_quota}（日/周/月）"
            )
        else:
            quota_info = (
                f"\n📊 配额（×{multiplier}）:\n"
                f"  PR: {user.daily_quota}/{user.weekly_quota}/{user.monthly_quota}（日/周/月）\n"
                f"  Issue: {user.issue_daily_quota}/{user.issue_weekly_quota}/{user.issue_monthly_quota}（日/周/月）\n"
                f"  Agent: {user.agent_daily_quota}/{user.agent_weekly_quota}/{user.agent_monthly_quota}（日/周/月）"
            )
        return True, f"注册成功{quota_info}"

    async def subscribe_repo(
        self, telegram_id: int, repo_name: str
    ) -> tuple[bool, str]:
        """用户订阅仓库"""
        # 检查用户是否存在
        user = await self.get_user_by_telegram_id(telegram_id)
        if not user:
            return False, "用户未注册，请先使用 /sign 命令注册"

        # 检查仓库是否在白名单中
        is_authorized = await self.is_authorized_repo(repo_name)
        if not is_authorized:
            return False, f"仓库 {repo_name} 未在白名单中，无法订阅"

        # 检查是否已订阅
        result = await self.session.execute(
            select(UserRepoSubscription).where(
                and_(
                    UserRepoSubscription.telegram_id == telegram_id,
                    UserRepoSubscription.repo_name == repo_name,
                )
            )
        )
        if result.scalar_one_or_none():
            return False, f"已订阅 {repo_name}"

        try:
            sub = UserRepoSubscription(telegram_id=telegram_id, repo_name=repo_name)
            self.session.add(sub)
            await self.session.commit()
        except Exception as e:
            await self.session.rollback()
            logger.error(f"订阅仓库失败: {e}", exc_info=True)
            return False, f"订阅失败: {e!s}"
        return True, f"已订阅 {repo_name}"

    async def unsubscribe_repo(
        self, telegram_id: int, repo_name: str
    ) -> tuple[bool, str]:
        """用户取消订阅仓库"""
        result = await self.session.execute(
            select(UserRepoSubscription).where(
                and_(
                    UserRepoSubscription.telegram_id == telegram_id,
                    UserRepoSubscription.repo_name == repo_name,
                )
            )
        )
        sub = result.scalar_one_or_none()
        if not sub:
            return False, f"未订阅 {repo_name}"

        try:
            await self.session.delete(sub)
            await self.session.commit()
        except Exception as e:
            await self.session.rollback()
            logger.error(f"取消订阅仓库失败: {e}", exc_info=True)
            return False, f"取消订阅失败: {e!s}"
        return True, f"已取消订阅 {repo_name}"

    async def get_notification_targets(
        self, repo_name: str, author: str = ""
    ) -> list[int]:
        """获取通知目标：作者 + 仓库订阅者（去重）"""
        chat_ids = []
        if author:
            user = await self.get_user_by_github_username(author)
            if user:
                chat_ids.append(user.telegram_id)
        subscribers = await self.get_repo_subscribers(repo_name)
        chat_ids = list(dict.fromkeys(chat_ids + subscribers))
        return chat_ids

    async def get_repo_subscribers(self, repo_name: str) -> list[int]:
        """获取仓库所有订阅者的 telegram_id 列表"""
        result = await self.session.execute(
            select(UserRepoSubscription.telegram_id).where(
                UserRepoSubscription.repo_name == repo_name
            )
        )
        return list(result.scalars().all())

    async def get_user_subscriptions(self, telegram_id: int) -> list[str]:
        """获取用户订阅的所有仓库名称"""
        result = await self.session.execute(
            select(UserRepoSubscription.repo_name).where(
                UserRepoSubscription.telegram_id == telegram_id
            )
        )
        return list(result.scalars().all())
