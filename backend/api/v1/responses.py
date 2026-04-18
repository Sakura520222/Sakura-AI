"""API v1 统一响应封装"""

from typing import Any, Optional

from fastapi.responses import JSONResponse


def success_response(
    data: Any = None,
    message: str = "ok",
    status_code: int = 200,
) -> JSONResponse:
    """成功响应"""
    return JSONResponse(
        status_code=status_code,
        content={"success": True, "message": message, "data": data},
    )


def error_response(
    error: str,
    detail: Optional[str] = None,
    code: Optional[str] = None,
    status_code: int = 400,
) -> JSONResponse:
    """错误响应"""
    content: dict[str, Any] = {
        "success": False,
        "error": error,
    }
    if detail:
        content["detail"] = detail
    if code:
        content["code"] = code
    return JSONResponse(status_code=status_code, content=content)


def paginated_response(
    items: list,
    total: int,
    page: int,
    total_pages: int,
    per_page: int,
    message: str = "ok",
) -> JSONResponse:
    """分页成功响应"""
    return success_response(
        data={
            "items": items,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
        },
        message=message,
    )
