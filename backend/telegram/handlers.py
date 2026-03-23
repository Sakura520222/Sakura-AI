"""Telegram Bot 命令处理器"""

from telegram import Update
from telegram.ext import ContextTypes
from loguru import logger

from backend.models.database import init_async_db
from backend.services.telegram_service import TelegramService
from backend.models.telegram_models import UserRole
from backend.core.config import get_settings
import re

settings = get_settings()


def validate_github_repo_name(repo_name: str) -> tuple[bool, str]:
    """验证 GitHub 仓库名称格式

    Args:
        repo_name: 仓库名称，格式应为 "owner/repo"

    Returns:
        (is_valid, error_message): 验证结果和错误信息
    """
    if not repo_name:
        return False, "仓库名不能为空"

    # 检查基本格式
    if "/" not in repo_name:
        return False, "仓库名格式错误，应为 owner/repo"

    parts = repo_name.split("/")
    if len(parts) != 2:
        return False, "仓库名格式错误，只能包含一个 / 分隔符"

    owner, repo = parts

    # 检查 owner 和 repo 是否为空
    if not owner or not repo:
        return False, "owner 和 repo 名不能为空"

    # GitHub 仓库名称规则：
    # - 只能包含字母、数字、下划线、横线、点
    # - 不允许连续的点
    # - 不允许以点或横线开头/结尾
    # - owner 最大 39 字符，repo 最大 100 字符
    pattern = r"^(?!.*\.\.)[a-zA-Z0-9][a-zA-Z0-9._-]{0,38}[a-zA-Z0-9]/[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}[a-zA-Z0-9]$"

    # 简化版：允许单字符仓库名
    simple_pattern = r"^(?!.*\.\.)[a-zA-Z0-9]([a-zA-Z0-9._-]{0,38}[a-zA-Z0-9])?/[a-zA-Z0-9]([a-zA-Z0-9._-]{0,99}[a-zA-Z0-9])?$"

    if not re.match(simple_pattern, repo_name):
        return False, (
            "仓库名只能包含字母、数字、下划线、横线和点，"
            "不能以点或横线开头/结尾，不能有连续的点"
        )

    # 防止路径遍历攻击
    if ".." in repo_name:
        return False, "仓库名不能包含连续的点"

    # 检查长度限制
    if len(owner) > 39:
        return False, "owner 名最长 39 个字符"
    if len(repo) > 100:
        return False, "repo 名最长 100 个字符"

    return True, ""


def get_async_session():
    """获取异步会话"""
    from backend.models.database import async_session

    if async_session is None:
        # 如果会话未初始化，尝试初始化
        try:
            init_async_db(settings.database_url)
        except Exception as e:
            logger.error(f"无法初始化数据库会话: {e}")
            raise RuntimeError("数据库未初始化")

    return async_session()


async def check_permission(
    telegram_id: int, required_role: UserRole = UserRole.USER
) -> bool:
    """检查用户权限"""
    # 超级管理员拥有所有权限
    if telegram_id in settings.telegram_admin_ids_list:
        return True

    async with get_async_session() as session:
        service = TelegramService(session)
        user = await service.get_user_by_telegram_id(telegram_id)

        if not user or not user.is_active:
            return False

        # 检查角色权限（字符串类型）
        role_hierarchy = {
            "user": 0,
            "admin": 1,
            "super_admin": 2,
        }

        # 将枚举转换为字符串进行比较
        required_role_str = (
            required_role.value if hasattr(required_role, "value") else required_role
        )

        return role_hierarchy.get(user.role, 0) >= role_hierarchy.get(
            required_role_str, 0
        )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """开始命令"""
    telegram_id = update.effective_user.id

    async with get_async_session() as session:
        service = TelegramService(session)

        # 检查是否为超级管理员（优先检查 .env 配置）
        is_super_admin = await service.is_super_admin(telegram_id)

        if is_super_admin:
            role_text = "👑 超级管理员（.env配置）"
        else:
            user = await service.get_user_by_telegram_id(telegram_id)
            if user:
                # 将角色字符串转换为小写，支持大小写不敏感的匹配
                role_lower = user.role.lower() if user.role else "user"

                # 将角色字符串转换为更友好的显示
                role_display = {
                    "user": "普通用户",
                    "admin": "管理员",
                    "super_admin": "超级管理员",
                }.get(role_lower, user.role)
                role_text = f"👤 {role_display}"
            else:
                role_text = "❌ 未注册"

        text = (
            f"🌸 *Sakura AI Reviewer Bot*\n\n"
            f"👤 你的ID: `{telegram_id}`\n"
            f"🏷️ 角色: {role_text}\n\n"
            f"使用 /help 查看可用命令"
        )

        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """帮助命令"""
    text = (
        "🌸 *Sakura AI Reviewer Bot - 使用帮助*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*📖 基础命令（所有人可用）*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "/start - 启动 Bot 并查看你的角色\n"
        "/help - 显示此帮助信息\n"
        "/status - 查看系统状态\n"
        "/recent - 查看最近 10 条审查记录\n"
        "/myquota - 查看我的配额使用情况\n"
        "/docs_status <owner/repo> - 查看仓库文档索引状态\n"
        "/code_status <owner/repo> - 查看仓库代码索引状态\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*👨‍💼 管理员命令（ADMIN 及以上）*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*用户管理：*\n"
        "➤ /user\\_add <telegram\\_id> <github\\_username>\n"
        "   示例: /user\\_add 123456789 Sakura520222\n"
        "   说明: 添加新用户（需要 Telegram ID 和 GitHub 用户名）\n\n"
        "➤ /user\\_remove <github\\_username>\n"
        "   示例: /user\\_remove Sakura520222\n"
        "   说明: 移除指定用户\n\n"
        "➤ /users\n"
        "   说明: 列出所有注册用户\n\n"
        "*仓库管理：*\n"
        "➤ /repo\\_add <owner/repo>\n"
        "   示例: /repo\\_add Sakura520222/my-project\n"
        "   说明: 添加仓库到授权列表\n\n"
        "➤ /repo\\_remove <owner/repo>\n"
        "   示例: /repo\\_remove Sakura520222/my-project\n"
        "   说明: 从授权列表移除仓库\n\n"
        "➤ /repos\n"
        "   说明: 列出所有授权仓库\n\n"
        "*配额管理：*\n"
        "➤ /quota\\_set <github\\_username> <daily|weekly|monthly> <limit>\n"
        "   示例: /quota\\_set Sakura520222 daily 20\n"
        "   说明: 设置指定用户的配额限制\n\n"
        "*文档管理：*\n"
        "➤ /update\\_docs <owner/repo>\n"
        "   示例: /update\\_docs Sakura520222/my-project\n"
        "   说明: 手动触发仓库文档索引更新\n\n"
        "*代码管理：*\n"
        "➤ /code\\_index <owner/repo> [paths...]\n"
        "   示例: /code\\_index Sakura520222/my-project\n"
        "         /code\\_index Sakura520222/my-project src/ lib/\n"
        "   说明: 手动触发仓库代码索引\n\n"
        "➤ /code\\_status <owner/repo>\n"
        "   示例: /code\\_status Sakura520222/my-project\n"
        "   说明: 查看仓库代码索引状态\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*👑 超级管理员命令（SUPER\\_ADMIN）*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "➤ /admin\\_add <telegram\\_id> <github\\_username>\n"
        "   示例: /admin\\_add 123456789 Sakura520222\n"
        "   说明: 添加管理员用户\n\n"
        "➤ /admin\\_remove <telegram\\_id>\n"
        "   示例: /admin\\_remove 123456789\n"
        "   说明: 移除管理员\n\n"
        "➤ /review <pr\\_url>\n"
        "   示例: /review https://github.com/owner/repo/pull/123\n"
        "   说明: 手动触发 PR 审查\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*💡 提示：*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• 获取你的 Telegram ID: 发送 /start 命令查看\n"
        "• 命令中的参数使用空格分隔\n"
        "• GitHub 用户名不需要 @ 符号\n"
        "• 仓库名格式: owner/repo\n"
        "• 管理员和超级管理员不受配额限制\n"
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """系统状态命令"""
    async with get_async_session() as session:
        service = TelegramService(session)

        users = await service.list_all_users()
        repos = await service.list_all_repos()

        text = (
            "📊 *系统状态*\n\n"
            f"👥 注册用户: {len(users)} 人\n"
            f"📦 授权仓库: {len(repos)} 个\n"
            f"🤖 AI模型: {settings.openai_model}\n"
            f"🌐 应用域名: {settings.app_domain}\n"
        )

        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """最近审查记录"""
    async with get_async_session() as session:
        service = TelegramService(session)
        reviews = await service.get_recent_reviews(limit=10)

        if not reviews:
            await update.message.reply_text("暂无审查记录")
            return

        text = "📋 *最近审查记录*\n\n"
        for r in reviews:
            score_icon = "⭐" if r["score"] and r["score"] >= 7 else "⚠️"
            text += (
                f"{score_icon} *{r['repo']}* #{r['pr_number']}\n"
                f"   作者: {r['author']}\n"
                f"   评分: {r['score'] or 'N/A'}/10\n"
                f"   时间: {r['created_at']}\n\n"
            )

        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_myquota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看我的配额"""
    telegram_id = update.effective_user.id

    async with get_async_session() as session:
        service = TelegramService(session)

        # 超级管理员不受配额限制
        if await service.is_super_admin(telegram_id):
            text = "👑 *超级管理员*\n\n您不受配额限制"
            await update.message.reply_text(text, parse_mode="Markdown")
            return

        user = await service.get_user_by_telegram_id(telegram_id)
        if not user:
            await update.message.reply_text("❌ 您还未注册")
            return

        quota_info = await service.get_user_quota_info(user.github_username)
        if not quota_info:
            await update.message.reply_text("❌ 无法获取配额信息")
            return

        text = (
            f"📊 *我的配额*\n\n"
            f"👤 用户: {quota_info['github_username']}\n"
            f"🏷️ 角色: {quota_info['role']}\n\n"
            f"📅 每日: {quota_info['daily']['used']}/{quota_info['daily']['limit']}\n"
            f"📆 每周: {quota_info['weekly']['used']}/{quota_info['weekly']['limit']}\n"
            f"🗓️ 每月: {quota_info['monthly']['used']}/{quota_info['monthly']['limit']}"
        )

        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加管理员（仅超级管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.SUPER_ADMIN):
        await update.message.reply_text("❌ 此命令仅超级管理员可用")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "用法: /admin_add <telegram_id> <github_username>"
        )
        return

    try:
        target_id = int(context.args[0])
        github_username = context.args[1]
    except ValueError:
        await update.message.reply_text("❌ Telegram ID 必须是数字")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.add_user(
            telegram_id=target_id,
            github_username=github_username,
            role=UserRole.ADMIN,
        )

        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除管理员（仅超级管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.SUPER_ADMIN):
        await update.message.reply_text("❌ 此命令仅超级管理员可用")
        return

    if len(context.args) < 1:
        await update.message.reply_text("用法: /admin_remove <telegram_id>")
        return

    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID 必须是数字")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        user = await service.get_user_by_telegram_id(target_id)

        if not user:
            await update.message.reply_text("❌ 用户不存在")
            return

        success, message = await service.remove_user(user.github_username)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_user_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加用户（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "用法: /user_add <telegram_id> <github_username>"
        )
        return

    try:
        target_id = int(context.args[0])
        github_username = context.args[1]
    except ValueError:
        await update.message.reply_text("❌ Telegram ID 必须是数字")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.add_user(
            telegram_id=target_id,
            github_username=github_username,
            role=UserRole.USER,
        )

        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_user_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除用户（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    if len(context.args) < 1:
        await update.message.reply_text("用法: /user_remove <github_username>")
        return

    github_username = context.args[0]

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.remove_user(github_username)

        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_repo_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """添加仓库（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    if len(context.args) < 1:
        await update.message.reply_text("用法: /repo_add <owner/repo>")
        return

    repo_name = context.args[0]

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.add_repo(repo_name, telegram_id)

        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_repo_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """移除仓库（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    if len(context.args) < 1:
        await update.message.reply_text("用法: /repo_remove <owner/repo>")
        return

    repo_name = context.args[0]

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.remove_repo(repo_name)

        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_quota_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """设置用户配额（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    if len(context.args) < 3:
        await update.message.reply_text(
            "用法: /quota_set <github_username> <daily|weekly|monthly> <limit>"
        )
        return

    github_username = context.args[0]
    quota_type = context.args[1]
    limit_str = context.args[2]

    try:
        limit = int(limit_str)
    except ValueError:
        await update.message.reply_text("❌ 配额限制必须是数字")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.set_user_quota(
            github_username, quota_type, limit
        )

        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有用户（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        users = await service.list_all_users()

        if not users:
            await update.message.reply_text("暂无注册用户")
            return

        text = "👥 *注册用户*\n\n"
        for user in users:
            # 将角色字符串转换为小写，支持大小写不敏感的匹配
            role_lower = user.role.lower() if user.role else "user"

            # 将角色字符串转换为更友好的显示
            role_display = {
                "user": "普通用户",
                "admin": "管理员",
                "super_admin": "超级管理员",
            }.get(role_lower, user.role)

            role_icon = (
                "👑"
                if role_lower == "super_admin"
                else "👤"
                if role_lower == "admin"
                else "👤"
            )

            # 管理员和超级管理员不受配额限制
            if role_lower in ["admin", "super_admin"]:
                quota_text = "✅ 不受配额限制"
            else:
                quota_text = f"{user.daily_used}/{user.daily_quota}"

            text += (
                f"{role_icon} *{user.github_username}*\n"
                f"   Telegram: `{user.telegram_id}`\n"
                f"   角色: {role_display}\n"  # 使用友好的中文显示
                f"   每日配额: {quota_text}\n\n"
            )

        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_repos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """列出所有仓库（仅管理员）"""
    telegram_id = update.effective_user.id

    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        repos = await service.list_all_repos()

        if not repos:
            await update.message.reply_text("暂无授权仓库")
            return

        text = "📦 *授权仓库*\n\n"
        for repo in repos:
            text += f"• {repo.repo_name}\n"

        await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """手动触发审查（仅超级管理员）"""
    telegram_id = update.effective_user.id

    # 1. 权限检查
    if not await check_permission(telegram_id, UserRole.SUPER_ADMIN):
        await update.message.reply_text("❌ 此命令仅超级管理员可用")
        return

    # 2. 参数检查
    if len(context.args) < 1:
        await update.message.reply_text(
            "用法: /review <pr_url>\n\n"
            "示例: /review https://github.com/owner/repo/pull/123"
        )
        return

    pr_url = context.args[0]

    try:
        # 3. 发送处理中消息
        status_msg = await update.message.reply_text("⏳ 正在获取PR信息...")

        # 4. 从URL获取PR信息
        from backend.core.github_app import get_pr_info_from_url

        pr_info = await get_pr_info_from_url(pr_url)

        # 4.5 检查并删除旧的审查记录
        old_review_deleted = False
        try:
            async with get_async_session() as session:
                from backend.models.database import PRReview
                from sqlalchemy import select, and_

                # 查询旧记录
                result = await session.execute(
                    select(PRReview).where(
                        and_(
                            PRReview.repo_name == pr_info["repo_name"],
                            PRReview.pr_id == pr_info["pr_number"],
                        )
                    )
                )
                old_review = result.scalar_one_or_none()

                if old_review:
                    # 删除旧记录（级联删除关联评论）
                    await session.delete(old_review)
                    await session.commit()
                    old_review_deleted = True
                    logger.info(
                        f"已删除旧审查记录: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
                        f"review_id={old_review.id}"
                    )

        except Exception as delete_error:
            # 删除失败不影响后续审查流程
            logger.warning(f"删除旧审查记录失败（将继续审查）: {delete_error}")

        # 5. 检查PR状态
        if pr_info.get("state") != "open":
            await status_msg.edit_text(
                f"❌ PR未打开\n\n"
                f"📋 PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}\n"
                f"状态: {pr_info.get('state', 'unknown')}"
            )
            return

        if pr_info.get("draft"):
            await status_msg.edit_text(
                f"❌ 这是草稿PR，跳过审查\n\n"
                f"📋 PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return

        if pr_info.get("merged"):
            await status_msg.edit_text(
                f"❌ PR已合并，跳过审查\n\n"
                f"📋 PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return

        # 6. 可选：检查仓库授权（超级管理员可以跳过）
        async with get_async_session() as session:
            # 超级管理员可以审查任何仓库，但仍然会记录到数据库
            logger.info(
                f"超级管理员手动触发审查: {telegram_id} -> "
                f"{pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )

        # 7. 发送审查开始通知
        from backend.telegram.notifications import get_notification_sender

        notification_sender = get_notification_sender()
        if notification_sender:
            await notification_sender.send_review_start(
                repo_name=pr_info["repo_full_name"],
                pr_number=pr_info["pr_number"],
                pr_title=pr_info.get("title", ""),
                author=pr_info["author"],
            )

        # 8. 提交审查任务（异步执行）
        from backend.workers.review_worker import submit_review_task

        task_id = await submit_review_task(pr_info)

        # 9. 发送确认消息
        if old_review_deleted:
            delete_notice = "\n🧹 检测到旧记录，已为您清理并重新分析..."
        else:
            delete_notice = ""

        await status_msg.edit_text(
            f"✅ 审查任务已提交{delete_notice}\n\n"
            f"📋 PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}\n"
            f"👤 作者: {pr_info['author']}\n"
            f"📝 标题: {pr_info['title'][:50]}{'...' if len(pr_info['title']) > 50 else ''}\n"
            f"🆔 任务ID: `{task_id}`\n\n"
            f"⏳ 审查完成后将通过Telegram通知您",
            parse_mode="Markdown",
        )

        logger.info(
            f"手动审查任务已提交: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
            f"task_id={task_id}, triggered_by={telegram_id}"
        )

    except ValueError as e:
        # URL格式错误
        await update.message.reply_text(f"❌ {str(e)}")
        logger.warning(f"PR URL格式错误: {pr_url}, error={e}")

    except Exception as e:
        # 其他错误
        error_msg = str(e)

        # 构建友好的错误消息
        if "访问权限" in error_msg or "installation" in error_msg.lower():
            friendly_msg = (
                f"❌ 无法访问仓库\n\n"
                f"可能原因：\n"
                f"• GitHub App 未安装到目标仓库\n"
                f"• 仓库不存在或无权限访问\n\n"
                f"错误详情: {error_msg}"
            )
        elif "Not Found" in error_msg or "不存在" in error_msg:
            friendly_msg = f"❌ PR不存在\n\n请检查PR URL是否正确\n错误详情: {error_msg}"
        else:
            friendly_msg = f"❌ 获取PR信息失败\n\n错误详情: {error_msg}"

        await update.message.reply_text(friendly_msg)
        logger.error(f"手动触发审查失败: {e}", exc_info=True)


async def cmd_update_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """更新仓库文档索引（仅管理员）"""
    telegram_id = update.effective_user.id

    # 权限检查
    if not await check_permission(telegram_id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员可用")
        return

    # 参数检查
    if len(context.args) < 1:
        await update.message.reply_text(
            "用法: /update_docs <owner/repo>\n\n"
            "示例: /update_docs Sakura520222/my-project\n\n"
            "说明: 手动触发指定仓库的文档索引更新"
        )
        return

    repo_name = context.args[0]

    try:
        # 发送处理中消息
        status_msg = await update.message.reply_text(
            f"⏳ 正在更新文档索引: {repo_name}..."
        )

        # 导入 RAG 服务
        from backend.services.rag_service import get_rag_service
        from backend.core.github_app import GitHubAppClient
        from backend.core.config import get_settings

        settings = get_settings()

        # 检查 RAG 功能是否启用
        if not settings.enable_rag:
            await status_msg.edit_text("❌ RAG 功能未启用")
            return

        # 验证仓库名称格式
        is_valid, error_msg = validate_github_repo_name(repo_name)
        if not is_valid:
            await status_msg.edit_text(f"❌ {error_msg}")
            return

        repo_owner, repo_name_only = repo_name.split("/", 1)

        # 获取仓库对象
        github_app_client = GitHubAppClient()
        client = github_app_client.get_repo_client(repo_owner, repo_name_only)
        if not client:
            await status_msg.edit_text(f"❌ 无法访问仓库: {repo_name}")
            return

        repo = client.get_repo(repo_name)

        # 克隆仓库到临时目录
        import tempfile
        import shutil

        temp_dir = tempfile.mkdtemp()
        try:
            # 使用 git clone
            import subprocess

            clone_url = repo.clone_url.replace(
                "https://", f"https://x-access-token:{settings.github_app_id}@"
            )

            subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, temp_dir],
                check=True,
                capture_output=True,
                timeout=60,
            )

            # 执行索引
            rag_service = get_rag_service()
            result = await rag_service.index_repository_docs(repo_name, temp_dir)

            # 构建结果消息
            result_text = (
                f"✅ 文档索引更新完成\n\n"
                f"📦 仓库: {repo_name}\n"
                f"📄 总文件数: {result['total_files']}\n"
                f"🆕 新增文件: {result['new_files']}\n"
                f"🔄 更新文件: {result['updated_files']}\n"
                f"🗑️  删除文件: {result['deleted_files']}\n"
                f"📦 总块数: {result['total_chunks']}\n"
            )

            await status_msg.edit_text(result_text)

        finally:
            # 清理临时目录
            shutil.rmtree(temp_dir, ignore_errors=True)

    except Exception as e:
        logger.error(f"更新文档索引失败: {str(e)}", exc_info=True)
        await status_msg.edit_text(f"❌ 更新失败: {str(e)}")


async def cmd_docs_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看仓库文档索引状态（所有人可用）"""
    # 参数检查
    if len(context.args) < 1:
        await update.message.reply_text(
            "用法: /docs_status <owner/repo>\n\n"
            "示例: /docs_status Sakura520222/my-project\n\n"
            "说明: 查看仓库的文档索引状态"
        )
        return

    repo_name = context.args[0]

    try:
        # 导入 RAG 服务
        from backend.services.rag_service import get_rag_service
        from backend.core.config import get_settings

        settings = get_settings()

        # 检查 RAG 功能是否启用
        if not settings.enable_rag:
            await update.message.reply_text("❌ RAG 功能未启用")
            return

        # 获取索引状态
        rag_service = get_rag_service()
        status = await rag_service.get_index_status(repo_name)

        # 构建状态消息
        if status.get("error"):
            text = f"❌ 获取状态失败: {status['error']}"
        elif not status.get("indexed"):
            text = (
                f"📋 文档索引状态\n\n"
                f"📦 仓库: {repo_name}\n"
                f"❓ 状态: 未索引\n"
                f"💡 提示: 请使用 /update_docs {repo_name} 创建索引"
            )
        else:
            text = (
                f"📋 文档索引状态\n\n"
                f"📦 仓库: {repo_name}\n"
                f"✅ 状态: 已索引\n"
                f"📄 文档数量: {status['document_count']} 个块\n"
            )

        await update.message.reply_text(text)

    except Exception as e:
        logger.error(f"获取文档状态失败: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 获取状态失败: {str(e)}")


async def cmd_code_index(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """索引仓库代码（管理员和超级管理员可用）"""
    # 权限检查
    if not await check_permission(update.effective_user.id, UserRole.ADMIN):
        await update.message.reply_text("❌ 此命令仅管理员和超级管理员可用")
        return

    # 参数检查
    if len(context.args) < 1:
        await update.message.reply_text(
            "用法: /code_index <owner/repo> [paths...]\n\n"
            "示例: /code_index Sakura520222/my-project\n"
            "       /code_index Sakura520222/my-project src/ lib/\n\n"
            "说明: 索引仓库的代码文件，支持指定路径"
        )
        return

    repo_name = context.args[0]

    # 验证仓库名格式
    is_valid, error_msg = validate_github_repo_name(repo_name)
    if not is_valid:
        await update.message.reply_text(f"❌ {error_msg}")
        return

    # 发送开始消息
    status_msg = await update.message.reply_text(
        f"🔍 开始索引仓库代码...\n📦 {repo_name}"
    )

    try:
        from backend.services.code_index_service import get_code_index_service

        # 获取代码索引服务
        code_index_service = get_code_index_service()

        # 获取仓库的代码索引状态
        code_count = await code_index_service.vector_store.get_collection_count(
            repo_name
        )

        # 检查代码索引状态
        async with get_async_session() as session:
            from backend.services.telegram_service import TelegramService
            from backend.models.database import CodeIndex

            service = TelegramService(session)
            repo = await service.get_repo(repo_name)

            if not repo:
                await status_msg.edit_text(f"❌ 仓库 {repo_name} 未在系统中注册")
                return

            # 查询代码索引记录
            from sqlalchemy import select

            stmt = select(CodeIndex).where(CodeIndex.repo_full_name == repo_name)
            result = await session.execute(stmt)
            code_index_record = result.scalar_one_or_none()

        # 构建状态消息
        if code_index_record:
            text = (
                f"📊 代码索引状态\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🧩 代码块: {code_count} 个\n"
                f"📁 文件数: {code_index_record.file_count}\n"
                f"🔄 状态: {code_index_record.indexing_status}\n"
                f"📅 最后索引: {code_index_record.last_indexed_at}\n"
                f"🔖 类型: {code_index_record.index_type}\n\n"
                f"💡 提示: PR审查时会自动索引变更文件"
            )
        else:
            text = (
                f"📊 代码索引状态\n\n"
                f"📦 仓库: {repo_name}\n"
                f"🧩 代码块: {code_count} 个\n"
                f"📝 状态: 尚无索引记录\n\n"
                f"💡 提示: 创建或更新PR时会自动索引变更文件"
            )

        await status_msg.edit_text(text)

    except Exception as e:
        logger.error(f"代码索引失败: {str(e)}", exc_info=True)
        await status_msg.edit_text(f"❌ 索引失败: {str(e)}")


async def cmd_code_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """查看仓库代码索引状态（所有人可用）"""
    # 参数检查
    if len(context.args) < 1:
        await update.message.reply_text(
            "用法: /code_status <owner/repo>\n\n"
            "示例: /code_status Sakura520222/my-project\n\n"
            "说明: 查看仓库的代码索引状态"
        )
        return

    repo_name = context.args[0]

    # 验证仓库名格式
    is_valid, error_msg = validate_github_repo_name(repo_name)
    if not is_valid:
        await update.message.reply_text(f"❌ {error_msg}")
        return

    try:
        from backend.services.code_index_service import get_code_index_service
        from backend.models.database import CodeIndex

        code_index_service = get_code_index_service()

        # 获取代码索引状态
        async with get_async_session() as session:
            from sqlalchemy import select

            result = await session.execute(
                select(CodeIndex).where(CodeIndex.repo_full_name == repo_name)
            )
            code_index = result.scalar_one_or_none()

        # 获取向量库中的代码块数量
        chunk_count = await code_index_service.vector_store.get_collection_count(
            repo_name
        )

        # 构建状态消息
        if not code_index:
            text = (
                f"🧩 代码索引状态\n\n"
                f"📦 仓库: {repo_name}\n"
                f"❓ 状态: 未索引\n"
                f"💡 提示: PR审查时会自动索引变更文件\n"
                f"         或使用 /code_index {repo_name} 手动索引"
            )
        else:
            text = (
                f"🧩 代码索引状态\n\n"
                f"📦 仓库: {repo_name}\n"
                f"✅ 状态: 已索引\n"
                f"📁 文件数量: {code_index.file_count} 个\n"
                f"🧩 代码块: {chunk_count} 个\n"
                f"🕐 最后更新: {code_index.last_indexed_at.strftime('%Y-%m-%d %H:%M')}\n"
            )

        await update.message.reply_text(text)

    except Exception as e:
        logger.error(f"获取代码状态失败: {e}", exc_info=True)
        await update.message.reply_text(f"❌ 获取状态失败: {str(e)}")
