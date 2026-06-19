"""dashboard_credentials — first-run admin login persisted in the DB

The first-run /setup screen creates ONE admin credential (PBKDF2 password hash
+ HMAC session secret) and stores it here, so every later run shows the normal
login instead of asking again. Single-row by construction: a UNIQUE constraint
on the constant ``singleton`` column blocks a second INSERT at the DB layer
(defence-in-depth on top of the repo's already-configured guard). No secret is
ever in the repo or .env — once set, this table is the source of truth.

Revision ID: a7e3c1b9f204
Revises: c3d8f1a6b240
Create Date: 2026-06-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7e3c1b9f204"
down_revision: str | Sequence[str] | None = "c3d8f1a6b240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dashboard_credentials",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("singleton", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("session_secret", sa.String(256), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("singleton", name="uq_dashboard_credentials_singleton"),
    )


def downgrade() -> None:
    op.drop_table("dashboard_credentials")
