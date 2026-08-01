"""Initial schema: all 9 core tables.

Creates: users, tickets, messages, events, tool_executions, oauth_connections,
outbox_events, model_attempts, langgraph_checkpoints.

Revision ID: 0001
Revises:
Create Date: 2026-07-29T00:00:00Z
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ---------------------------------------------------------------------------
# Enums — created via raw SQL so asyncpg doesn't double-create them.
# ---------------------------------------------------------------------------
TICKET_STATUS_VALUES = (
    "open",
    "queued",
    "running",
    "waiting_for_user",
    "resolved",
    "failed",
)
AGENT_STATUS_VALUES = (
    "idle",
    "planning",
    "generating",
    "calling_tool",
    "awaiting_authorization",
    "synthesizing_voice",
    "completed",
)
OAUTH_PROVIDER_VALUES = ("github", "gmail")

ticket_status_enum = postgresql.ENUM(*TICKET_STATUS_VALUES, name="ticketstatus", create_type=False)
agent_status_enum = postgresql.ENUM(*AGENT_STATUS_VALUES, name="agentstatus", create_type=False)
oauth_provider_enum = postgresql.ENUM(*OAUTH_PROVIDER_VALUES, name="oauthprovider", create_type=False)


def upgrade() -> None:
    # --- Enums (raw SQL — avoids asyncpg double-create issues) -------------
    op.execute("CREATE TYPE ticketstatus AS ENUM ('open', 'queued', 'running', 'waiting_for_user', 'resolved', 'failed')")
    op.execute("CREATE TYPE agentstatus AS ENUM ('idle', 'planning', 'generating', 'calling_tool', 'awaiting_authorization', 'synthesizing_voice', 'completed')")
    op.execute("CREATE TYPE oauthprovider AS ENUM ('github', 'gmail')")

    # --- 1. users ----------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- 2. tickets --------------------------------------------------------
    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", ticket_status_enum, nullable=False, server_default="open"),
        sa.Column("agent_status", agent_status_enum, nullable=False, server_default="idle"),
        sa.Column("pending_oauth_provider", oauth_provider_enum, nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("selected_model", sa.String(128), nullable=True),
        sa.Column("fallback_reason", sa.Text, nullable=True),
        sa.Column("last_event_sequence", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tickets_user_id", "tickets", ["user_id"])

    # --- 3. messages -------------------------------------------------------
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),  # "user" | "assistant"
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("client_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("usage", postgresql.JSONB, nullable=True),
        sa.Column("audio_retention_metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_messages_ticket_id", "messages", ["ticket_id"])
    # Unique index on (ticket_id, client_message_id) — deduplication constraint.
    op.create_index(
        "ux_messages_ticket_client_msg",
        "messages",
        ["ticket_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )

    # --- 4. events ---------------------------------------------------------
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence", sa.BigInteger, nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_events_ticket_id", "events", ["ticket_id"])
    # Unique index on (ticket_id, sequence) — monotonic event ordering.
    op.create_index("ux_events_ticket_sequence", "events", ["ticket_id", "sequence"], unique=True)

    # --- 5. tool_executions ------------------------------------------------
    op.create_table(
        "tool_executions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("arguments", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("oauth_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("dry_run", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),  # pending|running|succeeded|failed
        sa.Column("result", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_tool_executions_ticket_id", "tool_executions", ["ticket_id"])
    op.create_index("ux_tool_executions_idem", "tool_executions", ["idempotency_key"], unique=True)

    # --- 6. oauth_connections ----------------------------------------------
    op.create_table(
        "oauth_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", oauth_provider_enum, nullable=False),
        sa.Column("provider_user_id", sa.String(255), nullable=True),
        sa.Column("provider_user_email", sa.String(320), nullable=True),
        # Encrypted tokens — bytea, never plaintext.
        sa.Column("refresh_token_encrypted", postgresql.BYTEA, nullable=True),
        sa.Column("access_token_encrypted", postgresql.BYTEA, nullable=True),
        sa.Column("access_token_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("scopes", sa.ARRAY(sa.String(128)), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_oauth_connections_user_id", "oauth_connections", ["user_id"])
    op.create_index("ux_oauth_connections_user_provider", "oauth_connections", ["user_id", "provider"], unique=True)

    # --- 7. outbox_events --------------------------------------------------
    op.create_table(
        "outbox_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dispatched", sa.Boolean, nullable=False, server_default=sa.text("false")),
        # Nullable — set by the sweeper when the ARQ job is enqueued.
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_outbox_events_dispatched", "outbox_events", ["dispatched"])
    op.create_index("ix_outbox_events_ticket_id", "outbox_events", ["ticket_id"])

    # --- 8. model_attempts -------------------------------------------------
    op.create_table(
        "model_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("error_class", sa.String(128), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("token_usage", postgresql.JSONB, nullable=True),
        sa.Column("succeeded", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_model_attempts_ticket_id", "model_attempts", ["ticket_id"])

    # --- 9. langgraph_checkpoints -----------------------------------------
    op.create_table(
        "langgraph_checkpoints",
        sa.Column("ticket_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("node_name", sa.String(64), nullable=False),
        sa.Column("checkpoint_data", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("ticket_id", "run_id", "node_name", name="pk_langgraph_checkpoints"),
    )


def downgrade() -> None:
    op.drop_table("langgraph_checkpoints")
    op.drop_table("model_attempts")
    op.drop_table("outbox_events")
    op.drop_table("oauth_connections")
    op.drop_table("tool_executions")
    op.drop_table("events")
    op.drop_table("messages")
    op.drop_table("tickets")
    op.drop_table("users")

    op.execute("DROP TYPE oauthprovider")
    op.execute("DROP TYPE agentstatus")
    op.execute("DROP TYPE ticketstatus")
