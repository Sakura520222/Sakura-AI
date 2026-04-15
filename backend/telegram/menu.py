"""Telegram Bot 按钮菜单系统 — InlineKeyboard + ForceReply 引导"""

import re

from loguru import logger
from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, filters

from backend.models.telegram_models import UserRole
from backend.telegram.handlers import (
    check_permission,
    get_async_session,
    validate_github_repo_name,
)
from backend.services.telegram_service import TelegramService

# ---------------------------------------------------------------------------
# Callback data 协议: action:target
#   menu:*   → 菜单导航（编辑消息）
#   exec:*   → 直接执行无参数命令（发新消息）
#   input:*  → 发起 ForceReply 引导输入
# ---------------------------------------------------------------------------

# 无参数命令 → 对应 handler 函数名（从 handlers 导入）
_EXEC_COMMANDS = {
    "status": "cmd_status",
    "recent": "cmd_recent",
    "myquota": "cmd_myquota",
    "my_subscriptions": "cmd_my_subscriptions",
    "users": "cmd_users",
    "repos": "cmd_repos",
}

# 需要参数的命令 → ForceReply 提示文本
INPUT_PROMPTS = {
    "sign": "请输入你的 GitHub 用户名：\n示例: mygithub",
    "repo_add": "请输入要添加的仓库名：\n格式: owner/repo\n示例: Sakura520222/my-project",
    "repo_remove": "请输入要移除的仓库名：\n格式: owner/repo",
    "user_add": (
        "请输入 Telegram ID 和 GitHub 用户名：\n"
        "格式: <telegram_id> <github_username>\n"
        "示例: 123456789 mygithub"
    ),
    "user_remove": "请输入要移除的 GitHub 用户名：\n示例: mygithub",
    "admin_add": (
        "请输入 Telegram ID 和 GitHub 用户名：\n"
        "格式: <telegram_id> <github_username>"
    ),
    "admin_remove": "请输入要移除的 Telegram ID：\n示例: 123456789",
    "quota_set": (
        "请输入配额设置：\n"
        "格式: <github_username> <daily|weekly|monthly> <limit>\n"
        "示例: mygithub daily 20"
    ),
    "review": "请输入 PR URL：\n示例: https://github.com/owner/repo/pull/123",
    "update_docs": "请输入仓库名：\n格式: owner/repo",
    "code_index": "请输入仓库名（可选路径）：\n格式: owner/repo [path1 path2...]",
    "docs_status": "请输入仓库名：\n格式: owner/repo",
    "code_status": "请输入仓库名：\n格式: owner/repo",
    "repo_subscribe": "请输入要订阅的仓库名：\n格式: owner/repo",
    "repo_unsubscribe": "请输入要取消订阅的仓库名：\n格式: owner/repo",
}

# 权限映射: action → 最低所需角色
_PERMISSION_MAP = {
    # 管理员+
    "exec:users": UserRole.ADMIN,
    "exec:repos": UserRole.ADMIN,
    "input:user_add": UserRole.ADMIN,
    "input:user_remove": UserRole.ADMIN,
    "input:repo_add": UserRole.ADMIN,
    "input:repo_remove": UserRole.ADMIN,
    "input:quota_set": UserRole.ADMIN,
    "input:update_docs": UserRole.ADMIN,
    "input:code_index": UserRole.ADMIN,
    # 超管
    "input:admin_add": UserRole.SUPER_ADMIN,
    "input:admin_remove": UserRole.SUPER_ADMIN,
    "input:review": UserRole.SUPER_ADMIN,
}


# ---------------------------------------------------------------------------
# 菜单构建
# ---------------------------------------------------------------------------


def build_main_menu(user_role: str) -> InlineKeyboardMarkup:
    """根据用户角色构建主菜单"""
    rows = []

    # 基础按钮（所有人）
    rows.append(
        [
            InlineKeyboardButton("📊 系统状态", callback_data="exec:status"),
            InlineKeyboardButton("🕐 最近记录", callback_data="exec:recent"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("💎 我的配额", callback_data="exec:myquota"),
            InlineKeyboardButton("📋 我的订阅", callback_data="exec:my_subscriptions"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("📚 文档状态", callback_data="input:docs_status"),
            InlineKeyboardButton("💻 代码状态", callback_data="input:code_status"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("📖 帮助", callback_data="menu:help_basic"),
            InlineKeyboardButton("📝 注册", callback_data="input:sign"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("📌 订阅仓库", callback_data="input:repo_subscribe"),
            InlineKeyboardButton("❌ 取消订阅", callback_data="input:repo_unsubscribe"),
        ]
    )

    # 管理员+ 按钮
    if user_role in ("admin", "super_admin"):
        rows.append(
            [
                InlineKeyboardButton("👥 用户管理", callback_data="menu:user_mgmt"),
                InlineKeyboardButton("📁 仓库管理", callback_data="menu:repo_mgmt"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("⚙️ 配额管理", callback_data="input:quota_set"),
                InlineKeyboardButton("🔄 文档更新", callback_data="input:update_docs"),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("🔍 代码索引", callback_data="input:code_index"),
            ]
        )

    # 超管按钮
    if user_role == "super_admin":
        rows.append(
            [
                InlineKeyboardButton("👑 管理员管理", callback_data="menu:admin_mgmt"),
                InlineKeyboardButton("🔧 手动审查", callback_data="input:review"),
            ]
        )

    return InlineKeyboardMarkup(rows)


def build_user_mgmt_menu() -> InlineKeyboardMarkup:
    """用户管理子菜单"""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ 添加用户", callback_data="input:user_add"),
                InlineKeyboardButton("➖ 移除用户", callback_data="input:user_remove"),
            ],
            [
                InlineKeyboardButton("📋 用户列表", callback_data="exec:users"),
            ],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")],
        ]
    )


def build_repo_mgmt_menu() -> InlineKeyboardMarkup:
    """仓库管理子菜单"""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ 添加仓库", callback_data="input:repo_add"),
                InlineKeyboardButton("➖ 移除仓库", callback_data="input:repo_remove"),
            ],
            [
                InlineKeyboardButton("📁 仓库列表", callback_data="exec:repos"),
            ],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")],
        ]
    )


def build_admin_mgmt_menu() -> InlineKeyboardMarkup:
    """管理员管理子菜单（超管专属）"""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ 添加管理员", callback_data="input:admin_add"),
                InlineKeyboardButton("➖ 移除管理员", callback_data="input:admin_remove"),
            ],
            [InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")],
        ]
    )


def build_help_markup(current_page: str) -> InlineKeyboardMarkup:
    """帮助页分页按钮"""
    buttons = [
        InlineKeyboardButton("📖 基础命令", callback_data="menu:help_basic"),
        InlineKeyboardButton("👨‍💼 管理员", callback_data="menu:help_admin"),
        InlineKeyboardButton("👑 超管", callback_data="menu:help_superadmin"),
    ]
    # 高亮当前页（用括号标记）
    labels = {"help_basic": "📖 基础命令", "help_admin": "👨‍💼 管理员", "help_superadmin": "👑 超管"}
    highlighted = [InlineKeyboardButton(
        f"👉 {labels[current_page]}", callback_data=f"menu:{current_page}"
    )]
    return InlineKeyboardMarkup([highlighted, buttons])


# ---------------------------------------------------------------------------
# 帮助文本
# ---------------------------------------------------------------------------

_HELP_BASIC = (
    "📖 *基础命令（所有人可用）*\n\n"
    "/start - 启动 Bot 并查看你的角色\n"
    "/status - 查看系统状态\n"
    "/recent - 查看最近 10 条审查记录\n"
    "/myquota - 查看我的配额使用情况\n"
    "/docs\\_status <owner/repo> - 文档索引状态\n"
    "/code\\_status <owner/repo> - 代码索引状态\n"
    "/sign <github\\_username> - 注册账号\n"
    "/repo\\_subscribe <owner/repo> - 订阅仓库\n"
    "/repo\\_unsubscribe <owner/repo> - 取消订阅\n"
    "/my\\_subscriptions - 查看订阅列表"
)

_HELP_ADMIN = (
    "👨‍💼 *管理员命令（ADMIN 及以上）*\n\n"
    "*用户管理：*\n"
    "/user\\_add <telegram\\_id> <github\\_username>\n"
    "/user\\_remove <github\\_username>\n"
    "/users\n\n"
    "*仓库管理：*\n"
    "/repo\\_add <owner/repo>\n"
    "/repo\\_remove <owner/repo>\n"
    "/repos\n\n"
    "*配额管理：*\n"
    "/quota\\_set <github\\_username> <daily|weekly|monthly> <limit>\n\n"
    "*索引管理：*\n"
    "/update\\_docs <owner/repo>\n"
    "/code\\_index <owner/repo> \\[paths...\\]"
)

_HELP_SUPERADMIN = (
    "👑 *超级管理员命令（SUPER\\_ADMIN）*\n\n"
    "/admin\\_add <telegram\\_id> <github\\_username>\n"
    "/admin\\_remove <telegram\\_id>\n"
    "/review <pr\\_url>"
)


# ---------------------------------------------------------------------------
# 获取用户角色
# ---------------------------------------------------------------------------


async def _get_user_role(telegram_id: int) -> str:
    """获取用户角色字符串，未注册返回空字符串"""
    async with get_async_session() as session:
        service = TelegramService(session)
        if await service.is_super_admin(telegram_id):
            return "super_admin"
        user = await service.get_user_by_telegram_id(telegram_id)
        if user and user.is_active:
            return user.role.lower().strip() if user.role else "user"
        return ""


# ---------------------------------------------------------------------------
# CallbackQuery 统一分发
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理所有 InlineKeyboard 回调"""
    query = update.callback_query
    await query.answer()

    data = query.data
    telegram_id = query.from_user.id
    action, _, target = data.partition(":")

    # ---- 权限检查 ----
    required_role = _PERMISSION_MAP.get(data)
    if required_role and not await check_permission(telegram_id, required_role):
        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔙 返回主菜单", callback_data="menu:main")]]
                )
            )
        except BadRequest:
            pass
        await query.message.reply_text("❌ 权限不足，无法使用此功能")
        return

    # ---- 菜单导航 ----
    if action == "menu":
        await _handle_menu(query, target, telegram_id, context)

    # ---- 直接执行无参数命令 ----
    elif action == "exec":
        await _handle_exec(query, target, telegram_id, context)

    # ---- 发起 ForceReply 引导 ----
    elif action == "input":
        await _handle_input(query, target, telegram_id, context)


async def _handle_menu(query, target: str, telegram_id: int, context) -> None:
    """处理菜单导航"""
    role = await _get_user_role(telegram_id)

    # 根据目标构造文本和按钮
    if target == "main":
        text = "🌸 *Sakura AI Reviewer Bot*\n请选择功能："
        markup = build_main_menu(role)
    elif target == "help_basic":
        text = _HELP_BASIC
        markup = build_help_markup("help_basic")
    elif target == "help_admin":
        text = _HELP_ADMIN
        markup = build_help_markup("help_admin")
    elif target == "help_superadmin":
        text = _HELP_SUPERADMIN
        markup = build_help_markup("help_superadmin")
    elif target == "user_mgmt":
        text = "👥 *用户管理*\n请选择操作："
        markup = build_user_mgmt_menu()
    elif target == "repo_mgmt":
        text = "📁 *仓库管理*\n请选择操作："
        markup = build_repo_mgmt_menu()
    elif target == "admin_mgmt":
        text = "👑 *管理员管理*\n请选择操作："
        markup = build_admin_mgmt_menu()
    else:
        return

    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return  # 重复点击同页按钮，忽略
        raise


async def _handle_exec(query, target: str, telegram_id: int, context) -> None:
    """直接执行无参数命令"""
    from backend.telegram import handlers

    handler_name = _EXEC_COMMANDS.get(target)
    if not handler_name:
        await query.message.reply_text("❌ 未知命令")
        return

    handler = getattr(handlers, handler_name, None)
    if not handler:
        await query.message.reply_text("❌ 命令未实现")
        return

    # 构造兼容的 mock update/context，让原 handler 正常工作
    def _default_attr(self, name):
        return None

    mock_update = type("MockUpdate", (), {
        "effective_user": query.from_user,
        "message": query.message,
        "__getattr__": _default_attr,
    })()
    mock_ctx = type("MockContext", (), {
        "args": [],
        "__getattr__": _default_attr,
    })()

    try:
        await handler(mock_update, mock_ctx)
    except Exception as e:
        logger.error(f"exec 命令执行失败: {target}, error={e}", exc_info=True)
        await query.message.reply_text(f"❌ 执行失败: {e}")


async def _handle_input(query, target: str, telegram_id: int, context) -> None:
    """发起 ForceReply 引导输入"""
    prompt = INPUT_PROMPTS.get(target)
    if not prompt:
        await query.message.reply_text("❌ 未知命令")
        return

    # 记录 pending action 到 user_data
    context.user_data["pending_action"] = f"input:{target}"

    # 删除按钮消息，避免界面杂乱
    try:
        await query.message.delete()
    except Exception:
        pass  # 删除失败不影响流程

    # 发送提示消息 + ForceReply
    await query.message.reply_text(
        f"📝 *{target.replace('_', ' ').title()}*\n\n{prompt}",
        parse_mode="Markdown",
        reply_markup=ForceReply(selective=True, input_field_placeholder="请回复..."),
    )


# ---------------------------------------------------------------------------
# ForceReply 响应处理
# ---------------------------------------------------------------------------


async def handle_force_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 ForceReply 的用户回复"""
    pending = context.user_data.get("pending_action")

    if not pending or not pending.startswith("input:"):
        return  # 不是在回应 ForceReply，忽略

    # 清除 pending 状态（无论成功失败）
    context.user_data.pop("pending_action", None)

    _, _, target = pending.partition(":")
    user_input = update.message.text.strip()

    if not user_input:
        await update.message.reply_text("❌ 输入不能为空，请重新操作")
        return

    # 根据目标分发到对应的执行逻辑
    try:
        await _execute_input_command(target, user_input, update, context)
    except Exception as e:
        logger.error(f"执行按钮命令失败: {target}, error={e}", exc_info=True)
        await update.message.reply_text(f"❌ 执行失败: {e}")


async def _execute_input_command(
    target: str, user_input: str, update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """根据 target 分发执行需要参数的命令"""
    telegram_id = update.effective_user.id
    args = user_input.split()

    if target == "sign":
        await _do_sign(update, args, telegram_id)

    elif target == "repo_add":
        await _do_repo_add(update, args, telegram_id)

    elif target == "repo_remove":
        await _do_repo_remove(update, args, telegram_id)

    elif target == "user_add":
        await _do_user_add(update, args, telegram_id)

    elif target == "user_remove":
        await _do_user_remove(update, args, telegram_id)

    elif target == "admin_add":
        await _do_admin_add(update, args, telegram_id)

    elif target == "admin_remove":
        await _do_admin_remove(update, args, telegram_id)

    elif target == "quota_set":
        await _do_quota_set(update, args, telegram_id)

    elif target == "review":
        await _do_review(update, args, telegram_id)

    elif target == "update_docs":
        await _do_update_docs(update, args, telegram_id)

    elif target == "code_index":
        await _do_code_index(update, args, telegram_id)

    elif target == "docs_status":
        await _do_docs_status(update, args)

    elif target == "code_status":
        await _do_code_status(update, args)

    elif target == "repo_subscribe":
        await _do_repo_subscribe(update, args, telegram_id)

    elif target == "repo_unsubscribe":
        await _do_repo_unsubscribe(update, args, telegram_id)

    else:
        await update.message.reply_text("❌ 未知命令")


# ---------------------------------------------------------------------------
# 各命令的具体执行逻辑（复用 handlers.py 的服务层）
# ---------------------------------------------------------------------------


async def _do_sign(update, args: list, telegram_id: int) -> None:
    """注册"""
    if not args:
        await update.message.reply_text("❌ 请提供 GitHub 用户名")
        return

    github_username = args[0].strip().lower()
    if not re.match(r"^[a-zA-Z0-9](?:[a-zA-Z0-9]|-(?=[a-zA-Z0-9])){0,38}$", github_username):
        await update.message.reply_text("❌ GitHub 用户名格式无效")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.register_user(telegram_id, github_username)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_repo_add(update, args: list, telegram_id: int) -> None:
    """添加仓库"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return

    repo_name = args[0]
    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.add_repo(repo_name, telegram_id)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_repo_remove(update, args: list, telegram_id: int) -> None:
    """移除仓库"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return

    repo_name = args[0]
    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.remove_repo(repo_name)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_user_add(update, args: list, telegram_id: int) -> None:
    """添加用户"""
    if len(args) < 2:
        await update.message.reply_text("❌ 格式: <telegram_id> <github_username>")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID 必须是数字")
        return

    github_username = args[1]
    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.add_user(
            telegram_id=target_id,
            github_username=github_username,
            role=UserRole.USER,
        )
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_user_remove(update, args: list, telegram_id: int) -> None:
    """移除用户"""
    if not args:
        await update.message.reply_text("❌ 请提供 GitHub 用户名")
        return

    github_username = args[0]
    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.remove_user(github_username)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_admin_add(update, args: list, telegram_id: int) -> None:
    """添加管理员"""
    if len(args) < 2:
        await update.message.reply_text("❌ 格式: <telegram_id> <github_username>")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Telegram ID 必须是数字")
        return

    github_username = args[1]
    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.add_user(
            telegram_id=target_id,
            github_username=github_username,
            role=UserRole.ADMIN,
        )
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_admin_remove(update, args: list, telegram_id: int) -> None:
    """移除管理员"""
    if not args:
        await update.message.reply_text("❌ 请提供 Telegram ID")
        return

    try:
        target_id = int(args[0])
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


async def _do_quota_set(update, args: list, telegram_id: int) -> None:
    """设置配额"""
    if len(args) < 3:
        await update.message.reply_text(
            "❌ 格式: <github_username> <daily|weekly|monthly> <limit>"
        )
        return

    github_username = args[0]
    quota_type = args[1]
    try:
        limit = int(args[2])
    except ValueError:
        await update.message.reply_text("❌ 配额限制必须是数字")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.set_user_quota(github_username, quota_type, limit)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


def _make_mock(update: Update, args: list):
    """构造 mock update/context 供原 handler 复用"""
    def _default_attr(self, name):
        return None

    mock_update = type("MockUpdate", (), {
        "effective_user": update.effective_user,
        "message": update.message,
        "__getattr__": _default_attr,
    })()
    mock_ctx = type("MockContext", (), {
        "args": args,
        "__getattr__": _default_attr,
    })()
    return mock_update, mock_ctx


async def _do_review(update, args: list, telegram_id: int) -> None:
    """手动审查"""
    if not args:
        await update.message.reply_text("❌ 请提供 PR URL")
        return
    from backend.telegram import handlers
    mock_update, mock_ctx = _make_mock(update, args)
    await handlers.cmd_review(mock_update, mock_ctx)


async def _do_update_docs(update, args: list, telegram_id: int) -> None:
    """更新文档索引"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return
    from backend.telegram import handlers
    mock_update, mock_ctx = _make_mock(update, args)
    await handlers.cmd_update_docs(mock_update, mock_ctx)


async def _do_code_index(update, args: list, telegram_id: int) -> None:
    """代码索引"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return
    from backend.telegram import handlers
    mock_update, mock_ctx = _make_mock(update, args)
    await handlers.cmd_code_index(mock_update, mock_ctx)


async def _do_docs_status(update, args: list) -> None:
    """文档索引状态"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return
    from backend.telegram import handlers
    mock_update, mock_ctx = _make_mock(update, args)
    await handlers.cmd_docs_status(mock_update, mock_ctx)


async def _do_code_status(update, args: list) -> None:
    """代码索引状态"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return
    from backend.telegram import handlers
    mock_update, mock_ctx = _make_mock(update, args)
    await handlers.cmd_code_status(mock_update, mock_ctx)


async def _do_repo_subscribe(update, args: list, telegram_id: int) -> None:
    """订阅仓库"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return

    repo_name = args[0].strip()
    is_valid, error_msg = validate_github_repo_name(repo_name)
    if not is_valid:
        await update.message.reply_text(f"❌ {error_msg}")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.subscribe_repo(telegram_id, repo_name)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


async def _do_repo_unsubscribe(update, args: list, telegram_id: int) -> None:
    """取消订阅仓库"""
    if not args:
        await update.message.reply_text("❌ 请提供仓库名")
        return

    repo_name = args[0].strip()
    is_valid, error_msg = validate_github_repo_name(repo_name)
    if not is_valid:
        await update.message.reply_text(f"❌ {error_msg}")
        return

    async with get_async_session() as session:
        service = TelegramService(session)
        success, message = await service.unsubscribe_repo(telegram_id, repo_name)
        await update.message.reply_text(f"✅ {message}" if success else f"❌ {message}")


# ---------------------------------------------------------------------------
# Handler 注册（供 bot.py 使用）
# ---------------------------------------------------------------------------


def get_callback_handler() -> CallbackQueryHandler:
    """获取 CallbackQuery 处理器"""
    return CallbackQueryHandler(handle_callback)


def get_force_reply_handler() -> MessageHandler:
    """获取 ForceReply 响应处理器"""
    return MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, handle_force_reply)
