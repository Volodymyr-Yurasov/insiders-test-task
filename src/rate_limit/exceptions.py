from fastapi import status

from src.exceptions import DetailedHTTPException
from src.rate_limit.constants import ErrorCode


class RateLimited(DetailedHTTPException):
    STATUS_CODE = status.HTTP_429_TOO_MANY_REQUESTS
    CODE = ErrorCode.RATE_LIMITED
    DETAIL = "Too many transactions; slow down"

    def __init__(self, retry_after_s: int) -> None:
        super().__init__(headers={"Retry-After": str(retry_after_s)}, retry_after_s=retry_after_s)
