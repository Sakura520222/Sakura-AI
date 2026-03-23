"""GitHub Webhook API端点"""

from fastapi import APIRouter, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from typing import Dict, Any
from loguru import logger

from backend.core.github_app import (
    verify_webhook_signature,
    extract_pr_info_from_webhook,
)
from backend.workers.review_worker import submit_review_task
from backend.services.telegram_service import TelegramService
from backend.telegram.notifications import get_notification_sender
from backend.core.config import get_settings
import asyncio

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
        else:
            logger.info(f"忽略事件类型: {x_github_event}")
            return JSONResponse(content={"status": "ignored", "event": x_github_event})

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"处理Webhook时出错: {e}", exc_info=True)
        return JSONResponse(
            status_code=500, content={"status": "error", "message": str(e)}
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
        supported_actions = ["opened", "synchronized", "reopened"]
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

            # 2. 检查仓库是否已授权（管理员和超级管理员可以跳过此检查）
            # 管理员和超级管理员可以审查任何仓库
            # 转换为小写进行比较，支持大小写不敏感
            role_lower = user.role.lower().strip() if user.role else ""
            if role_lower in ["admin", "super_admin"]:
                logger.info(
                    f"管理员/超级管理员跳过仓库白名单检查: {github_username} (role: {user.role})"
                )
            else:
                # 普通用户需要仓库在白名单中
                is_authorized = await service.is_authorized_repo(
                    pr_info["repo_full_name"]
                )
                if not is_authorized:
                    logger.warning(f"未授权的仓库: {pr_info['repo_full_name']}")
                    if notification_sender:
                        await notification_sender.send_unauthorized_repo(
                            repo_name=pr_info["repo_full_name"],
                            pr_number=pr_info["pr_number"],
                        )
                    return JSONResponse(
                        content={
                            "status": "skipped",
                            "reason": "unauthorized repository",
                        }
                    )

            # 3. 检查并消耗配额
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
                        pr_number=pr_info["pr_number"],
                        reason=reason,
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
                await notification_sender.send_review_start(
                    repo_name=pr_info["repo_full_name"],
                    pr_number=pr_info["pr_number"],
                    pr_title=pr_info.get("title", ""),
                    author=github_username,
                )

        # 提交审查任务到队列
        task_id = await submit_review_task(pr_info)

        logger.info(
            f"已提交审查任务: {pr_info['repo_full_name']}#{pr_info['pr_number']}, "
            f"任务ID: {task_id}"
        )

        # 异步索引PR代码变更（不阻塞响应）
        asyncio.create_task(
            _index_pr_code_async(
                pr_info["repo_full_name"],
                pr_info["pr_number"],
                pr_info.get("install_id", 0),
            )
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
            status_code=500, content={"status": "error", "message": str(e)}
        )


@router.get("/health")
async def health_check() -> JSONResponse:
    """健康检查端点"""
    return JSONResponse(content={"status": "healthy", "service": "Sakura AI Reviewer"})


async def _index_pr_code_async(
    repo_full_name: str,
    pr_number: int,
    install_id: int,
) -> None:
    """异步索引PR代码变更

    Args:
        repo_full_name: 仓库名称
        pr_number: PR编号
        install_id: GitHub App安装ID
    """
    try:
        from backend.services.pr_code_indexer import get_pr_code_indexer

        indexer = get_pr_code_indexer()
        result = await indexer.index_pr_changes(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            install_id=install_id,
        )

        logger.info(
            f"PR #{pr_number} 代码索引完成: "
            f"索引={result.get('indexed', 0)}, "
            f"跳过={result.get('skipped', 0)}, "
            f"代码块={result.get('total_chunks', 0)}"
        )

    except Exception as e:
        logger.error(f"异步索引PR代码失败 (PR #{pr_number}): {e}", exc_info=True)
