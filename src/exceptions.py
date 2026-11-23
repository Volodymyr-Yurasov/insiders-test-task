"""Global exception hierarchy.

Module exceptions subclass these and override CODE/DETAIL. Every one is rendered by a single
handler as `{"code": ..., "detail": ..., **extra}` so clients get one error shape.
"""

from typing import Any, ClassVar

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from src.constants import ErrorCode


class DetailedHTTPException(Exception):
    STATUS_CODE: ClassVar[int] = status.HTTP_500_INTERNAL_SERVER_ERROR
    CODE: ClassVar[str] = ErrorCode.INTERNAL_ERROR
    DETAIL: ClassVar[str] = "Server error"
    HEADERS: ClassVar[dict[str, str] | None] = None

    def __init__(self, detail: str | None = None, **extra: Any) -> None:
        self.detail = detail or self.DETAIL
        self.extra = extra
        super().__init__(self.detail)


class NotAuthenticated(DetailedHTTPException):
    STATUS_CODE = status.HTTP_401_UNAUTHORIZED
    CODE: ClassVar[str] = ErrorCode.NOT_AUTHENTICATED
    DETAIL = "Not authenticated"
    HEADERS: ClassVar[dict[str, str] | None] = {"WWW-Authenticate": "Bearer"}


class PermissionDenied(DetailedHTTPException):
    STATUS_CODE = status.HTTP_403_FORBIDDEN
    CODE: ClassVar[str] = ErrorCode.PERMISSION_DENIED
    DETAIL = "Permission denied"


class NotFound(DetailedHTTPException):
    STATUS_CODE = status.HTTP_404_NOT_FOUND
    CODE: ClassVar[str] = ErrorCode.NOT_FOUND
    DETAIL = "Not found"


class Conflict(DetailedHTTPException):
    STATUS_CODE = status.HTTP_409_CONFLICT
    CODE: ClassVar[str] = ErrorCode.CONFLICT
    DETAIL = "Conflict"


async def _detailed_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DetailedHTTPException)
    return JSONResponse(
        status_code=exc.STATUS_CODE,
        content={"code": str(exc.CODE), "detail": exc.detail, **exc.extra},
        headers=exc.HEADERS,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DetailedHTTPException, _detailed_exception_handler)
