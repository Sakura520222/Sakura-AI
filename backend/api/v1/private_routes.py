"""HTTP caching policy shared by authenticated application API responses."""

from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException


class PrivateAPIRoute(APIRoute):
    """Cover concrete JSON/stream responses as well as FastAPI model responses."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def private_response(request):
            try:
                response = await original(request)
            except HTTPException as exc:
                exc.headers = {
                    **(exc.headers or {}),
                    "Cache-Control": "no-store",
                    "Vary": "Cookie, Authorization",
                }
                raise
            response.headers["Cache-Control"] = "no-store"
            response.headers["Vary"] = "Cookie, Authorization"
            return response

        return private_response
