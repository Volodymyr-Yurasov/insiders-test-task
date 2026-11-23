from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from redis.asyncio import Redis
from sqlalchemy import select

from src.auth import service as auth_service
from src.auth.dependencies import CurrentUser, admin_user, valid_user_id
from src.auth.exceptions import InvalidCredentials, UsernameTaken
from src.auth.models import AppUser
from src.auth.schemas import RefreshRequest, TokenPair, UserCreate, UserRead, UserUpdate
from src.database import DbSession
from src.pagination import Page, Pagination, paginate
from src.redis import get_redis
from src.schemas import IdPath

router = APIRouter(prefix="/auth", tags=["auth"])

# Admin-only by default: every route added here inherits the role check.
users_router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(admin_user)])


@router.post(
    "/token",
    response_model=TokenPair,
    summary="Log in with username and password (OAuth2 password form)",
    responses={
        401: {"description": "Invalid credentials"},
        429: {"description": "Too many login attempts for this username or client"},
    },
)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    request: Request,
    db: DbSession,
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenPair:
    # The direct peer address; X-Forwarded-For is deliberately not trusted (spoofable)
    # unless a proxy is configured to set it, e.g. uvicorn --proxy-headers.
    client_ip = request.client.host if request.client else "unknown"
    await auth_service.throttle_login(redis, form.username, client_ip)
    user = await auth_service.authenticate(db, form.username, form.password)
    if user is None:
        raise InvalidCredentials()
    return await auth_service.issue_token_pair(db, user.id)


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate a refresh token into a new access + refresh pair",
    description=(
        "The presented refresh token is revoked. Presenting an already-rotated token again "
        "is treated as theft and revokes every token of that login chain."
    ),
    responses={401: {"description": "Invalid, expired, revoked or reused refresh token"}},
)
async def refresh(body: RefreshRequest, db: DbSession) -> TokenPair:
    return await auth_service.rotate_refresh_token(db, body.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the refresh-token chain (always 204)",
)
async def logout(body: RefreshRequest, db: DbSession) -> None:
    await auth_service.revoke_refresh_family(db, body.refresh_token)


@router.get("/me", response_model=UserRead, summary="Current user")
async def me(user: CurrentUser) -> AppUser:
    return user


@users_router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a user (admin)",
    responses={409: {"description": "Username already taken"}},
)
async def create_user(body: UserCreate, db: DbSession) -> AppUser:
    user = await auth_service.create_user(
        db, username=body.username, password=body.password, role=body.role
    )
    if user is None:
        raise UsernameTaken()
    return user


@users_router.get("", response_model=Page[UserRead], summary="List users (admin)")
async def list_users(db: DbSession, page: Pagination) -> dict[str, Any]:
    return await paginate(db, select(AppUser).order_by(AppUser.id), page)


@users_router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="Get a user (admin)",
    responses={404: {"description": "User not found"}},
)
async def get_user(user: Annotated[AppUser, Depends(valid_user_id)]) -> AppUser:
    return user


@users_router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="Edit, deactivate or reactivate a user (admin)",
    description=(
        "Deactivation or a password change revokes all of the user's refresh tokens. "
        "The last active admin can't be demoted or deactivated."
    ),
    responses={
        404: {"description": "User not found"},
        409: {"description": "Username taken, or last active admin"},
    },
)
async def update_user(user_id: IdPath, body: UserUpdate, db: DbSession) -> AppUser:
    # Not using valid_user_id: the service must load the target *after* taking its locks.
    return await auth_service.update_user(db, user_id, body)
