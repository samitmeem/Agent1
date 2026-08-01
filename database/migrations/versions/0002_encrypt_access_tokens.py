"""Encrypt OAuth access tokens

Migrates oauth_connections.access_token from plaintext TEXT to encrypted BYTEA.
All existing access tokens are invalidated (users must re-authorize).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-01T00:00:00Z
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the plaintext access_token column
    op.drop_column("oauth_connections", "access_token")

    # Add encrypted access_token_encrypted column
    op.add_column(
        "oauth_connections",
        sa.Column("access_token_encrypted", postgresql.BYTEA, nullable=True),
    )

    # Invalidate all access token expiry timestamps (force re-auth)
    op.execute("UPDATE oauth_connections SET access_token_expires_at = NOW() - INTERVAL '1 day'")


def downgrade() -> None:
    # Restore plaintext column (data loss - all tokens must be re-acquired)
    op.drop_column("oauth_connections", "access_token_encrypted")
    op.add_column(
        "oauth_connections",
        sa.Column("access_token", sa.Text, nullable=True),
    )
