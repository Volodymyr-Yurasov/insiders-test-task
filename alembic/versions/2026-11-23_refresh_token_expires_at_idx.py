"""refresh_token_expires_at_idx

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-23 12:35:25.082679

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_index(
        op.f("refresh_token_expires_at_idx"), "refresh_token", ["expires_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("refresh_token_expires_at_idx"), table_name="refresh_token")
