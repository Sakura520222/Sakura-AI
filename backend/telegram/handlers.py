"""Telegram Bot 命令处理器"""

from telegram import Update
from telegram.ext import ContextTypes
from loguru import logger

from backend.models.database import init_async_db
from backend.services.telegram_service import TelegramService
from backend.models.telegram_models import UserRole
from backend.core.config import get_settings

settings = get_settings()


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
        "/myquota - 查看我的配额使用情况\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*👨‍💼 管理员命令（ADMIN 及以上）*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*用户管理：*\n"
        "➤ /user_add <telegram_id> <github_username>\n"
        "   示例: /user_add 123456789 Sakura520222\n"
        "   说明: 添加新用户（需要 Telegram ID 和 GitHub 用户名）\n\n"
        "➤ /user_remove <github_username>\n"
        "   示例: /user_remove Sakura520222\n"
        "   说明: 移除指定用户\n\n"
        "➤ /users\n"
        "   说明: 列出所有注册用户\n\n"
        "*仓库管理：*\n"
        "➤ /repo_add <owner/repo>\n"
        "   示例: /repo_add Sakura520222/my-project\n"
        "   说明: 添加仓库到授权列表\n\n"
        "➤ /repo_remove <owner/repo>\n"
        "   示例: /repo_remove Sakura520222/my-project\n"
        "   说明: 从授权列表移除仓库\n\n"
        "➤ /repos\n"
        "   说明: 列出所有授权仓库\n\n"
        "*配额管理：*\n"
        "➤ /quota_set <github_username> <daily|weekly|monthly> <limit>\n"
        "   示例: /quota_set Sakura520222 daily 20\n"
        "   说明: 设置指定用户的配额限制\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "*👑 超级管理员命令（SUPER_ADMIN）*\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "➤ /admin_add <telegram_id> <github_username>\n"
        "   示例: /admin_add 123456789 Sakura520222\n"
        "   说明: 添加管理员用户\n\n"
        "➤ /admin_remove <telegram_id>\n"
        "   示例: /admin_remove 123456789\n"
        "   说明: 移除管理员\n\n"
        "➤ /review <pr_url>\n"
        "   示例: /review https://github.com/owner/repo/pull/123\n"
        "   说明: 手动触发 PR 审查（开发中）\n\n"
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

    if not await check_permission(telegram_id, UserRole.SUPER_ADMIN):
        await update.message.reply_text("❌ 此命令仅超级管理员可用")
        return

    if len(context.args) < 1:
        await update.message.reply_text("用法: /review <pr_url>")
        return

    pr_url = context.args[0]

    # TODO: 实现 PR 解析和触发逻辑
    # 这需要与现有的 webhook 集成
    await update.message.reply_text(f"🔧 手动触发功能开发中\n\nPR URL: {pr_url}")
