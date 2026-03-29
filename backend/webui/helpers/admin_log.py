"""管理员操作日志辅助函数"""

import json
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.admin_action_log import AdminActionLog


async def log_admin_action(
    db: AsyncSession,
    admin_id: int,
    action: str,
    target_type: str = None,
    target_id: str = None,
    detail: dict = None,
):
    """记录管理员操作日志"""
    try:
        log_entry = AdminActionLog(
            admin_id=admin_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=json.dumps(detail, ensure_ascii=False) if detail else None,
        )
        db.add(log_entry)
        await db.commit()
    except Exception as e:
        logger.error(f"记录操作日志失败: {e}")
        await db.rollback()
