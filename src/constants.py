from enum import StrEnum


class Environment(StrEnum):
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def shows_docs(self) -> bool:
        return self in (Environment.LOCAL, Environment.STAGING)


class ErrorCode(StrEnum):
    """Generic error codes; modules define their own specific codes in their constants."""

    INTERNAL_ERROR = "internal_error"
    NOT_AUTHENTICATED = "not_authenticated"
    PERMISSION_DENIED = "permission_denied"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
