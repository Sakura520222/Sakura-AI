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
            status_code=500, content={"status": "error", "message": str(e)}
        )


@router.get("/health")
async def health_check() -> JSONResponse:
    """健康检查端点"""
    return JSONResponse(content={"status": "healthy", "service": "Sakura AI Reviewer"})
