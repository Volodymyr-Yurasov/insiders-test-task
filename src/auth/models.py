import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Identity, String, true
from sqlalchemy.orm import Mapped, mapped_column

from src.auth.constants import Role
from src.database import Base, TimestampMixin


class AppUser(TimestampMixin, Base):
    # Not "user": that's a reserved word in Postgres.
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # VARCHAR + CHECK instead of a native PG enum: adding a role needs no ALTER TYPE.
    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            name="role",
            native_enum=False,
            create_constraint=True,
            length=20,
            values_callable=lambda roles: [r.value for r in roles],
        )
    )
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


class RefreshToken(TimestampMixin, Base):
    """Opaque refresh token. Only its sha256 is stored: a DB leak yields no usable tokens.

    All tokens produced by rotating one login share a family_id, so reuse of a rotated
    (revoked) token can revoke the whole chain.
    """

    __tablename__ = "refresh_token"

    id: Mapped[int] = mapped_column(Identity(), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"), index=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
