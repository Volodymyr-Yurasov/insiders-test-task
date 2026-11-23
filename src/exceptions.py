"""Global exception hierarchy.

Module exceptions subclass these and override CODE/DETAIL. Every one is rendered by a single
handler as `{"code": ..., "detail": ..., **extra}` so clients get one error shape.
"""

import logging
from collections.abc import Mapping
from typing import Any, ClassVar

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.constants import ErrorCode

logger = logging.getLogger(__name__)


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


class ServiceBusy(DetailedHTTPException):
    # Lock wait timed out or a deadlock was broken: safe to retry shortly.
    STATUS_CODE = status.HTTP_503_SERVICE_UNAVAILABLE
    CODE: ClassVar[str] = ErrorCode.SERVICE_BUSY
    DETAIL = "The resource is busy, please retry"
    HEADERS: ClassVar[dict[str, str] | None] = {"Retry-After": "1"}


def _error(
    status_code: int,
    code: str,
    detail: str,
    *,
    headers: Mapping[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"code": code, "detail": detail, **extra},
        headers=dict(headers) if headers else None,
    )


async def _detailed_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DetailedHTTPException)
    return _error(exc.STATUS_CODE, exc.CODE, exc.detail, headers=exc.HEADERS, **exc.extra)


async def _validation_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Only loc/msg/type: Pydantic's `input` would echo submitted values (e.g. passwords)
    # back to the client, and `ctx` may hold non-JSON-serializable exception objects.
    errors = [{"loc": e["loc"], "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return _error(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        ErrorCode.VALIDATION_ERROR,
        "Request validation failed",
        errors=errors,
    )


_CODE_BY_STATUS = {
    status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
}


async def _http_exception_handler(_: Request, exc: Exception) -> Response:
    # Framework-raised errors: unknown route, wrong method, FastAPI internals.
    assert isinstance(exc, StarletteHTTPException)
    code = _CODE_BY_STATUS.get(exc.status_code, ErrorCode.HTTP_ERROR)
    return _error(exc.status_code, code, str(exc.detail), headers=exc.headers)


async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    # Logged with traceback; the client gets nothing internal.
    logger.error("unhandled exception", exc_info=exc)
    return _error(
        status.HTTP_500_INTERNAL_SERVER_ERROR, ErrorCode.INTERNAL_ERROR, "Internal server error"
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DetailedHTTPException, _detailed_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
