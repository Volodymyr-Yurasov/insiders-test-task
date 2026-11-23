from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    STOREKEEPER = "storekeeper"


class ErrorCode(StrEnum):
    INVALID_CREDENTIALS = "invalid_credentials"
    INVALID_TOKEN = "invalid_token"
    INVALID_REFRESH_TOKEN = "invalid_refresh_token"
    USERNAME_TAKEN = "username_taken"
    USER_NOT_FOUND = "user_not_found"
    LAST_ADMIN = "last_admin"
