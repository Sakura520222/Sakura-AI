"""管理员操作日志辅助函数"""

import json

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.log_context import get_log_context
from backend.core.log_sanitizer import sanitize
from backend.models.admin_action_log import AdminActionLog


async def log_admin_action(
    db: AsyncSession,
    admin_id: int,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    detail: dict | None = None,
):
    """记录管理员操作日志"""
    try:
        log_entry = AdminActionLog(
            admin_id=admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=json.dumps(sanitize(detail), ensure_ascii=False) if detail else None,
        )
        db.add(log_entry)
        await db.commit()
        logger.bind(**get_log_context(), audit_action=action, admin_id=admin_id).info(
            "Admin action recorded"
        )
    except Exception as e:
        logger.opt(exception=True).error("记录操作日志失败: {}", e)
        await db.rollback()
