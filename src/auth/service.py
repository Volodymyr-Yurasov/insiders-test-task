import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth import utils
from src.auth.config import auth_settings
from src.auth.constants import Role
from src.auth.exceptions import InvalidRefreshToken, LastAdmin, UsernameTaken, UserNotFound
from src.auth.models import AppUser, RefreshToken
from src.auth.schemas import TokenPair, UserUpdate

logger = logging.getLogger(__name__)


async def get_by_username(db: AsyncSession, username: str) -> AppUser | None:
    result = await db.execute(
        select(AppUser).where(AppUser.username == utils.normalize_username(username))
    )
    return result.scalar_one_or_none()


async def authenticate(db: AsyncSession, username: str, password: str) -> AppUser | None:
    """Return the user for valid credentials, else None.

    Every path runs exactly one password verification, so unknown / wrong-password /
    inactive cases are indistinguishable by status, body, and timing.
    """
    user = await get_by_username(db, username)
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
    """Create the env-configured first admin if missing. Create-only: never overwrites an
    existing user's password or role, and ON CONFLICT makes concurrent workers safe."""
    username = auth_settings.FIRST_ADMIN_USERNAME
    password = auth_settings.FIRST_ADMIN_PASSWORD
    if not username or password is None:
        logger.info("first-admin seed skipped: FIRST_ADMIN_USERNAME/PASSWORD not set")
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
    active_admin_ids: set[int] = set()
    if removes_admin:
        active_admin_ids = set(
            await db.scalars(
                select(AppUser.id)
                .where(AppUser.role == Role.ADMIN, AppUser.is_active)
                .order_by(AppUser.id)
                .with_for_update()
            )
        )

    # populate_existing: the request's auth dependency may have cached a pre-lock snapshot.
    user = await db.scalar(
        select(AppUser)
        .where(AppUser.id == user_id)
        .with_for_update()
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
    if changes.password is not None:
        user.password_hash = await utils.hash_password(changes.password)
    if changes.password is not None or changes.is_active is False:
        await revoke_all_refresh_tokens(db, user.id)

    try:
        await db.commit()
    except IntegrityError as exc:
        # UPDATE can't use ON CONFLICT; the only unique column we change is username.
        await db.rollback()
        raise UsernameTaken() from exc
    return user


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

    user = await db.get(AppUser, claimed.user_id)
    if user is None or not user.is_active:
        await _revoke_family(db, claimed.family_id)
        await db.commit()
        raise InvalidRefreshToken()

    return await issue_token_pair(db, user.id, family_id=claimed.family_id)


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
