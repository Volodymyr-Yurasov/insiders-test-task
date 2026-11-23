from typing import ClassVar

from fastapi import status

from src.exceptions import Conflict, DetailedHTTPException
from src.idempotency.constants import ErrorCode


class IdempotencyKeyReused(DetailedHTTPException):
    # Same key, different request: a client bug, never silently replay the other result.
    STATUS_CODE = status.HTTP_422_UNPROCESSABLE_CONTENT
    CODE = ErrorCode.IDEMPOTENCY_KEY_REUSED
    DETAIL = "Idempotency-Key was already used with a different request"


class IdempotencyInProgress(Conflict):
    CODE = ErrorCode.IDEMPOTENCY_IN_PROGRESS
    DETAIL = "A request with this Idempotency-Key is still being processed; retry shortly"
    HEADERS: ClassVar[dict[str, str] | None] = {"Retry-After": "1"}
