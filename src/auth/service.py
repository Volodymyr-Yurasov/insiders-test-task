import logging
import uuid
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis
from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import utils
from src.auth.config import auth_settings
from src.auth.constants import Role
from src.auth.exceptions import InvalidRefreshToken, LastAdmin, UsernameTaken, UserNotFound
from src.auth.models import AppUser, RefreshToken
from src.auth.schemas import TokenPair, UserUpdate
from src.rate_limit import service as rate_limit_service
from src.rate_limit.exceptions import RateLimited

logger = logging.getLogger(__name__)


async def get_by_username(
    db: AsyncSession, username: str, *, for_share: bool = False
) -> AppUser | None:
    stmt = select(AppUser).where(AppUser.username == utils.normalize_username(username))
    if for_share:
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


async def throttle_login(redis: Redis, username: str, client_ip: str) -> None:
    """Sliding-window limits on login attempts, checked BEFORE any argon2 work.

    Per username: caps online guessing against one account (e.g. 'admin') from anywhere.
    Per client IP: caps one client spraying many usernames / burning CPU. Fails open if Redis
    is down (rate_limit.check logs it). Raises 429 with Retry-After.
    """
    window = auth_settings.LOGIN_WINDOW_S
    # IP first: every attempt from a flooding client counts, even ones the username cap
    # would have rejected anyway.
    for key, limit in (
        (f"rl:login:ip:{client_ip}", auth_settings.LOGIN_MAX_ATTEMPTS_PER_IP),
        (
            f"rl:login:user:{utils.normalize_username(username)}",
            auth_settings.LOGIN_MAX_ATTEMPTS_PER_USERNAME,
        ),
    ):
        decision = await rate_limit_service.check(redis, key, limit, window)
        if not decision.allowed:
            raise RateLimited(decision.retry_after_s)


async def authenticate(db: AsyncSession, username: str, password: str) -> AppUser | None:
    """Return the user for valid credentials, else None.

    Every path runs exactly one password verification, so unknown / wrong-password /
    inactive cases are indistinguishable by status, body, and timing.

    The user row is held FOR SHARE until the caller commits (login issues the refresh token
    in the same transaction). That serializes login with update_user's row lock: a password
    change or deactivation either completes first (and this check sees it) or waits for the
    new token to commit (and its revoke-all then sees and revokes it).
    """
    user = await get_by_username(db, username, for_share=True)
    if user is None:
        await utils.burn_password_check(password)
        return None
    if not await utils.verify_password(password, user.password_hash):
        return None
    if not user.is_active:
        return None
    return user


async def create_user(
    db: AsyncSession,
    *,
    username: str,
    password: str,
    role: Role,
    is_active: bool = True,
) -> AppUser | None:
    """Insert a user; return None if the username is taken.

    ON CONFLICT DO NOTHING instead of catching IntegrityError: a unique violation would
    abort the whole Postgres transaction, and a pre-check SELECT would race.
    """
    stmt = (
        insert(AppUser)
        .values(
            username=utils.normalize_username(username),
            password_hash=await utils.hash_password(password),
            role=role,
            is_active=is_active,
        )
        .on_conflict_do_nothing(index_elements=[AppUser.username])
        .returning(AppUser)
    )
    user = (await db.execute(stmt)).scalar_one_or_none()
    await db.commit()
    return user


async def ensure_first_admin(db: AsyncSession) -> None:
    """Bootstrap: create the env-configured admin only if NO admin exists at all.

    Keyed on "any admin exists", not on the username: otherwise renaming the bootstrap
    account (e.g. to retire the default name) would make the next restart re-create an admin
    with the env password. Create-only, and ON CONFLICT keeps concurrent workers safe.
    """
    username = auth_settings.FIRST_ADMIN_USERNAME
    password = auth_settings.FIRST_ADMIN_PASSWORD
    if not username or password is None:
        logger.info("first-admin seed skipped: FIRST_ADMIN_USERNAME/PASSWORD not set")
        return
    if await db.scalar(select(exists().where(AppUser.role == Role.ADMIN))):
        logger.info("first-admin seed skipped: an admin already exists")
        await db.rollback()  # end the read transaction
        return
    created = await create_user(
        db, username=username, password=password.get_secret_value(), role=Role.ADMIN
    )
    if created is None:
        logger.info("first-admin seed: user %r already exists, left unchanged", username)
    else:
        logger.info("first-admin seed: created admin %r", created.username)


async def update_user(db: AsyncSession, user_id: int, changes: UserUpdate) -> AppUser:
    """Apply a partial update, never leaving the system without an active admin.

    Last-admin guard: a change that could remove an active admin first locks ALL active admin
    rows (fixed id order -> no deadlocks), then re-reads the target. Two admins demoting each
    other concurrently serialize here; the second re-evaluates after the first commits and
    is rejected.
    """
    removes_admin = (changes.role is not None and changes.role != Role.ADMIN) or (
        changes.is_active is False
    )
    # argon2 (~50 ms of CPU) BEFORE taking any row lock: never hold locks while hashing.
    new_password_hash = (
        await utils.hash_password(changes.password) if changes.password is not None else None
    )
    active_admin_ids: set[int] = set()
    if removes_admin:
        active_admin_ids = await lock_active_admins(db)

    # FOR NO KEY UPDATE (key_share=True): conflicts with login/refresh's FOR SHARE and with
    # other updates, but NOT with the FOR KEY SHARE every FK insert (operation.performed_by,
    # refresh_token.user_id) takes, so movements and logins never queue behind an admin edit.
    # populate_existing: the request's auth dependency may have cached a pre-lock snapshot.
    user = await db.scalar(
        select(AppUser)
        .where(AppUser.id == user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise UserNotFound()
    if removes_admin and user.id in active_admin_ids and not (active_admin_ids - {user.id}):
        raise LastAdmin()

    if changes.username is not None:
        user.username = changes.username
    if changes.role is not None:
        user.role = changes.role
    if changes.is_active is not None:
        user.is_active = changes.is_active
    if new_password_hash is not None:
        user.password_hash = new_password_hash
    if new_password_hash is not None or changes.is_active is False:
        # Runs after the user-row lock: any login/refresh that got the row first has already
        # committed its new token, so this statement's snapshot includes and revokes it.
        await revoke_all_refresh_tokens(db, user.id)

    try:
        await db.commit()
    except IntegrityError as exc:
        # UPDATE can't use ON CONFLICT; the only unique column we change is username.
        await db.rollback()
        raise UsernameTaken() from exc
    return user


async def lock_active_admins(db: AsyncSession) -> set[int]:
    """Last-admin guard: lock all active admin rows in id order (no deadlock cycles).

    FOR NO KEY UPDATE rather than FOR UPDATE, so FK inserts referencing these admins
    (their movements, their refresh tokens) are not blocked while the guard is held.
    """
    return set(
        await db.scalars(
            select(AppUser.id)
            .where(AppUser.role == Role.ADMIN, AppUser.is_active)
            .order_by(AppUser.id)
            .with_for_update(key_share=True)
        )
    )


# --- Refresh tokens -------------------------------------------------------------------------


async def issue_token_pair(
    db: AsyncSession, user_id: int, *, family_id: uuid.UUID | None = None
) -> TokenPair:
    """Create an access token + a new refresh token (new family on login, same on rotation).

    Commits, so a rotation's revoke + insert land atomically together.
    """
    if family_id is None:  # a fresh login (not a rotation): good moment for housekeeping
        await _prune_expired_refresh_tokens(db)
    raw = utils.new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user_id,
            family_id=family_id or uuid.uuid4(),
            token_hash=utils.hash_refresh_token(raw),
            expires_at=datetime.now(UTC) + timedelta(days=auth_settings.JWT_REFRESH_EXP_DAYS),
        )
    )
    await db.commit()
    return TokenPair(access_token=utils.create_access_token(user_id), refresh_token=raw)


async def rotate_refresh_token(db: AsyncSession, raw: str) -> TokenPair:
    token_hash = utils.hash_refresh_token(raw)

    # Lock the owner's row FOR SHARE *before* touching the token (lock order user -> token,
    # the same as update_user). A concurrent password change / deactivation then either
    # finishes first (its revoke hits this token, the claim below fails) or waits until our
    # new token is committed, so its revoke-all statement sees and revokes it. Without this,
    # its revoke could take its snapshot before our insert and miss the new token.
    owner_id = await db.scalar(
        select(RefreshToken.user_id).where(RefreshToken.token_hash == token_hash)
    )
    if owner_id is None:
        raise InvalidRefreshToken()
    owner = await db.scalar(
        select(AppUser)
        .where(AppUser.id == owner_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )

    # Compare-and-set in ONE statement: of two concurrent refreshes with the same token, the
    # second blocks on the row lock, re-checks `revoked_at IS NULL` after the first commits,
    # matches nothing, and falls through to reuse detection. Never two new pairs.
    claimed = (
        await db.execute(
            update(RefreshToken)
            .where(
                RefreshToken.token_hash == token_hash,
                RefreshToken.revoked_at.is_(None),
                RefreshToken.expires_at > func.now(),
            )
            .values(revoked_at=func.now())
            .returning(RefreshToken.user_id, RefreshToken.family_id)
            .execution_options(synchronize_session=False)
        )
    ).one_or_none()

    if claimed is None:
        known = await db.scalar(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        if known is not None and known.revoked_at is not None:
            # A rotated token came back: someone holds a copy. Kill the whole chain.
            await _revoke_family(db, known.family_id)
            await db.commit()
        raise InvalidRefreshToken()

    if owner is None or not owner.is_active:
        await _revoke_family(db, claimed.family_id)
        await db.commit()
        raise InvalidRefreshToken()

    return await issue_token_pair(db, owner.id, family_id=claimed.family_id)


async def revoke_refresh_family(db: AsyncSession, raw: str) -> None:
    """Logout: revoke the chain the presented token belongs to. Silent if unknown."""
    family_id = await db.scalar(
        select(RefreshToken.family_id).where(
            RefreshToken.token_hash == utils.hash_refresh_token(raw)
        )
    )
    if family_id is not None:
        await _revoke_family(db, family_id)
        await db.commit()


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: int) -> None:
    """Revoke every live refresh token of a user. Does NOT commit (caller's transaction)."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def _revoke_family(db: AsyncSession, family_id: uuid.UUID) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def _prune_expired_refresh_tokens(db: AsyncSession) -> None:
    """Lazy cleanup: delete a bounded batch of long-expired tokens. No scheduler needed.

    Tokens expired (or revoked) within the retention window are kept so a replayed old token
    is still recognized as reuse. SKIP LOCKED + LIMIT: concurrent logins never wait on each
    other, and a large backlog can't make one login slow.
    """
    cutoff = func.now() - timedelta(days=auth_settings.REFRESH_TOKEN_RETENTION_DAYS)
    doomed = (
        select(RefreshToken.id)
        .where(RefreshToken.expires_at < cutoff)
        .limit(auth_settings.REFRESH_TOKEN_PRUNE_BATCH)
        .with_for_update(skip_locked=True)
    )
    await db.execute(
        delete(RefreshToken)
        .where(RefreshToken.id.in_(doomed.scalar_subquery()))
        .execution_options(synchronize_session=False)
    )
