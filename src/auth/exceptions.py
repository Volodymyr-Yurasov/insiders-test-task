from src.auth.constants import ErrorCode
from src.exceptions import Conflict, NotAuthenticated, NotFound


class InvalidCredentials(NotAuthenticated):
    # One message for unknown user / wrong password / inactive user: no account enumeration.
    CODE = ErrorCode.INVALID_CREDENTIALS
    DETAIL = "Incorrect username or password"


class InvalidToken(NotAuthenticated):
    # Bad signature, expired, malformed, or the user is gone/deactivated: same answer.
    CODE = ErrorCode.INVALID_TOKEN
    DETAIL = "Could not validate credentials"


class InvalidRefreshToken(NotAuthenticated):
    # Unknown, expired, revoked, reused, or the user is deactivated: same answer.
    CODE = ErrorCode.INVALID_REFRESH_TOKEN
    DETAIL = "Invalid refresh token"


class UsernameTaken(Conflict):
    CODE = ErrorCode.USERNAME_TAKEN
    DETAIL = "Username is already taken"


class LastAdmin(Conflict):
    CODE = ErrorCode.LAST_ADMIN
    DETAIL = "Cannot demote or deactivate the last active admin"


class UserNotFound(NotFound):
    CODE = ErrorCode.USER_NOT_FOUND
    DETAIL = "User not found"
