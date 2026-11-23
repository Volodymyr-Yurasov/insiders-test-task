"""Auth as chained dependencies: token -> user id -> active DB user -> admin.

FastAPI caches each dependency per request, so routes and other dependencies can all depend
on `current_user` and the token is decoded and the user loaded only once.
"""

from typing import Annotated

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from src.auth import utils as auth_utils
from src.auth.constants import Role
from src.auth.exceptions import InvalidToken, UserNotFound
from src.auth.models import AppUser
from src.database import DbSession
from src.exceptions import NotAuthenticated, PermissionDenied

# auto_error=False: a missing token is reported through our own 401 format.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)


async def parse_jwt_data(token: Annotated[str | None, Depends(oauth2_scheme)]) -> int:
    if not token:
        raise NotAuthenticated()
    return auth_utils.decode_access_token(token)


async def current_user(user_id: Annotated[int, Depends(parse_jwt_data)], db: DbSession) -> AppUser:
    # Re-read on every request: deactivation and role changes apply immediately.
    user = await db.get(AppUser, user_id)
    if user is None or not user.is_active:
        raise InvalidToken()
    return user


CurrentUser = Annotated[AppUser, Depends(current_user)]


async def admin_user(user: CurrentUser) -> AppUser:
    if user.role != Role.ADMIN:
        raise PermissionDenied()
    return user


AdminUser = Annotated[AppUser, Depends(admin_user)]


async def valid_user_id(user_id: int, db: DbSession) -> AppUser:
    user = await db.get(AppUser, user_id)
    if user is None:
        raise UserNotFound()
    return user
