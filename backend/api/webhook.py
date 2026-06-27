"""GitHub Webhook API端点"""

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse, PlainTextResponse
from typing import Dict, Any, Optional
import asyncio
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
from backend.core.config import get_settings, get_dynamic_config, get_user_dynamic_config

settings = get_settings()

SCAN_SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "suggestion": 3}


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


async def _mark_agent_task_external_reviewing(pr_info: dict[str, Any]) -> None:
    """Mark Agent Team PR task as waiting for external review."""
    branch = pr_info.get("branch") or ""
    if not branch.startswith("sakura-agent/"):
        return

    from sqlalchemy import select

    from backend.models.agent_team_models import AgentTeamTask, AgentTeamTaskStatus

    async with get_async_session() as session:
        result = await session.execute(
            select(AgentTeamTask).where(
                AgentTeamTask.repo_owner == pr_info["repo_owner"],
                AgentTeamTask.repo_name == pr_info["repo_name"],
                AgentTeamTask.pr_number == pr_info["pr_number"],
                AgentTeamTask.branch_name == branch,
            )
        )
        task = result.scalar_one_or_none()
        if not task:
            return
        if task.status not in {
            AgentTeamTaskStatus.PR_OPENED.value,
            AgentTeamTaskStatus.EXTERNAL_REVIEWING.value,
        }:
            return

        task.status = AgentTeamTaskStatus.EXTERNAL_REVIEWING.value
        task.current_phase = "external_reviewing"
        if pr_info.get("head_sha"):
            task.pr_head_sha = pr_info["head_sha"]
        await session.commit()


router = APIRouter()


@router.post("/github")
async def handle_github_webhook(
    request: Request,
    x_hub_signature: str = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str = Header(None, alias="X-GitHub-Event"),
    x_github_delivery: str = Header(None, alias="X-GitHub-Delivery"),
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
            return await handle_pull_request_event(
                payload_data,
                delivery_id=x_github_delivery,
            )
        elif x_github_event == "issues":
            return await handle_issue_event(payload_data)
        elif x_github_event == "issue_comment":
            return await handle_issue_comment_event(payload_data)
        elif x_github_event == "pull_request_review":
            return await handle_pull_request_review_event(payload_data)
        elif x_github_event == "check_run":
            return await handle_check_run_event(payload_data)
        elif x_github_event == "workflow_job":
            return await handle_workflow_job_event(payload_data)
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


async def resolve_pr_number_for_ci(
    repo_owner: str,
    repo_name: str,
    repo_full_name: str,
    head_sha: str,
    payload_prs: list,
) -> Optional[int]:
    """CI 失败事件三层降级解析 pr_number / Resolve pr_number (three-tier fallback).

    ① payload.pull_requests 字段（Fork 场景为空）
    ② head_sha_pr_map 映射表（由 pull_request 事件维护）
    ③ GET /commits/{sha}/pulls API 兜底
    三层都失败返回 None（调用方忽略该 CI 事件）。
    """
    # ① payload 自带字段
    for pr in payload_prs or []:
        number = pr.get("number")
        if number is not None:
            return int(number)

    # ② 映射表兜底
    from backend.services.ci_failure_service import CIFailureService

    pr_number = await CIFailureService().lookup_pr_number(repo_full_name, head_sha)
    if pr_number:
        return pr_number

    # ③ API 兜底
    github_app = GitHubAppClient()
    return await asyncio.to_thread(
        github_app.get_pr_number_for_commit, repo_owner, repo_name, head_sha
    )


async def handle_check_run_event(payload: Dict[str, Any]) -> JSONResponse:
    """处理 check_run.completed 事件：采集外部 CI（Checks API 类）失败详情。

    过滤自身 Sakura Check Run；非 completed 动作忽略；解不出 pr_number 忽略。
    所有异常吞掉，不影响其他 webhook 事件。
    """
    try:
        action = payload.get("action")
        if action != "completed":
            logger.info(f"忽略 check_run 动作: {action}")
            return JSONResponse(content={"status": "ignored", "action": action})

        check_run = payload.get("check_run") or {}
        name = check_run.get("name", "")
        conclusion = check_run.get("conclusion", "")
        head_sha = check_run.get("head_sha", "") or ""
        repo_info = payload.get("repository") or {}
        repo_owner = repo_info.get("owner", {}).get("login", "")
        repo_name = repo_info.get("name", "")
        repo_full_name = repo_info.get("full_name", "")
        logger.info(
            "[check_run] completed 收到: name={!r}, conclusion={!r}, head_sha={!r}".format(
                name, conclusion, head_sha[:8]
            )
        )

        # 过滤自身 Check Run（避免把 Sakura 审查状态当外部失败）
        if name == "Sakura AI Review":
            logger.info("[check_run] 跳过自身 Check Run")
            return JSONResponse(
                content={"status": "ignored", "reason": "self check run"}
            )

        if conclusion not in {"failure", "timed_out", "cancelled", "action_required"}:
            logger.info(
                "[check_run] 跳过非失败 conclusion: name={!r}, conclusion={!r}".format(
                    name, conclusion
                )
            )
            # 重跑成功：清除同 (repo, head_sha, name) 的旧失败记录，避免注入过时失败
            if repo_full_name and head_sha:
                from backend.services.ci_failure_service import CIFailureService

                await CIFailureService().delete_failures(
                    repo_full_name, head_sha, "check_run", name
                )
            return JSONResponse(
                content={"status": "ignored", "reason": "non-failure conclusion"}
            )

        if not all([repo_owner, repo_name, repo_full_name, head_sha]):
            logger.warning("check_run payload 缺少必要字段")
            return JSONResponse(
                content={"status": "ignored", "reason": "missing fields"}
            )

        pr_number = await resolve_pr_number_for_ci(
            repo_owner,
            repo_name,
            repo_full_name,
            head_sha,
            check_run.get("pull_requests") or [],
        )
        if not pr_number:
            logger.info(
                f"check_run 事件无法关联 PR（head_sha={head_sha}），忽略"
            )
            return JSONResponse(
                content={"status": "ignored", "reason": "no associated PR"}
            )

        from backend.services.ci_failure_service import CIFailureService

        await CIFailureService().record_check_run_failure(
            repo_owner,
            repo_name,
            repo_full_name,
            pr_number,
            head_sha,
            check_run,
        )
        logger.info(
            "[check_run] 已采集失败: name={!r}, pr_number={}".format(name, pr_number)
        )
        return JSONResponse(
            content={
                "status": "accepted",
                "pr_number": pr_number,
                "check_run": name,
            }
        )
    except Exception as e:
        logger.error(f"处理 check_run 事件失败: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_workflow_job_event(payload: Dict[str, Any]) -> JSONResponse:
    """处理 workflow_job.completed 事件：采集 GitHub Actions Job 失败详情。

    workflow_job payload 无 pull_requests 字段，pr_number 走映射表/API 兜底。
    所有异常吞掉，不影响其他 webhook 事件。
    """
    try:
        action = payload.get("action")
        if action != "completed":
            logger.info(f"忽略 workflow_job 动作: {action}")
            return JSONResponse(content={"status": "ignored", "action": action})

        workflow_job = payload.get("workflow_job") or {}
        wf_name = workflow_job.get("name", "")
        conclusion = workflow_job.get("conclusion", "")
        head_sha = workflow_job.get("head_sha", "") or ""
        repo_info = payload.get("repository") or {}
        repo_full_name = repo_info.get("full_name", "")
        logger.info(
            "[workflow_job] completed 收到: name={!r}, conclusion={!r}, head_sha={!r}".format(
                wf_name, conclusion, head_sha[:8]
            )
        )
        if conclusion not in {"failure", "timed_out", "cancelled", "action_required"}:
            logger.info(
                "[workflow_job] 跳过非失败 conclusion: name={!r}, conclusion={!r}".format(
                    wf_name, conclusion
                )
            )
            # 重跑成功：清除同 (repo, head_sha, name) 的旧失败记录
            if repo_full_name and head_sha:
                from backend.services.ci_failure_service import CIFailureService

                await CIFailureService().delete_failures(
                    repo_full_name, head_sha, "workflow_job", wf_name
                )
            return JSONResponse(
                content={"status": "ignored", "reason": "non-failure conclusion"}
            )

        repo_owner = repo_info.get("owner", {}).get("login", "")
        repo_name = repo_info.get("name", "")

        if not all([repo_owner, repo_name, repo_full_name, head_sha]):
            logger.warning("workflow_job payload 缺少必要字段")
            return JSONResponse(
                content={"status": "ignored", "reason": "missing fields"}
            )

        pr_number = await resolve_pr_number_for_ci(
            repo_owner, repo_name, repo_full_name, head_sha, []
        )
        if not pr_number:
            logger.info(
                f"workflow_job 事件无法关联 PR（head_sha={head_sha}），忽略"
            )
            return JSONResponse(
                content={"status": "ignored", "reason": "no associated PR"}
            )

        from backend.services.ci_failure_service import CIFailureService

        await CIFailureService().record_workflow_job_failure(
            repo_owner,
            repo_name,
            repo_full_name,
            pr_number,
            head_sha,
            workflow_job,
        )
        logger.info(
            "[workflow_job] 已采集失败: name={!r}, pr_number={}".format(
                wf_name, pr_number
            )
        )
        return JSONResponse(
            content={
                "status": "accepted",
                "pr_number": pr_number,
                "workflow_job": wf_name,
            }
        )
    except Exception as e:
        logger.error(f"处理 workflow_job 事件失败: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_pull_request_event(
    payload: Dict[str, Any],
    delivery_id: str | None = None,
) -> JSONResponse:
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

        # Handle PR closed/merged event: cancel any active review task
        if action == "closed":
            # Lazy import to avoid webhook ↔ worker circular dependency
            from backend.workers.review_worker import ReviewWorker, get_worker

            task_key = ReviewWorker._make_task_key(pr_info)
            try:
                worker = get_worker()
                cancelled = worker.cancel_task(task_key)
                if cancelled:
                    logger.info(f"[webhook] PR closed event: 已取消审查任务 {task_key}")
                else:
                    logger.debug(
                        f"[webhook] PR closed event: 无活跃审查任务 {task_key}"
                    )
            except Exception as e:
                logger.warning(f"[webhook] 取消审查任务失败: {e}")

            # 清理该 PR 的 pending 增量队列，避免永久残留 / PR 重开后污染新审查上下文
            try:
                from backend.services.pr_review_incremental_queue import (
                    PRReviewIncrementalQueueService,
                )

                cancelled_queue = (
                    await PRReviewIncrementalQueueService().cancel_pending_for_pr(
                        pr_info["repo_full_name"],
                        int(pr_info["pr_number"]),
                    )
                )
                if cancelled_queue:
                    logger.info(
                        "[webhook] PR closed event：已取消 {} 条 pending 增量 "
                        "{}#{}".format(
                            cancelled_queue,
                            pr_info["repo_full_name"],
                            pr_info["pr_number"],
                        )
                    )
            except Exception as e:
                logger.warning(f"[webhook] 清理增量队列失败: {e}")

            # 清理该 PR 的外部 CI 失败记录，并顺带清理过期记录。
            # 失败不影响 PR closed 事件处理。
            try:
                from backend.services.ci_failure_service import CIFailureService

                ci_service = CIFailureService()
                cleaned = await ci_service.cleanup_for_pr(
                    pr_info["repo_full_name"], int(pr_info["pr_number"])
                )
                expired = await ci_service.cleanup_expired()
                if cleaned or expired:
                    logger.info(
                        "[webhook] PR closed event：已清理外部 CI 失败记录 "
                        "pr_records={} expired_records={} {}#{}".format(
                            cleaned,
                            expired,
                            pr_info["repo_full_name"],
                            pr_info["pr_number"],
                        )
                    )
            except Exception as e:
                logger.warning(f"[webhook] 清理外部 CI 失败记录失败: {e}")

            return JSONResponse(
                content={"status": "accepted", "action": "cancelled", "task": task_key}
            )

        # 只处理以下动作
        supported_actions = ["opened", "synchronize", "reopened", "ready_for_review"]
        if action not in supported_actions:
            logger.info(f"忽略PR动作: {action}")
            return JSONResponse(content={"status": "ignored", "action": action})

        # 维护 head_sha → pr_number 映射，供 CI webhook 三层降级兜底
        # （check_run.pull_requests 在 Fork 场景为空，需映射表兜底）
        try:
            from backend.services.ci_failure_service import CIFailureService

            head_sha_for_map = pr_info.get("head_sha") or pr_info.get("after")
            if head_sha_for_map:
                await CIFailureService().upsert_head_sha_pr_map(
                    pr_info["repo_owner"],
                    pr_info["repo_name"],
                    pr_info["repo_full_name"],
                    head_sha_for_map,
                    int(pr_info["pr_number"]),
                )
        except Exception as e:
            logger.warning(
                f"[webhook] 维护 head_sha 映射失败（不影响审查）: {e}"
            )

        if not get_settings().enable_auto_review:
            logger.info(
                f"自动审查已关闭，跳过PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return JSONResponse(
                content={"status": "skipped", "reason": "auto review disabled"}
            )


        # 过滤 Bot 自身创建的 PR（如 sakura-memory 系统创建的 PR）
        # 但允许 Agent Team 创建的 PR 进入审查
        bot_username = settings.bot_username
        sender = pr_info.get("sender", "")
        author = pr_info.get("author", "")
        branch = pr_info.get("branch", "")
        is_agent_team_pr = branch.startswith("sakura-agent/")

        if bot_username and (sender == bot_username or author == bot_username):
            if is_agent_team_pr:
                logger.info(
                    f"Agent Team PR，允许审查: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
                )
            else:
                logger.info(
                    f"跳过 Bot 自身创建的 PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
                )
                return JSONResponse(
                    content={"status": "ignored", "reason": "bot self-created PR"}
                )

        # 过滤 sakura-memory 分支 PR（兜底过滤）
        if pr_info.get("branch", "").startswith("sakura-memory/"):
            logger.info(
                f"跳过 sakura-memory 分支 PR: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
            )
            return JSONResponse(
                content={"status": "ignored", "reason": "sakura-memory branch PR"}
            )

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

        # Synchronize 去重：在配额扣费前按 delivery_id 去重，避免 GitHub 重试投递
        # 导致同一增量被重复扣费（enqueue_from_webhook 内部也会去重，但那发生在
        # check_and_consume_quota 之后）。/ Dedup synchronize by delivery_id BEFORE
        # charging quota so GitHub retries don't double-bill.
        if action == "synchronize" and delivery_id:
            try:
                from backend.services.pr_review_incremental_queue import (
                    PRReviewIncrementalQueueService,
                )

                duplicate = await PRReviewIncrementalQueueService().find_by_delivery_id(
                    delivery_id
                )
                if duplicate is not None:
                    logger.info(
                        "[webhook] synchronize 重复投递 delivery_id={} 已入队"
                        "（status={}），跳过且不重复扣费: {}#{}",
                        delivery_id,
                        duplicate.status,
                        pr_info["repo_full_name"],
                        pr_info["pr_number"],
                    )
                    return JSONResponse(
                        content={
                            "status": "deduplicated",
                            "action": "synchronize",
                            "pr": f"{pr_info['repo_full_name']}#{pr_info['pr_number']}",
                            "head_sha": pr_info.get("head_sha")
                            or pr_info.get("after"),
                        }
                    )
            except Exception as e:
                logger.warning(
                    "[webhook] synchronize delivery_id 去重检查失败（继续正常流程）: {}",
                    e,
                )

        # Telegram 权限检查
        notification_sender = get_notification_sender()
        async with get_async_session() as session:
            service = TelegramService(session)

            # 1. 先检查用户是否已注册
            github_username = pr_info.get("repo_owner", "")
            if not github_username:
                logger.warning(
                    f"无法获取仓库所有者: {pr_info['repo_full_name']}#{pr_info['pr_number']}"
                )
                return JSONResponse(
                    content={"status": "skipped", "reason": "unknown repo owner"}
                )

            user = await service.get_user_by_github_username(github_username)
            if not user:
                logger.warning(f"仓库所有者未注册: {github_username}")
                if notification_sender:
                    await notification_sender.send_unauthorized_user(
                        repo_name=pr_info["repo_full_name"],
                        pr_number=pr_info["pr_number"],
                        github_username=github_username,
                    )
                return JSONResponse(
                    content={"status": "skipped", "reason": "unregistered repo owner"}
                )
            pr_info["user_id"] = user.id

            # 2. 检查并消耗配额
            allowed, reason = await service.check_and_consume_quota(
                github_username=github_username,
                repo_name=pr_info["repo_full_name"],
                pr_number=pr_info["pr_number"],
            )

            if not allowed:
                logger.warning(f"配额不足: {github_username} (仓库所有者) - {reason}")
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

        # Synchronize event: immediately dismiss stale bot reviews
        # to prevent old APPROVE from being exploited while the review
        # task is waiting in the queue (security: close the vulnerability window)
        if action == "synchronize":
            try:
                github_app = GitHubAppClient()
                bot_name = github_app.get_bot_username(
                    pr_info["repo_owner"], pr_info["repo_name"]
                )
                if bot_name:
                    dismissed = await asyncio.to_thread(
                        github_app.dismiss_bot_reviews,
                        pr_info["repo_owner"],
                        pr_info["repo_name"],
                        pr_info["pr_number"],
                        bot_name,
                    )
                    if dismissed > 0:
                        logger.info(
                            f"[webhook] synchronize 事件：已立即撤回 {dismissed} 条旧 Review "
                            f"({pr_info['repo_full_name']}#{pr_info['pr_number']})"
                        )
                    else:
                        logger.debug(
                            f"[webhook] synchronize 事件：无旧 Review 需撤回 "
                            f"({pr_info['repo_full_name']}#{pr_info['pr_number']})"
                        )
            except Exception as e:
                logger.warning(
                    f"[webhook] synchronize 事件 dismiss 旧 Review 失败（不影响后续审查）: {e}"
                )

            from backend.services.pr_review_incremental_queue import (
                PRReviewIncrementalQueueService,
            )

            try:
                queued = await PRReviewIncrementalQueueService().enqueue_from_webhook(
                    pr_info,
                    delivery_id=delivery_id,
                )
            except Exception as e:
                logger.warning(
                    "[webhook] synchronize 增量入队失败（将走完整审查）: {}", e
                )
                queued = None
            if queued:
                # 立即在新 head 上创建 check run（queued），让 PR Checks 面板在
                # 当前审查消费增量前就能看到新 commit 的 check（否则消费前新
                # commit 无 check 显示）。当前审查消费增量时会迁移到该 head。
                try:
                    from backend.services.check_run_service import CheckRunService

                    inc_head = pr_info.get("head_sha") or pr_info.get("after")
                    if inc_head:
                        inc_lang = await get_user_dynamic_config(
                            "output_language", pr_info.get("user_id")
                        )
                        await CheckRunService().report_queued(
                            pr_info["repo_owner"],
                            pr_info["repo_name"],
                            inc_head,
                            pr_number=pr_info["pr_number"],
                            output_language=inc_lang,
                        )
                except Exception as e:
                    logger.warning(
                        "[webhook] 增量入队创建 Check Run 失败（不影响审查）: {}", e
                    )
                logger.info(
                    "[webhook] synchronize 增量已入队 {}#{} head={}",
                    pr_info["repo_full_name"],
                    pr_info["pr_number"],
                    pr_info.get("head_sha") or pr_info.get("after"),
                )
                return JSONResponse(content={
                    "status": "accepted",
                    "action": "queued_incremental",
                    "pr": f"{pr_info['repo_full_name']}#{pr_info['pr_number']}",
                    "head_sha": pr_info.get("head_sha") or pr_info.get("after"),
                })

        # 提交审查任务到队列
        await _mark_agent_task_external_reviewing(pr_info)
        task_key = await submit_review_task(pr_info)

        logger.info(
            f"已提交审查任务: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
            f"任务标识: {task_key}"
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "审查任务已提交",
                "pr": f"{pr_info['repo_full_name']}#{pr_info['pr_number']}",
                "action": action,
                "task_key": task_key,
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

        # 处理评论编辑（包括复选框切换）
        if action == "edited":
            return await handle_comment_edited_event(payload)

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
            # 检查 /agent 命令
            if re.match(r"^/agent(\s|$)", comment_body):
                if issue.get("pull_request"):
                    return await handle_pr_agent_command(payload)
                return await handle_agent_command(payload)
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
            if permission == "unknown":
                return await _permission_check_unavailable_response(
                    github_app,
                    repo_owner,
                    repo_name,
                    repo_full_name,
                    pr_number,
                    commenter_login,
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
                "body": pr.body or "",
                "branch": pr.head.ref,
                "head_sha": getattr(pr.head, "sha", None),
                "base_branch": pr.base.ref,
                "diff_url": pr.diff_url,
                "patch_url": pr.patch_url,
                "html_url": pr.html_url,
                "state": pr.state,
                "draft": pr.draft,
                "merged": pr.merged,
            }

            try:
                async with get_async_session() as session:
                    svc = TelegramService(session)
                    trigger_user = await svc.get_user_by_github_username(
                        commenter_login
                    )
                    if trigger_user:
                        pr_info["user_id"] = trigger_user.id
                    else:
                        author_user = await svc.get_user_by_github_username(
                            pr.user.login
                        )
                        if author_user:
                            pr_info["user_id"] = author_user.id
            except Exception as e:
                logger.warning(f"解析 /full-review 用户配置上下文失败: {e}")
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
        task_key = await submit_review_task(pr_info)

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
            f"task_key={task_key}, triggered_by={commenter_login}"
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "全量审查任务已提交",
                "pr": f"{repo_full_name}#{pr_number}",
                "deleted_comments": deleted_result,
                "dismissed_reviews": dismissed_reviews,
                "task_key": task_key,
            }
        )

    except Exception as e:
        logger.error(f"处理Issue Comment事件时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def _handle_label_checkbox_toggle_inner(
    repo_owner: str,
    repo_name: str,
    pr_number: int,
    old_body: str,
    new_body: str,
    editor_login: str,
    pr_author_login: str,
    comment_source: str,
    comment_id: Optional[int] = None,
) -> JSONResponse:
    """Shared logic for detecting label checkbox toggles and applying changes.

    Args:
        repo_owner: Repository owner login.
        repo_name: Repository name.
        pr_number: PR number.
        old_body: Comment body before the edit.
        new_body: Comment body after the edit.
        editor_login: GitHub username of the person who edited the comment.
        pr_author_login: GitHub username of the PR author.
        comment_source: "issue_comment" or "pull_request_review" for logging.
        comment_id: Comment/review ID for restoring checkbox state on denial.

    Returns:
        JSONResponse with the result.
    """
    # Lazy import to avoid circular dependency: webhook → label_service → github_app (webhook already imports it)
    from backend.services.label_service import label_service

    # Quick check: is this a Sakura label comment?
    if not label_service.is_sakura_label_comment(new_body):
        return JSONResponse(
            content={"status": "ignored", "reason": "not a Sakura label comment"}
        )

    # Detect checkbox changes
    labels_to_add, labels_to_remove = label_service.parse_checkbox_changes(
        old_body, new_body
    )

    if not labels_to_add and not labels_to_remove:
        return JSONResponse(
            content={"status": "ignored", "reason": "no checkbox changes detected"}
        )

    logger.info(
        f"[{comment_source}] 检测到标签复选框变化: "
        f"add={labels_to_add}, remove={labels_to_remove}, "
        f"editor={editor_login}, {repo_owner}/{repo_name}#{pr_number}"
    )

    # Permission check: PR author or collaborator (admin/write)
    #
    # Special case for pull_request_review: GitHub attributes review body edits
    # to the review author (the bot).  Although `sender` gives us the actual
    # editor, we skip permission checks for review bodies to avoid infinite
    # revert loops (bot reverting triggers another edited event) and to allow
    # user interaction with label checkboxes.  This is an intentional security
    # trade-off: only users with repo access can see and interact with PRs, so
    # the risk is bounded by GitHub's own access controls.
    is_pr_author = editor_login == pr_author_login
    is_collaborator = False
    is_review_body_edit = comment_source == "pull_request_review"
    github_app: Optional[GitHubAppClient] = None

    if not is_pr_author and not is_review_body_edit:
        github_app = GitHubAppClient()
        permission = await asyncio.to_thread(
            github_app.check_collaborator_permission,
            repo_owner, repo_name, editor_login,
        )
        if permission == "unknown":
            logger.warning(
                f"[{comment_source}] 无法校验用户 {editor_login} 在 "
                f"{repo_owner}/{repo_name} 的权限，跳过标签切换"
            )
            return JSONResponse(
                status_code=503,
                content={"status": "error", "reason": "permission check unavailable"},
            )
        is_collaborator = permission in ("admin", "write")

    if not is_pr_author and not is_collaborator and not is_review_body_edit:
        logger.info(
            f"[{comment_source}] 用户 {editor_login} 无权切换标签 "
            f"(非PR作者且非仓库协作者)"
        )
        # Restore original checkbox state and post a notice comment
        try:
            if github_app is None:
                github_app = GitHubAppClient()
            assert github_app is not None

            def _revert_and_notify() -> None:
                client = github_app.get_repo_client(repo_owner, repo_name)
                if not client:
                    return
                repo = client.get_repo(f"{repo_owner}/{repo_name}")
                # Restore the original comment body to revert checkbox changes
                if comment_id is not None:
                    if comment_source == "issue_comment":
                        comment_obj = repo.get_issue(pr_number).get_comment(comment_id)
                    else:
                        comment_obj = repo.get_pull(pr_number).get_review(
                            comment_id
                        )
                    comment_obj.edit(old_body)
                    logger.info(
                        f"[{comment_source}] 已恢复评论 #{comment_id} 的原始复选框状态"
                    )
                # Post a notice about insufficient permission
                pr = repo.get_pull(pr_number)
                pr.create_issue_comment(
                    f"❌ @{editor_login}，只有 PR 作者或仓库管理员/协作者才能切换标签复选框。"
                )

            await asyncio.to_thread(_revert_and_notify)
        except Exception as e:
            logger.warning(f"[{comment_source}] 恢复复选框/回复无权限提示失败: {e}")

        return JSONResponse(
            content={"status": "denied", "reason": "insufficient permission"}
        )

    # Apply the label changes
    result = await label_service.handle_label_checkbox_toggle(
        repo_owner=repo_owner,
        repo_name=repo_name,
        pr_number=pr_number,
        labels_to_add=labels_to_add,
        labels_to_remove=labels_to_remove,
        operator=editor_login,
        pr_author=pr_author_login,
    )

    applied = result.get("applied", [])
    removed = result.get("removed", [])
    failed = result.get("failed", [])

    logger.info(
        f"[{comment_source}] 标签复选框操作完成: "
        f"applied={applied}, removed={removed}, failed={failed}"
    )

    return JSONResponse(
        content={
            "status": "ok",
            "applied": applied,
            "removed": removed,
            "failed": failed,
        }
    )


async def handle_comment_edited_event(payload: Dict[str, Any]) -> JSONResponse:
    """Handle issue_comment edited events to detect label checkbox toggles.

    When a user edits a comment in a PR and changes the checked state of a
    label checkbox in a Sakura review comment, this handler applies or removes
    the corresponding label on the PR.
    """
    try:
        # Must be a PR comment (issue with pull_request field)
        issue = payload.get("issue", {})
        if not issue.get("pull_request"):
            return JSONResponse(
                content={"status": "ignored", "reason": "not a PR comment"}
            )

        comment = payload.get("comment", {})
        changes = payload.get("changes", {})
        new_body = comment.get("body", "")
        old_body = changes.get("body", {}).get("from", "") if changes else ""

        if not old_body:
            return JSONResponse(
                content={"status": "ignored", "reason": "no old body in changes"}
            )

        repo_info = payload.get("repository", {})
        repo_owner = repo_info.get("owner", {}).get("login", "")
        repo_name = repo_info.get("name", "")
        pr_number = issue.get("number")

        if not all([repo_owner, repo_name, pr_number]):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        editor_login = comment.get("user", {}).get("login", "")
        pr_author_login = issue.get("user", {}).get("login", "")

        if not editor_login:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法获取编辑者信息"},
            )

        comment_id = comment.get("id")

        return await _handle_label_checkbox_toggle_inner(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pr_number=pr_number,
            old_body=old_body,
            new_body=new_body,
            editor_login=editor_login,
            pr_author_login=pr_author_login,
            comment_source="issue_comment",
            comment_id=comment_id,
        )

    except Exception as e:
        logger.error(f"处理评论编辑事件时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_pull_request_review_event(
    payload: Dict[str, Any],
) -> JSONResponse:
    """Handle pull_request_review events.

    Currently handles the ``edited`` action to detect label checkbox toggles
    in PR review body edits.
    """
    try:
        action = payload.get("action")

        # Only handle edited reviews (checkbox toggles)
        if action != "edited":
            return JSONResponse(content={"status": "ignored", "action": action})

        review = payload.get("review", {})
        changes = payload.get("changes", {})
        new_body = review.get("body", "")
        old_body = changes.get("body", {}).get("from", "") if changes else ""

        if not old_body:
            return JSONResponse(
                content={"status": "ignored", "reason": "no old body in changes"}
            )

        pr_info_payload = payload.get("pull_request", {})
        repo_info = payload.get("repository", {})

        repo_owner = repo_info.get("owner", {}).get("login", "")
        repo_name = repo_info.get("name", "")
        pr_number = pr_info_payload.get("number")

        if not all([repo_owner, repo_name, pr_number]):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取PR信息"},
            )

        editor_login = payload.get("sender", {}).get("login", "")
        pr_author_login = pr_info_payload.get("user", {}).get("login", "")

        if not editor_login:
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法获取编辑者信息"},
            )

        review_id = review.get("id")

        return await _handle_label_checkbox_toggle_inner(
            repo_owner=repo_owner,
            repo_name=repo_name,
            pr_number=pr_number,
            old_body=old_body,
            new_body=new_body,
            editor_login=editor_login,
            pr_author_login=pr_author_login,
            comment_source="pull_request_review",
            comment_id=review_id,
        )

    except Exception as e:
        logger.error(f"处理PR Review事件时出错: {e}", exc_info=True)
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

        if permission == "unknown":
            return await _permission_check_unavailable_response(
                github_app,
                repo_owner,
                repo_name,
                repo_full_name,
                pr_number,
                commenter_login,
                log_prefix="/revoke",
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
        supported_actions = ["opened", "edited", "reopened", "closed", "deleted"]
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

        # deleted 事件：清理数据库记录和向量索引 / deleted event: clean up DB and vector index
        if action == "deleted":
            repo_owner = issue_info["repo_owner"]
            repo_name = issue_info["repo_name"]
            issue_number = issue_info["issue_number"]
            logger.info(f"处理 Issue 删除事件: {repo_owner}/{repo_name}#{issue_number}")

            # 延迟导入避免循环依赖 / lazy import to avoid circular dependency
            from backend.services.issue_service import issue_service

            async with get_async_session() as session:
                cleanup_result = await issue_service.delete_issue_data(
                    repo_owner, repo_name, issue_number, session
                )

            return JSONResponse(
                content={
                    "status": "accepted",
                    "action": "deleted",
                    "cleanup": cleanup_result,
                }
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
                        # 同步数据库中的 issue_state
                        try:
                            from backend.models.database import (
                                IssueAnalysis as _IA,
                                async_session as _as,
                            )
                            from sqlalchemy import update as sql_update

                            _repo_full = f"{repo_owner}/{repo_name}"
                            async with _as() as _session:
                                await _session.execute(
                                    sql_update(_IA)
                                    .where(
                                        _IA.repo_name == _repo_full,
                                        _IA.issue_number == issue_number,
                                    )
                                    .values(issue_state="open")
                                )
                                await _session.commit()
                        except Exception as _e:
                            logger.warning(f"同步 Issue reopened 状态到数据库失败: {_e}")
                else:
                    logger.debug("跳过 Pull Request 的 Issue 向量同步")
            except Exception as e:
                logger.warning(f"语义 Issue 向量同步失败: {e}")

        # closed 事件：向量同步 + 数据库 issue_state 更新
        if action == "closed":
            # 同步数据库中的 issue_state
            try:
                from backend.models.database import IssueAnalysis, async_session
                from sqlalchemy import update as sql_update

                repo_full = f"{repo_owner}/{repo_name}"
                async with async_session() as session:
                    await session.execute(
                        sql_update(IssueAnalysis)
                        .where(
                            IssueAnalysis.repo_name == repo_full,
                            IssueAnalysis.issue_number == issue_number,
                        )
                        .values(issue_state="closed")
                    )
                    await session.commit()
            except Exception as e:
                logger.warning(f"同步 Issue closed 状态到数据库失败: {e}")

            # 失效候选池缓存
            try:
                from backend.services.agent_team.candidate_service import (
                    AgentTeamCandidateService,
                )

                AgentTeamCandidateService().invalidate_cache()
            except Exception:
                pass

            return JSONResponse(
                content={
                    "status": "accepted",
                    "action": "closed",
                    "sync": "vector_and_db",
                }
            )

        # 检查功能是否启用
        if not await get_dynamic_config("enable_issue_analysis"):
            logger.info("Issue 分析功能未启用")
            return JSONResponse(
                content={"status": "skipped", "reason": "feature disabled"}
            )

        # Telegram 权限检查
        notification_sender = get_notification_sender()
        async with get_async_session() as session:
            service = TelegramService(session)

            github_username = issue_info.get("repo_owner", "")
            if not github_username:
                logger.warning("无法获取 Issue 仓库所有者")
                return JSONResponse(
                    content={"status": "skipped", "reason": "unknown repo owner"}
                )

            user = await service.get_user_by_github_username(github_username)
            if not user:
                logger.info(f"Issue 仓库所有者未注册: {github_username}，跳过分析")
                return JSONResponse(
                    content={"status": "skipped", "reason": "unregistered repo owner"}
                )
            issue_info["user_id"] = user.id

            # Issue 配额检查
            allowed, reason = await service.check_and_consume_issue_quota(
                github_username=github_username,
                repo_name=issue_info["repo_full_name"],
                issue_number=issue_info["issue_number"],
            )
            if not allowed:
                logger.warning(
                    f"Issue 配额不足: {github_username} (仓库所有者) - {reason}"
                )
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
        if not await get_dynamic_config("enable_issue_analysis"):
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
            issue_info["user_id"] = user.id

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


async def _post_issue_comment(
    github_app: "GitHubAppClient",
    repo_owner: str,
    repo_name: str,
    repo_full_name: str,
    issue_number: int,
    message: str,
) -> None:
    """发送 Issue 评论（同步 GitHub API 通过 asyncio.to_thread 包装，失败静默）"""
    try:
        def _do_post():
            client = github_app.get_repo_client(repo_owner, repo_name)
            if client:
                repo = client.get_repo(repo_full_name)
                repo.get_issue(issue_number).create_comment(message)

        await asyncio.to_thread(_do_post)
    except Exception as e:
        logger.warning("发送 Issue 评论失败: {}#{} - {}", repo_full_name, issue_number, e)


async def _permission_check_unavailable_response(
    github_app: "GitHubAppClient",
    repo_owner: str,
    repo_name: str,
    repo_full_name: str,
    issue_number: int,
    commenter: str,
    log_prefix: str = "权限检查",
) -> JSONResponse:
    """权限校验不可用时返回可重试错误，避免误报用户无权限。"""
    logger.warning(
        "{} 无法校验用户 {} 在 {} 的权限，请稍后重试",
        log_prefix,
        commenter,
        repo_full_name,
    )
    await _post_issue_comment(
        github_app,
        repo_owner,
        repo_name,
        repo_full_name,
        issue_number,
        f"⚠️ @{commenter}，暂时无法连接 GitHub 校验权限，请稍后重试。",
    )
    return JSONResponse(
        status_code=503,
        content={"status": "error", "reason": "permission check unavailable"},
    )


def _parse_agent_base_branch(
    comment_body: str,
) -> tuple[str | None, JSONResponse | None]:
    """解析 ``base:(\\S+)`` 参数。返回 (base_branch, error_response)。

    error_response 非 None 表示分支名校验失败，调用方应直接返回该响应。
    """
    base_branch = None
    branch_match = re.search(r"base:(\S+)", comment_body)
    if branch_match:
        base_branch = branch_match.group(1)
        if ".." in base_branch or not re.match(r"^[a-zA-Z0-9._/\-]+$", base_branch):
            return None, JSONResponse(
                content={"status": "error", "reason": f"无效的分支名: {base_branch}"}
            )
    return base_branch, None


def _is_bot_self_comment(commenter: str) -> bool:
    """判断是否为 Bot 自身评论（需忽略）。"""
    bot_username = settings.bot_username
    return bool(bot_username and commenter == bot_username)


async def _check_agent_team_enabled() -> JSONResponse | None:
    """检查 Agent 团队功能开关。返回错误响应或 None（通过）。"""
    if not await get_dynamic_config("agent_team_enabled"):
        return JSONResponse(
            content={"status": "skipped", "reason": "agent team feature disabled"}
        )
    return None


async def _check_agent_permission(
    github_app: "GitHubAppClient",
    repo_owner: str,
    repo_name: str,
    repo_full_name: str,
    commenter: str,
    issue_number: int,
    log_prefix: str = "/agent",
) -> JSONResponse | None:
    """校验评论者权限（admin/write）。返回错误响应或 None（通过）。"""
    permission = await asyncio.to_thread(
        github_app.check_collaborator_permission,
        repo_owner,
        repo_name,
        commenter,
    )
    if permission == "unknown":
        return await _permission_check_unavailable_response(
            github_app,
            repo_owner,
            repo_name,
            repo_full_name,
            issue_number,
            commenter,
            log_prefix=log_prefix,
        )

    if permission not in ("admin", "write"):
        logger.info("{} 权限不足: {} 权限为 {}", log_prefix, commenter, permission)
        await _post_issue_comment(
            github_app,
            repo_owner,
            repo_name,
            repo_full_name,
            issue_number,
            f"❌ @{commenter}，只有仓库管理员/协作者才能触发 Agent 任务。",
        )
        return JSONResponse(
            content={"status": "denied", "reason": "insufficient permission"}
        )
    return None


async def _consume_agent_quota_or_cleanup(
    github_app: "GitHubAppClient",
    repo_owner: str,
    repo_name: str,
    repo_full_name: str,
    task_id: int,
    issue_number: int,
    log_prefix: str = "/agent",
) -> JSONResponse | None:
    """消耗仓库所有者 Agent 配额；失败时清理孤儿任务并回复。

    返回 None 表示配额消耗成功；返回 JSONResponse 表示失败（调用方应直接返回）。
    """
    async with get_async_session() as session:
        service = TelegramService(session)
        ok, reason = await service.check_and_consume_agent_quota(
            github_username=repo_owner,
            repo_name=repo_full_name,
            task_id=task_id,
        )
    if ok:
        return None

    is_unregistered = "未注册" in reason
    if is_unregistered:
        reply = f"❌ 仓库所有者 @{repo_owner} 尚未注册，无法使用 Agent 任务。请先注册后再试。"
    else:
        reply = f"❌ Agent 配额不足（仓库所有者 @{repo_owner}）：{reason}"
    logger.warning("{} 配额检查失败: repo_owner={} - {}", log_prefix, repo_owner, reason)

    # 延迟导入：避免 webhook ↔ agent_team_models 循环依赖
    from backend.models.agent_team_models import AgentTeamTask as _ATT

    try:
        async with get_async_session() as cleanup_session:
            await cleanup_session.execute(
                _ATT.__table__.delete().where(_ATT.id == task_id)
            )
            await cleanup_session.commit()
            logger.info("{} 已清理孤儿任务: task_id={}", log_prefix, task_id)
    except Exception as cleanup_err:
        logger.warning(
            "{} 清理孤儿任务失败: task_id={} - {}", log_prefix, task_id, cleanup_err
        )

    await _post_issue_comment(
        github_app, repo_owner, repo_name, repo_full_name, issue_number, reply
    )
    return JSONResponse(
        content={
            "status": "skipped",
            "reason": "unregistered" if is_unregistered else "quota exceeded",
            "detail": reason,
        }
    )


async def handle_agent_command(payload: Dict[str, Any]) -> JSONResponse:
    """处理 /agent 命令：将已分析的 Issue 委派给 Agent 团队执行"""
    try:
        comment_body = payload.get("comment", {}).get("body", "").strip()

        # 解析 base_branch 参数
        base_branch, err = _parse_agent_base_branch(comment_body)
        if err:
            return err

        # 过滤 Bot 自身评论
        commenter = payload.get("comment", {}).get("user", {}).get("login", "")
        if _is_bot_self_comment(commenter):
            return JSONResponse(
                content={"status": "ignored", "reason": "bot self-comment"}
            )

        # 提取仓库和 Issue 信息
        repo_info = payload.get("repository", {})
        issue = payload.get("issue", {})
        repo_owner = repo_info.get("owner", {}).get("login", "")
        repo_name = repo_info.get("name", "")
        repo_full_name = repo_info.get("full_name", "")
        issue_number = issue.get("number")

        if not all([repo_owner, repo_name, repo_full_name, issue_number]):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取 Issue 信息"},
            )

        logger.info(
            "/agent 指令: {}#{}, 评论者: {}, base: {}",
            repo_full_name, issue_number, commenter, base_branch or "default",
        )

        # 功能开关检查
        if err := await _check_agent_team_enabled():
            return err

        # 权限检查：仅 admin/write 用户可触发
        github_app = GitHubAppClient()
        if err := await _check_agent_permission(
            github_app, repo_owner, repo_name, repo_full_name, commenter, issue_number,
        ):
            return err

        # 前置校验：检查是否有已完成的 Issue 分析记录，或是否为扫描自动创建的报告 Issue
        # 延迟导入：避免 webhook ↔ database/scan_models 循环依赖
        from sqlalchemy import select, and_, desc
        from backend.models.database import IssueAnalysis, IssueAnalysisStatus
        from backend.models.scan_models import RepoScan, ScanFinding

        scan_report = None
        scan_findings = []
        async with get_async_session() as session:
            existing_analysis = await session.scalar(
                select(IssueAnalysis)
                .where(
                    and_(
                        IssueAnalysis.repo_owner == repo_owner,
                        IssueAnalysis.repo_name.in_({repo_name, repo_full_name}),
                        IssueAnalysis.issue_number == issue_number,
                        IssueAnalysis.status == IssueAnalysisStatus.COMPLETED.value,
                    )
                )
                .order_by(desc(IssueAnalysis.completed_at))
                .limit(1)
            )
            if not existing_analysis:
                scan_report = await session.scalar(
                    select(RepoScan)
                    .where(
                        and_(
                            RepoScan.repo_owner == repo_owner,
                            RepoScan.repo_name.in_({repo_name, repo_full_name}),
                            RepoScan.report_issue_number == issue_number,
                        )
                    )
                    .order_by(desc(RepoScan.completed_at), desc(RepoScan.created_at))
                    .limit(1)
                )
                if scan_report:
                    result = await session.execute(
                        select(ScanFinding)
                        .where(ScanFinding.scan_id == scan_report.id)
                        .order_by(ScanFinding.severity, desc(ScanFinding.confidence))
                    )
                    scan_findings = list(result.scalars().all())

        if not existing_analysis and not scan_report:
            logger.info("/agent 无分析记录或扫描报告: {}#{}", repo_full_name, issue_number)
            await _post_issue_comment(
                github_app, repo_owner, repo_name, repo_full_name, issue_number,
                "❌ 此 Issue 尚未完成 AI 分析，也未匹配到 Sakura 仓库扫描报告。请先使用 `/analyze` 命令分析此 Issue。",
            )
            return JSONResponse(
                content={
                    "status": "skipped",
                    "reason": "no completed analysis or scan report",
                }
            )

        # 构建提交上下文（与通过 WebUI 创建任务一致）
        # 延迟导入：避免 webhook ↔ agent_team_submission_context 循环依赖
        from backend.services.agent_team.submission_context import (
            build_agent_task_summary,
            build_issue_context_markdown,
            format_issue_analysis_context,
            load_issue_comments_for_context,
        )

        overrides = {}
        async with get_async_session() as session:
            analysis_ctx = format_issue_analysis_context(existing_analysis)
            issue_comments = await load_issue_comments_for_context(
                repo_owner=repo_owner,
                repo_name=repo_name,
                issue_number=issue_number,
            )
            issue_context_md = build_issue_context_markdown(
                repo_full_name=repo_full_name,
                issue_number=issue_number,
                issue_analysis_context=analysis_ctx,
                issue_comments=issue_comments,
            )
            task_summary = existing_analysis.summary if existing_analysis else ""
            if scan_report:
                # 延迟导入：避免 webhook ↔ scan_report_service/agent_team_models 循环依赖
                from backend.services.scan_report_service import ScanReportService
                from backend.models.agent_team_models import AgentTeamSourceType

                scan_markdown = ScanReportService().generate_issue_body(
                    scan_report, scan_findings
                )
                task_summary = scan_markdown
                highest = min(
                    (SCAN_SEVERITY_ORDER.get(f.severity, 4) for f in scan_findings),
                    default=3,
                )
                priority = "critical" if highest == 0 else "high" if highest == 1 else "medium"
                overrides.update(
                    {
                        "source_type": AgentTeamSourceType.SCAN_REPORT_ISSUE.value,
                        "source_id": scan_report.id,
                        "source_issue_number": issue_number,
                        "title": f"处理扫描报告 Issue #{issue_number}",
                        "priority": priority,
                        "candidate_score": 90 if highest == 0 else 80 if highest == 1 else 60,
                    }
                )
            agent_task_context = build_agent_task_summary(task_summary or "", issue_context_md)
            if agent_task_context:
                overrides["summary"] = agent_task_context

        # 创建 Agent 任务
        # 延迟导入：避免 webhook ↔ agent_team_candidate_service 循环依赖
        from backend.services.agent_team.candidate_service import (
            AgentTeamCandidateService,
        )

        candidate_service = AgentTeamCandidateService()
        async with get_async_session() as session:
            try:
                task = await candidate_service.create_task_from_manual_issue(
                    db=session,
                    repo_full_name=repo_full_name,
                    issue_number=issue_number,
                    started_by=commenter,
                    base_branch=base_branch,
                    overrides=overrides if overrides else None,
                )
            except ValueError as e:
                logger.warning("/agent 创建任务失败: {}", e)
                await _post_issue_comment(
                    github_app, repo_owner, repo_name, repo_full_name, issue_number,
                    f"❌ 无法创建 Agent 任务：{e}",
                )
                return JSONResponse(
                    content={
                        "status": "error",
                        "reason": "Failed to create task",
                    }
                )

            task_id = task.id

        # 仓库所有者配额消耗（任务创建成功后，使用实际 task_id）
        if err := await _consume_agent_quota_or_cleanup(
            github_app, repo_owner, repo_name, repo_full_name, task_id, issue_number,
        ):
            return err

        # 后台执行任务
        # 延迟导入：避免 webhook ↔ agent_team_worker 循环依赖
        from backend.workers.agent_team_worker import submit_agent_team_task

        asyncio.create_task(submit_agent_team_task(task_id))

        # 回复确认评论
        branch_info = f"，基础分支：`{base_branch}`" if base_branch else ""
        await _post_issue_comment(
            github_app, repo_owner, repo_name, repo_full_name, issue_number,
            f"已创建 Agent 任务（ID: {task_id}）{branch_info}\n\n"
            f"由 @{commenter} 触发",
        )

        logger.info(
            "/agent 任务已创建: {}#{}, task_id={}, base={}",
            repo_full_name, issue_number, task_id, base_branch or "default",
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "Agent 任务已创建并开始执行",
                "task_id": task_id,
            }
        )

    except Exception as e:
        logger.error("处理 /agent 命令时出错: {}", e, exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": "内部服务错误"}
        )


async def handle_pr_agent_command(payload: Dict[str, Any]) -> JSONResponse:
    """处理 PR 评论中的 /agent 命令：基于 PR 审查记录创建 Agent 修复任务。

    流程：
    1. 权限/开关校验
    2. 通过 GitHub API 获取 PR head_sha
    3. 调用 candidate_service.create_task_from_pr_review()（自带 duplicate guard）
    4. 扣除配额 → 调度后台任务 → 回复确认评论

    同一 PR 仅允许一个非终态 /agent 任务，支持多轮迭代。
    """
    try:
        comment_body = payload.get("comment", {}).get("body", "").strip()

        # 解析 base_branch 参数（可选）
        base_branch, err = _parse_agent_base_branch(comment_body)
        if err:
            return err

        # 过滤 Bot 自身评论
        commenter = payload.get("comment", {}).get("user", {}).get("login", "")
        if _is_bot_self_comment(commenter):
            return JSONResponse(
                content={"status": "ignored", "reason": "bot self-comment"}
            )

        # 提取仓库和 PR 信息
        repo_info = payload.get("repository", {})
        issue = payload.get("issue", {})
        repo_owner = repo_info.get("owner", {}).get("login", "")
        repo_name = repo_info.get("name", "")
        repo_full_name = repo_info.get("full_name", "")
        pr_number = issue.get("number")

        if not all([repo_owner, repo_name, repo_full_name, pr_number]):
            return JSONResponse(
                status_code=400,
                content={"status": "error", "message": "无法提取 PR 信息"},
            )

        logger.info(
            "/agent PR 指令: {}#{}, 评论者: {}, base: {}",
            repo_full_name, pr_number, commenter, base_branch or "default",
        )

        # 功能开关检查
        if err := await _check_agent_team_enabled():
            return err

        # 权限检查
        github_app = GitHubAppClient()
        if err := await _check_agent_permission(
            github_app, repo_owner, repo_name, repo_full_name, commenter, pr_number,
            log_prefix="/agent PR",
        ):
            return err

        # 获取 PR head_sha（供 create_task_from_pr_review 记录）
        def _get_pr_head_sha():
            client = github_app.get_repo_client(repo_owner, repo_name)
            if not client:
                return None
            repo = client.get_repo(repo_full_name)
            pr = repo.get_pull(pr_number)
            return pr.head.sha if pr and pr.head else None

        head_sha = await asyncio.to_thread(_get_pr_head_sha)

        # 创建 Agent 修复任务（自带 duplicate guard + PRReview 存在性检查）
        from backend.services.agent_team.candidate_service import (
            AgentTeamCandidateService,
        )

        candidate_service = AgentTeamCandidateService()
        async with get_async_session() as session:
            try:
                task = await candidate_service.create_task_from_pr_review(
                    db=session,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    started_by=commenter,
                    base_branch=base_branch,
                    head_sha=head_sha,
                )
            except ValueError as e:
                logger.warning("/agent PR 创建任务失败: {}", e)
                await _post_issue_comment(
                    github_app, repo_owner, repo_name, repo_full_name, pr_number,
                    "❌ 无法创建 Agent 修复任务，请稍后重试或联系仓库管理员。",
                )
                return JSONResponse(
                    content={"status": "error", "reason": "failed to create agent task"}
                )

            task_id = task.id

        # 仓库所有者配额消耗
        if err := await _consume_agent_quota_or_cleanup(
            github_app, repo_owner, repo_name, repo_full_name, task_id, pr_number,
            log_prefix="/agent PR",
        ):
            return err

        # 后台执行任务
        from backend.workers.agent_team_worker import submit_agent_team_task

        asyncio.create_task(submit_agent_team_task(task_id))

        # 回复确认评论
        branch_info = f"，基础分支：`{base_branch}`" if base_branch else ""
        await _post_issue_comment(
            github_app, repo_owner, repo_name, repo_full_name, pr_number,
            f"🤖 Agent 修复任务已创建（ID: {task_id}）{branch_info}\n\n"
            f"将基于 PR #{pr_number} 的审查意见创建独立修复分支并提交 PR。\n"
            f"由 @{commenter} 触发",
        )

        logger.info(
            "/agent PR 任务已创建: {}#{}, task_id={}, base={}",
            repo_full_name, pr_number, task_id, base_branch or "default",
        )

        return JSONResponse(
            content={
                "status": "accepted",
                "message": "Agent 修复任务已创建并开始执行",
                "task_id": task_id,
            }
        )

    except Exception as e:
        logger.error("处理 /agent PR 命令时出错: {}", e, exc_info=True)
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

        logger.info(f"GitHub App installation 事件: {action}, account={account_login}")

        # 清除该用户的安装状态 Redis 缓存
        try:
            from backend.core.redis import get_async_redis

            r = await get_async_redis()
            cache_key = f"github_app_installed:{account_login.lower()}"
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


@router.post("/stripe")
async def handle_stripe_webhook(
    request: Request,
) -> JSONResponse:
    """Handle Stripe webhook events (checkout.session.completed, etc.)"""
    from backend.services.payment import get_gateway, WebhookEventType
    from backend.services.payment_service import PaymentService, PaymentError

    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        gateway = await get_gateway("stripe")
        event = gateway.verify_webhook(payload, headers)

        if event.event_type == WebhookEventType.UNKNOWN:
            # Return 200 for unmapped but valid events so Stripe doesn't retry
            return JSONResponse(
                content={"status": "ignored", "message": "Event type not handled"}
            )

        async with get_async_session() as db:
            svc = PaymentService(db)
            try:
                if event.event_type == WebhookEventType.PAYMENT_COMPLETED:
                    order = await svc.confirm_payment(
                        order_no=event.order_no,
                        provider_tx_id=event.provider_tx_id,
                        paid_amount_cents=event.amount_cents,
                        paid_currency=event.currency,
                    )
                    await db.commit()
                    logger.info(
                        "Stripe webhook: payment confirmed for order {}",
                        order.order_no,
                    )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_completed"}
                    )

                elif event.event_type == WebhookEventType.PAYMENT_EXPIRED:
                    await svc.cancel_and_commit_if_needed(event.order_no)
                    logger.info(
                        "Stripe webhook: order expired/cancelled {}",
                        event.order_no,
                    )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_expired"}
                    )

                elif event.event_type == WebhookEventType.PAYMENT_REFUNDED:
                    # For refund events from Stripe, find order by provider_tx_id
                    from sqlalchemy import select
                    from backend.models.payment_models import Order, OrderStatus

                    stmt = select(Order).where(
                        Order.provider_tx_id == event.provider_tx_id,
                        Order.status == OrderStatus.FULFILLED.value,
                    )
                    order = (await db.execute(stmt)).scalar_one_or_none()
                    if order:
                        await svc.process_refund(order_id=order.id)
                        await db.commit()
                        logger.info(
                            "Stripe webhook: refund processed for order {}",
                            order.order_no,
                        )
                    else:
                        logger.info(
                            "Stripe webhook: refund event but no actionable order for tx {}",
                            event.provider_tx_id,
                        )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_refunded"}
                    )

                else:
                    logger.info("Stripe webhook: ignoring event type {}", event.event_type)
                    return JSONResponse(
                        content={"status": "ignored", "event": str(event.event_type)}
                    )

            except PaymentError as e:
                await db.rollback()
                logger.warning("Stripe webhook processing error: {}", e)
                return JSONResponse(
                    status_code=200,
                    content={"status": "error", "message": "Payment processing failed"},
                )

    except ValueError as e:
        logger.warning("Stripe webhook gateway error: {}", e)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Invalid request"},
        )
    except Exception as e:
        logger.error("Stripe webhook unexpected error: {} - {}", type(e).__name__, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal server error"},
        )


@router.post("/paddle")
async def handle_paddle_webhook(
    request: Request,
) -> JSONResponse:
    """Handle Paddle Billing webhook events (transaction.completed, etc.)"""
    from backend.services.payment import get_gateway, WebhookEventType
    from backend.services.payment_service import PaymentService, PaymentError

    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        gateway = await get_gateway("paddle")
        event = gateway.verify_webhook(payload, headers)

        if event.event_type == WebhookEventType.UNKNOWN:
            # Return 200 for unmapped but valid events so Paddle doesn't retry
            return JSONResponse(
                content={"status": "ignored", "message": "Event type not handled"}
            )

        async with get_async_session() as db:
            svc = PaymentService(db)
            try:
                if event.event_type == WebhookEventType.PAYMENT_COMPLETED:
                    order = await svc.confirm_payment(
                        order_no=event.order_no,
                        provider_tx_id=event.provider_tx_id,
                        paid_amount_cents=event.amount_cents,
                        paid_currency=event.currency,
                    )
                    await db.commit()
                    logger.info(
                        "Paddle webhook: payment confirmed for order {}",
                        order.order_no,
                    )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_completed"}
                    )

                elif event.event_type == WebhookEventType.PAYMENT_EXPIRED:
                    await svc.cancel_and_commit_if_needed(event.order_no)
                    logger.info(
                        "Paddle webhook: order expired/cancelled {}",
                        event.order_no,
                    )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_expired"}
                    )

                elif event.event_type == WebhookEventType.PAYMENT_REFUNDED:
                    # For refund events from Paddle, find order by provider_tx_id
                    from sqlalchemy import select
                    from backend.models.payment_models import Order, OrderStatus

                    stmt = select(Order).where(
                        Order.provider_tx_id == event.provider_tx_id,
                        Order.status == OrderStatus.FULFILLED.value,
                    )
                    order = (await db.execute(stmt)).scalar_one_or_none()
                    if order:
                        await svc.process_refund(order_id=order.id)
                        await db.commit()
                        logger.info(
                            "Paddle webhook: refund processed for order {}",
                            order.order_no,
                        )
                    else:
                        logger.info(
                            "Paddle webhook: refund event but no actionable order for tx {}",
                            event.provider_tx_id,
                        )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_refunded"}
                    )

                else:
                    logger.info("Paddle webhook: ignoring event type {}", event.event_type)
                    return JSONResponse(
                        content={"status": "ignored", "event": str(event.event_type)}
                    )

            except PaymentError as e:
                await db.rollback()
                logger.warning("Paddle webhook processing error: {}", e)
                return JSONResponse(
                    status_code=200,
                    content={"status": "error", "message": "Payment processing failed"},
                )

    except ValueError as e:
        logger.warning("Paddle webhook gateway error: {}", e)
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Invalid request"},
        )
    except Exception as e:
        logger.error("Paddle webhook unexpected error: {} - {}", type(e).__name__, e, exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal server error"},
        )


@router.post("/alipay", response_model=None)
async def handle_alipay_webhook(
    request: Request,
):
    """Handle Alipay async payment notification (当面付回调)

    支付宝回调为 POST form-urlencoded，验签成功后返回纯文本 "success"。
    """
    from backend.services.payment import get_gateway, WebhookEventType
    from backend.services.payment_service import PaymentService, PaymentError

    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        gateway = await get_gateway("alipay")
        event = gateway.verify_webhook(payload, headers)

        if event.event_type == WebhookEventType.UNKNOWN:
            logger.info("Alipay webhook: ignoring unmapped event")
            return PlainTextResponse("fail")

        async with get_async_session() as db:
            svc = PaymentService(db)
            try:
                if event.event_type == WebhookEventType.PAYMENT_COMPLETED:
                    order = await svc.confirm_payment(
                        order_no=event.order_no,
                        provider_tx_id=event.provider_tx_id,
                        paid_amount_cents=event.amount_cents,
                        paid_currency=event.currency,
                    )
                    await db.commit()
                    logger.info(
                        "Alipay webhook: payment confirmed for order {}",
                        order.order_no,
                    )
                    # 支付宝要求返回 "success" 纯文本
                    return PlainTextResponse("success")

                elif event.event_type == WebhookEventType.PAYMENT_EXPIRED:
                    await svc.cancel_and_commit_if_needed(event.order_no)
                    logger.info(
                        "Alipay webhook: order closed {}",
                        event.order_no,
                    )
                    return PlainTextResponse("success")

                else:
                    logger.info(
                        "Alipay webhook: ignoring event type {}", event.event_type
                    )
                    return PlainTextResponse("success")

            except PaymentError as e:
                await db.rollback()
                logger.warning("Alipay webhook processing error: {}", e)
                # 仍返回 success 避免支付宝重复通知
                return PlainTextResponse("success")

    except ValueError as e:
        logger.warning("Alipay webhook gateway error: {}", e)
        return PlainTextResponse("fail")
    except Exception as e:
        logger.error("Alipay webhook unexpected error: {} - {}", type(e).__name__, e, exc_info=True)
        return PlainTextResponse("fail")


@router.post("/nowpayments", response_model=None)
async def handle_nowpayments_webhook(
    request: Request,
) -> JSONResponse:
    """Handle NOWPayments IPN callback (virtual currency payment notification)

    NOWPayments sends POST JSON with x-nowpayments-sig header.
    Verification uses HMAC-SHA512 with IPN secret.
    """
    from backend.services.payment import get_gateway, WebhookEventType
    from backend.services.payment_service import PaymentService, PaymentError

    payload = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        gateway = await get_gateway("nowpayments")
        event = gateway.verify_webhook(payload, headers)

        if event.event_type == WebhookEventType.UNKNOWN:
            logger.info("NOWPayments webhook: ignoring unmapped event")
            return JSONResponse(content={"status": "ignored"})

        async with get_async_session() as db:
            svc = PaymentService(db)
            try:
                if event.event_type == WebhookEventType.PAYMENT_COMPLETED:
                    order = await svc.confirm_payment(
                        order_no=event.order_no,
                        provider_tx_id=event.provider_tx_id,
                        paid_amount_cents=event.amount_cents,
                        paid_currency=event.currency,
                    )
                    await db.commit()
                    logger.info(
                        "NOWPayments webhook: payment confirmed for order {}",
                        order.order_no,
                    )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_completed"}
                    )

                elif event.event_type == WebhookEventType.PAYMENT_EXPIRED:
                    await svc.cancel_and_commit_if_needed(event.order_no)
                    logger.info(
                        "NOWPayments webhook: order expired {}",
                        event.order_no,
                    )
                    return JSONResponse(
                        content={"status": "processed", "event": "payment_expired"}
                    )

                elif event.event_type == WebhookEventType.PAYMENT_REFUNDED:
                    logger.info("NOWPayments webhook: refund event received")
                    return JSONResponse(
                        content={"status": "processed", "event": "refund"}
                    )

                else:
                    logger.info(
                        "NOWPayments webhook: ignoring event type {}",
                        event.event_type,
                    )
                    return JSONResponse(content={"status": "ignored"})

            except PaymentError as e:
                await db.rollback()
                logger.warning("NOWPayments webhook processing error: {}", e)
                return JSONResponse(
                    content={"status": "error", "message": "Payment processing failed"}
                )

    except ValueError as e:
        logger.warning("NOWPayments webhook gateway error: {}", e)
        return JSONResponse(content={"status": "error", "message": "Invalid request"})
    except Exception as e:
        logger.error(
            "NOWPayments webhook unexpected error: {} - {}",
            type(e).__name__,
            e,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": "Internal server error"},
        )
