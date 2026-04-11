"""GitHub Webhook API端点"""

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from typing import Dict, Any
import re
from loguru import logger

from backend.core.github_app import (
    verify_webhook_signature,
    extract_pr_info_from_webhook,
    extract_issue_info_from_webhook,
    GitHubAppClient,
)
from backend.workers.review_worker import submit_review_task
from backend.services.telegram_service import TelegramService
from backend.telegram.notifications import get_notification_sender
from backend.core.config import get_settings
settings = get_settings()


def get_async_session():
    """获取异步会话"""
    from backend.models.database import async_session, init_async_db

    if async_session is None:
        # 如果会话未初始化，尝试初始化
        try:
            init_async_db(settings.database_url)
        except Exception as e:
            logger.error(f"无法初始化数据库会话: {e}")
            raise RuntimeError("数据库未初始化")

    return async_session()


router = APIRouter()


@router.post("/github")
async def handle_github_webhook(
    request: Request,
    x_hub_signature: str = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str = Header(None, alias="X-GitHub-Event"),
) -> JSONResponse:
    """
    处理GitHub Webhook事件

    支持的事件：
    - pull_request: PR被打开、更新或重新打开
    - issue_comment: PR评论指令（如 /full-review）
    """
    try:
        # 读取原始payload
        payload = await request.body()

        # 验证签名
        if not x_hub_signature:
            logger.warning("收到没有签名的Webhook请求")
            raise HTTPException(status_code=403, detail="缺少签名")

        if not verify_webhook_signature(payload, x_hub_signature):
            logger.warning("Webhook签名验证失败")
            raise HTTPException(status_code=403, detail="签名验证失败")

        # 解析JSON
        try:
            payload_data = await request.json()
        except Exception as e:
            logger.error(f"解析Webhook payload失败: {e}")
            raise HTTPException(status_code=400, detail="无效的JSON")

        # 记录事件
        logger.info(f"收到GitHub事件: {x_github_event}")

        # 处理PR事件
        if x_github_event == "pull_request":
            return await handle_pull_request_event(payload_data)
        elif x_github_event == "issues":
            return await handle_issue_event(payload_data)
        elif x_github_event == "issue_comment":
            return await handle_issue_comment_event(payload_data)
        elif x_github_event == "installation":
            return await handle_installation_event(payload_data)
        else:
            logger.info(f"忽略事件类型: {x_github_event}")
            return JSONResponse(content={"status": "ignored", "event": x_github_event})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理Webhook时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_pull_request_event(payload: Dict[str, Any]) -> JSONResponse:
    """处理Pull Request事件"""
    try:
        # 提取PR信息
        pr_info = extract_pr_info_from_webhook(payload)
        if not pr_info:
            logger.warning("无法提取PR信息")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        action = pr_info["action"]

        # 只处理以下动作
        supported_actions = ["opened", "synchronize", "reopened"]
        if action not in supported_actions:
            logger.info(f"忽略PR动作: {action}")
            return JSONResponse(content={"status": "ignored", "action": action})

        # 检查PR状态
        if pr_info.get("merged"):
            logger.info(
                f"PR已合并，跳过审查: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return JSONResponse(
                content={"status": "skipped", "reason": "already merged"}
            )

        if pr_info.get("draft"):
            logger.info(
                f"草稿PR，跳过审查: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return JSONResponse(content={"status": "skipped", "reason": "draft PR"})

        if pr_info.get("state") != "open":
            logger.info(
                f"PR未打开，跳过审查: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return JSONResponse(content={"status": "skipped", "reason": "PR not open"})

        # Telegram 权限检查
        notification_sender = get_notification_sender()
        async with get_async_session() as session:
            service = TelegramService(session)

            # 1. 先检查用户是否已注册
            github_username = pr_info.get("author", "")
            if not github_username:
                logger.warning(
                    f"无法获取PR作者: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
                )
                return JSONResponse(
                    content={"status": "skipped", "reason": "unknown author"}
                )

            user = await service.get_user_by_github_username(github_username)
            if not user:
                logger.warning(f"未注册的用户: {github_username}")
                if notification_sender:
                    await notification_sender.send_unauthorized_user(
                        repo_name=pr_info["repo_full_name"],
                        pr_number=pr_info["pr_number"],
                        github_username=github_username,
                    )
                return JSONResponse(
                    content={"status": "skipped", "reason": "unregistered user"}
                )

            # 2. 检查并消耗配额
            allowed, reason = await service.check_and_consume_quota(
                github_username=github_username,
                repo_name=pr_info["repo_full_name"],
                pr_number=pr_info["pr_number"],
            )

            if not allowed:
                logger.warning(f"配额不足: {github_username} - {reason}")
                if notification_sender:
                    await notification_sender.send_quota_exceeded(
                        repo_name=pr_info["repo_full_name"],
                        item_type="PR",
                        item_number=pr_info["pr_number"],
                        reason=reason,
                        chat_id=user.telegram_id,
                    )
                return JSONResponse(
                    content={
                        "status": "skipped",
                        "reason": "quota exceeded",
                        "detail": reason,
                    }
                )

            # 4. 发送审查开始通知
            if notification_sender:
                # 收集通知目标：作者 + 订阅者
                start_chat_ids = []
                if user:
                    start_chat_ids.append(user.telegram_id)
                repo_subscribers = await service.get_repo_subscribers(
                    pr_info["repo_full_name"]
                )
                start_chat_ids = list(dict.fromkeys(start_chat_ids + repo_subscribers))

                if start_chat_ids:
                    await notification_sender.send_review_start(
                        repo_name=pr_info["repo_full_name"],
                        pr_number=pr_info["pr_number"],
                        pr_title=pr_info.get("title", ""),
                        author=github_username,
                        chat_ids=start_chat_ids,
                    )

        # 提交审查任务到队列
        task_id = await submit_review_task(pr_info)

        logger.info(
            f"已提交审查任务: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
            f"任务ID: {task_id}"
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "审查任务已提交",
                "pr": f"{pr_info['repo_full_name']}#{pr_info['pr_number']}",
                "action": action,
                "task_id": task_id,
            }
        )

    except Exception as e:
        logger.error(f"处理PR事件时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_issue_comment_event(payload: Dict[str, Any]) -> JSONResponse:
    """处理Issue Comment事件（PR评论指令）"""
    try:
        action = payload.get("action")

        # 只处理新建评论
        if action != "created":
            return JSONResponse(content={"status": "ignored", "action": action})

        # 提取评论内容
        comment_body = payload.get("comment", {}).get("body", "").strip()

        # 提前获取 issue 信息，供命令分发使用
        issue = payload.get("issue", {})

        # 检查是否为 /full-review 指令（精确匹配，避免误匹配 /full-review-extra 等）
        if not re.match(r"^/full-review(\s|$)", comment_body):
            # 检查 /revoke 命令
            if re.match(r"^/revoke(\s|$)", comment_body):
                return await handle_revoke_command(payload)
            # 检查 /analyze 命令（仅限 Issue）
            if re.match(r"^/analyze(\s|$)", comment_body):
                if not issue.get("pull_request"):
                    return await handle_issue_analyze_command(payload)
                return JSONResponse(
                    content={"status": "ignored", "reason": "/analyze 仅适用于 Issue"}
                )
            return JSONResponse(
                content={"status": "ignored", "reason": "not a review command"}
            )

        # 提取PR信息
        repo_info = payload.get("repository", {})
        installation = payload.get("installation")
        pr_number = issue.get("number")

        if not repo_info or not installation or not pr_number:
            logger.warning("Issue comment payload中缺少必要字段")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        repo_owner = repo_info.get("owner", {}).get("login")
        repo_name = repo_info.get("name")
        repo_full_name = repo_info.get("full_name")
        installation_id = installation.get("id") if installation else None

        if not all([repo_owner, repo_name, repo_full_name, installation_id]):
            logger.warning("Issue comment payload中缺少必要字段")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        # 获取评论者信息
        commenter_login = payload.get("comment", {}).get("user", {}).get("login", "")
        pr_author_login = issue.get("user", {}).get("login", "")

        if not commenter_login:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法获取评论者信息"},
            )

        logger.info(
            f"收到 /full-review 指令: {repo_full_name}#{pr_number}, "
            f"评论者: {commenter_login}"
        )

        # 权限检查：PR作者 或 仓库管理员/协作者
        github_app = GitHubAppClient()

        is_pr_author = commenter_login == pr_author_login
        is_collaborator = False

        if not is_pr_author:
            permission = github_app.check_collaborator_permission(
                repo_owner, repo_name, commenter_login
            )
            is_collaborator = permission in ("admin", "write")

        if not is_pr_author and not is_collaborator:
            logger.info(
                f"用户 {commenter_login} 无权触发重新审查 (非PR作者且非仓库协作者)"
            )
            # 回复评论提示无权限
            try:
                client = github_app.get_repo_client(repo_owner, repo_name)
                if client:
                    repo = client.get_repo(repo_full_name)
                    pr = repo.get_pull(pr_number)
                    pr.create_issue_comment(
                        f"❌ @{commenter_login}，只有 PR 作者或仓库管理员/协作者才能触发重新审查。"
                    )
            except Exception as e:
                logger.warning(f"回复无权限提示失败: {e}")
            return JSONResponse(
                content={"status": "denied", "reason": "insufficient permission"}
            )

        # 通过 GitHub API 获取完整 PR 信息
        try:
            client = github_app.get_repo_client(repo_owner, repo_name)
            if not client:
                return JSONResponse(
                    status_code=403,
                    content={"status": "error", "message": "无法获取仓库访问权限"},
                )

            repo = client.get_repo(repo_full_name)
            pr = repo.get_pull(pr_number)

            # 检查PR状态
            if pr.state != "open":
                return JSONResponse(
                    content={"status": "skipped", "reason": "PR not open"}
                )
            if pr.draft:
                return JSONResponse(content={"status": "skipped", "reason": "draft PR"})
            if pr.merged:
                return JSONResponse(
                    content={"status": "skipped", "reason": "already merged"}
                )

            # 构造 pr_info 字典
            pr_info = {
                "action": "full_review",
                "pr_id": pr.id,
                "pr_number": pr.number,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "repo_full_name": repo_full_name,
                "installation_id": installation_id,
                "author": pr.user.login,
                "title": pr.title,
                "branch": pr.head.ref,
                "base_branch": pr.base.ref,
                "diff_url": pr.diff_url,
                "patch_url": pr.patch_url,
                "html_url": pr.html_url,
                "state": pr.state,
                "draft": pr.draft,
                "merged": pr.merged,
            }
        except Exception as e:
            logger.error(f"获取PR信息失败: {e}", exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"status": "error", "message": "获取PR信息失败"},
            )

        # 获取 bot 用户名
        bot_username = github_app.get_bot_username(repo_owner, repo_name)

        # 清理 GitHub 上的旧评论和 Review
        deleted_result = {"issue_comments": 0, "review_comments": 0}
        dismissed_reviews = 0

        if bot_username:
            deleted_result = github_app.delete_all_bot_comments(
                repo_owner, repo_name, pr_number, bot_username
            )
            dismissed_reviews = github_app.dismiss_bot_reviews(
                repo_owner, repo_name, pr_number, bot_username
            )

        logger.info(
            f"清理完成: Issue评论={deleted_result['issue_comments']}, "
            f"Review评论={deleted_result['review_comments']}, 撤回Review={dismissed_reviews}"
        )

        # 清理数据库中的旧审查记录
        try:
            async with get_async_session() as session:
                from backend.models.database import PRReview
                from sqlalchemy import select, and_

                result = await session.execute(
                    select(PRReview).where(
                        and_(
                            PRReview.repo_name == repo_name,
                            PRReview.pr_id == pr_number,
                        )
                    )
                )
                old_reviews = result.scalars().all()

                if old_reviews:
                    for old_review in old_reviews:
                        await session.delete(old_review)
                    await session.commit()
                    logger.info(
                        f"已删除 {len(old_reviews)} 条旧审查记录: "
                        f"{repo_full_name}#{pr_number}"
                    )
        except Exception as e:
            logger.warning(f"删除旧审查记录失败（将继续审查）: {e}")

        # 发送审查开始通知
        notification_sender = get_notification_sender()
        if notification_sender:
            # 收集通知目标：作者 + 订阅者
            manual_chat_ids = []
            try:
                async with get_async_session() as session:
                    svc = TelegramService(session)
                    author_name = pr_info.get("author", "")
                    if author_name:
                        author_user = await svc.get_user_by_github_username(author_name)
                        if author_user:
                            manual_chat_ids.append(author_user.telegram_id)
                    subscribers = await svc.get_repo_subscribers(repo_full_name)
                    manual_chat_ids = list(dict.fromkeys(manual_chat_ids + subscribers))
            except Exception as e:
                logger.warning(f"获取通知目标失败: {e}", exc_info=True)

            if manual_chat_ids:
                await notification_sender.send_review_start(
                    repo_name=repo_full_name,
                    pr_number=pr_number,
                    pr_title=pr_info.get("title", ""),
                    author=pr_info["author"],
                    chat_ids=manual_chat_ids,
                )

        # 提交全量审查任务
        task_id = await submit_review_task(pr_info)

        # 回复确认评论
        try:
            cleanup_info = []
            if deleted_result["review_comments"] > 0:
                cleanup_info.append(
                    f"删除 {deleted_result['review_comments']} 条行内评论"
                )
            if deleted_result["issue_comments"] > 0:
                cleanup_info.append(f"删除 {deleted_result['issue_comments']} 条评论")
            if dismissed_reviews > 0:
                cleanup_info.append(f"撤回 {dismissed_reviews} 条旧Review")
            cleanup_text = "、".join(cleanup_info) if cleanup_info else "无需清理"

            pr.create_issue_comment(
                f"已{cleanup_text}，正在重新全量审查...\n\n由 @{commenter_login} 触发"
            )
        except Exception as e:
            logger.warning(f"发送确认评论失败: {e}")

        logger.info(
            f"/full-review 已触发: {repo_full_name}#{pr_number}, "
            f"task_id={task_id}, triggered_by={commenter_login}"
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "全量审查任务已提交",
                "pr": f"{repo_full_name}#{pr_number}",
                "deleted_comments": deleted_result,
                "dismissed_reviews": dismissed_reviews,
                "task_id": task_id,
            }
        )

    except Exception as e:
        logger.error(f"处理Issue Comment事件时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_revoke_command(payload: Dict[str, Any]) -> JSONResponse:
    """处理 /revoke 命令（一键撤回 AI 评论和 Review）"""
    try:
        # 提取 PR 信息
        issue = payload.get("issue", {})

        # 必须是 PR 评论
        if not issue.get("pull_request"):
            return JSONResponse(
                content={"status": "ignored", "reason": "not a PR comment"}
            )

        repo_info = payload.get("repository", {})
        installation = payload.get("installation")
        pr_number = issue.get("number")

        if not repo_info or not installation or not pr_number:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        repo_owner = repo_info.get("owner", {}).get("login")
        repo_name = repo_info.get("name")
        repo_full_name = repo_info.get("full_name")
        installation_id = installation.get("id") if installation else None

        if not all([repo_owner, repo_name, repo_full_name, installation_id]):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        # 获取评论者信息
        commenter_login = payload.get("comment", {}).get("user", {}).get("login", "")

        if not commenter_login:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法获取评论者信息"},
            )

        logger.info(
            f"收到 /revoke 指令: {repo_full_name}#{pr_number}, "
            f"评论者: {commenter_login}"
        )

        # 权限检查：仅限仓库 admin
        github_app = GitHubAppClient()

        permission = github_app.check_collaborator_permission(
            repo_owner, repo_name, commenter_login
        )

        if permission != "admin":
            logger.info(
                f"用户 {commenter_login} 无权撤回评论 (权限: {permission}, 需要 admin)"
            )
            try:
                client = github_app.get_repo_client(repo_owner, repo_name)
                if client:
                    repo = client.get_repo(repo_full_name)
                    pr = repo.get_pull(pr_number)
                    pr.create_issue_comment(
                        f"❌ @{commenter_login}，只有仓库管理员才能撤回 AI 评论。"
                    )
            except Exception as e:
                logger.warning(f"回复无权限提示失败: {e}")
            return JSONResponse(
                content={"status": "denied", "reason": "insufficient permission"}
            )

        # 删除 bot 评论和撤回 Review
        bot_username = github_app.get_bot_username(repo_owner, repo_name)

        deleted_result = {"issue_comments": 0, "review_comments": 0}
        dismissed_reviews = 0

        if bot_username:
            deleted_result = github_app.delete_all_bot_comments(
                repo_owner, repo_name, pr_number, bot_username
            )
            dismissed_reviews = github_app.dismiss_bot_reviews(
                repo_owner, repo_name, pr_number, bot_username
            )

        logger.info(
            f"撤回完成: Issue评论={deleted_result['issue_comments']}, "
            f"Review评论={deleted_result['review_comments']}, 撤回Review={dismissed_reviews}"
        )

        # 回复确认评论
        try:
            client = github_app.get_repo_client(repo_owner, repo_name)
            if client:
                repo = client.get_repo(repo_full_name)
                pr = repo.get_pull(pr_number)

                cleanup_info = []
                if deleted_result["review_comments"] > 0:
                    cleanup_info.append(
                        f"删除 {deleted_result['review_comments']} 条行内评论"
                    )
                if deleted_result["issue_comments"] > 0:
                    cleanup_info.append(
                        f"删除 {deleted_result['issue_comments']} 条评论"
                    )
                if dismissed_reviews > 0:
                    cleanup_info.append(f"撤回 {dismissed_reviews} 条 Review")
                cleanup_text = (
                    "、".join(cleanup_info) if cleanup_info else "没有需要清理的内容"
                )

                pr.create_issue_comment(
                    f"✅ 已{cleanup_text}。\n\n由 @{commenter_login} 触发"
                )
        except Exception as e:
            logger.warning(f"发送确认评论失败: {e}")

        logger.info(
            f"/revoke 已执行: {repo_full_name}#{pr_number}, "
            f"triggered_by={commenter_login}"
        )

        return JSONResponse(
            content={
                "status": "success",
                "message": "AI 评论已撤回",
                "pr": f"{repo_full_name}#{pr_number}",
                "deleted_comments": deleted_result,
                "dismissed_reviews": dismissed_reviews,
            }
        )

    except Exception as e:
        logger.error(f"处理 /revoke 命令时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_issue_event(payload: Dict[str, Any]) -> JSONResponse:
    """处理 Issue 事件"""
    try:
        issue_info = extract_issue_info_from_webhook(payload)
        if not issue_info:
            logger.warning("无法提取 Issue 信息")
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取 Issue 信息"},
            )

        action = issue_info["action"]

        # 只处理以下动作
        supported_actions = ["opened", "edited", "reopened", "closed"]
        if action not in supported_actions:
            logger.info(f"忽略 Issue 动作: {action}")
            return JSONResponse(content={"status": "ignored", "action": action})

        # 过滤 Bot 自身事件
        bot_username = settings.bot_username
        if bot_username and issue_info.get("author") == bot_username:
            logger.info("跳过 Bot 自身创建的 Issue 事件")
            return JSONResponse(
                content={"status": "ignored", "reason": "bot self-event"}
            )

        # 过滤 Bot 触发的 edited 事件（如自动改写标题）
        if action == "edited" and bot_username:
            sender = payload.get("sender", {}).get("login", "")
            if sender == bot_username:
                logger.info("跳过 Bot 触发的 Issue edited 事件")
                return JSONResponse(
                    content={"status": "ignored", "reason": "bot edited event"}
                )

        # 语义关联 Issue 向量同步（独立于 issue 分析，仓库级别）
        if (
            hasattr(settings, "enable_semantic_issue_linking")
            and settings.enable_semantic_issue_linking
        ):
            try:
                # 过滤 Pull Request（PR 也触发 issues 事件）
                issue_payload = payload.get("issue", {})
                if not issue_payload.get("pull_request"):
                    from backend.services.issue_embedding_service import (
                        IssueEmbeddingService,
                    )

                    emb_service = IssueEmbeddingService()
                    repo_owner = issue_info["repo_owner"]
                    repo_name = issue_info["repo_name"]
                    issue_number = issue_info["issue_number"]

                    # embedding 改为在 issue_worker 中 AI 分析完成后使用摘要执行
                    if action == "closed":
                        # 标记为 closed 而非删除，保留在向量库中供查重
                        await emb_service.close_issue(
                            repo_owner, repo_name, issue_number
                        )
                    elif action == "reopened":
                        # reopened 时及时更新 state，issue_worker 的 AI 分析可能延迟
                        issue_title = issue_payload.get("title", "")
                        issue_body = issue_payload.get("body", "") or ""
                        await emb_service.upsert_issue(
                            repo_owner,
                            repo_name,
                            issue_number,
                            title=issue_title,
                            body=issue_body,
                            state="open",
                        )
                else:
                    logger.debug("跳过 Pull Request 的 Issue 向量同步")
            except Exception as e:
                logger.warning(f"语义 Issue 向量同步失败: {e}")

        # closed 事件仅用于向量同步，不需要触发 Issue 分析
        if action == "closed":
            return JSONResponse(
                content={"status": "accepted", "action": "closed", "sync": "vector_only"}
            )

        # 检查功能是否启用
        if not settings.enable_issue_analysis:
            logger.info("Issue 分析功能未启用")
            return JSONResponse(
                content={"status": "skipped", "reason": "feature disabled"}
            )

        # Telegram 权限检查
        notification_sender = get_notification_sender()
        async with get_async_session() as session:
            service = TelegramService(session)

            github_username = issue_info.get("author", "")
            if not github_username:
                logger.warning("无法获取 Issue 作者")
                return JSONResponse(
                    content={"status": "skipped", "reason": "unknown author"}
                )

            user = await service.get_user_by_github_username(github_username)
            if not user:
                logger.info(f"Issue 作者未注册: {github_username}，跳过分析")
                return JSONResponse(
                    content={"status": "skipped", "reason": "unregistered user"}
                )

            # Issue 配额检查
            allowed, reason = await service.check_and_consume_issue_quota(
                github_username=github_username,
                repo_name=issue_info["repo_full_name"],
                issue_number=issue_info["issue_number"],
            )
            if not allowed:
                logger.warning(f"Issue 配额不足: {github_username} - {reason}")
                if notification_sender:
                    await notification_sender.send_quota_exceeded(
                        repo_name=issue_info["repo_full_name"],
                        item_type="Issue",
                        item_number=issue_info["issue_number"],
                        reason=reason,
                        chat_id=user.telegram_id,
                    )
                return JSONResponse(
                    content={
                        "status": "skipped",
                        "reason": "quota exceeded",
                        "detail": reason,
                    }
                )

        # 提交分析任务
        from backend.workers.issue_worker import submit_issue_analysis_task

        task_id = await submit_issue_analysis_task(issue_info)

        logger.info(
            f"已提交 Issue 分析任务: {issue_info['repo_full_name']}#{issue_info['issue_number']}, "
            f"任务ID: {task_id}"
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "Issue 分析任务已提交",
                "issue": f"{issue_info['repo_full_name']}#{issue_info['issue_number']}",
                "action": action,
                "task_id": task_id,
            }
        )

    except Exception as e:
        logger.error(f"处理 Issue 事件时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_issue_analyze_command(payload: Dict[str, Any]) -> JSONResponse:
    """处理 /analyze 命令（手动触发 Issue 分析）"""
    try:
        issue_info = extract_issue_info_from_webhook(payload)
        if not issue_info:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取 Issue 信息"},
            )

        # 过滤 Bot 自身评论
        bot_username = settings.bot_username
        commenter = payload.get("comment", {}).get("user", {}).get("login", "")
        if bot_username and commenter == bot_username:
            return JSONResponse(
                content={"status": "ignored", "reason": "bot self-comment"}
            )

        # 检查功能是否启用
        if not settings.enable_issue_analysis:
            return JSONResponse(
                content={"status": "skipped", "reason": "feature disabled"}
            )

        # 权限和配额检查
        notification_sender = get_notification_sender()
        async with get_async_session() as session:
            service = TelegramService(session)
            user = await service.get_user_by_github_username(commenter)
            if not user:
                return JSONResponse(
                    content={"status": "skipped", "reason": "unregistered user"}
                )

            allowed, reason = await service.check_and_consume_issue_quota(
                github_username=commenter,
                repo_name=issue_info["repo_full_name"],
                issue_number=issue_info["issue_number"],
            )
            if not allowed:
                logger.warning(f"Issue 配额不足: {commenter} - {reason}")
                if notification_sender:
                    await notification_sender.send_quota_exceeded(
                        repo_name=issue_info["repo_full_name"],
                        item_type="Issue",
                        item_number=issue_info["issue_number"],
                        reason=reason,
                        chat_id=user.telegram_id,
                    )
                return JSONResponse(
                    content={
                        "status": "skipped",
                        "reason": "quota exceeded",
                        "detail": reason,
                    }
                )

        # 提交分析任务
        from backend.workers.issue_worker import submit_issue_analysis_task

        task_id = await submit_issue_analysis_task(issue_info)

        logger.info(
            f"/analyze 命令触发: {issue_info['repo_full_name']}#{issue_info['issue_number']}, "
            f"triggered_by={commenter}"
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "Issue 分析任务已提交",
                "task_id": task_id,
            }
        )

    except Exception as e:
        logger.error(f"处理 /analyze 命令时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_installation_event(payload: Dict[str, Any]) -> JSONResponse:
    """处理 GitHub App installation 事件，清除安装状态缓存"""
    try:
        action = payload.get("action", "")
        installation = payload.get("installation", {})
        account = installation.get("account", {})
        account_login = account.get("login", "")

        if not account_login:
            logger.warning("installation 事件缺少 account.login")
            return JSONResponse(
                status_code=200,
                content={"status": "processed", "action": action},
            )

        logger.info(
            f"GitHub App installation 事件: {action}, account={account_login}"
        )

        # 清除该用户的安装状态 Redis 缓存
        try:
            from backend.core.redis import get_async_redis

            r = await get_async_redis()
            cache_key = f"github_app_installed:{account_login}"
            deleted = await r.delete(cache_key)
            if deleted:
                logger.info(f"已清除 {account_login} 的安装状态缓存")
        except Exception as e:
            logger.warning(f"清除安装状态缓存失败: {e}")

        return JSONResponse(
            status_code=200,
            content={"status": "processed", "action": action},
        )
    except Exception as e:
        logger.error(f"处理 installation 事件出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


@router.get("/health")
async def health_check() -> JSONResponse:
    """健康检查端点"""
    return JSONResponse(content={"status": "healthy", "service": "Sakura AI Reviewer"})


