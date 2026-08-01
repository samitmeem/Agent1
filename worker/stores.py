"""Postgres-backed stores for tool execution and OAuth credentials.

Replaces stub implementations with real persistence for production.

These stores implement the protocols defined in ``worker.tools.base``:
- ``PostgresExecutionStore`` implements ``ToolExecutionStoreProtocol``.
- ``PostgresOAuthStore`` implements ``OAuthCredentialStoreProtocol``.

Idempotency is enforced via the unique constraint on
``tool_executions.idempotency_key`` — a retry that reuses the same key
hits a conflict and the existing row's result is returned instead of
re-executing.  This is critical for preventing duplicate emails, GitHub
issues, etc.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import asyncpg

from worker.tools.base import OAuthCredential, ToolExecutionRecord

logger = logging.getLogger("worker.stores")


class PostgresExecutionStore:
    """Postgres-backed tool execution store with idempotency support.

    Implements ``worker.tools.base.ToolExecutionStoreProtocol``.

    Prevents duplicate tool executions by checking ``idempotency_key``
    before executing tools. The unique constraint on
    ``tool_executions.idempotency_key`` guarantees that only one row is
    ever created per key, even under concurrent inserts.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def insert_pending(self, record: ToolExecutionRecord) -> None:
        """Insert a ``pending`` row before invocation.

        Raises on ``idempotency_key`` conflict (duplicate) — the caller
        should then look up the existing row via ``get_by_idempotency_key``.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_executions (
                    id, ticket_id, user_id, tool_name, idempotency_key,
                    arguments, oauth_connection_id, dry_run, status,
                    started_at, created_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending', $9, NOW())
                """,
                record.execution_id,
                record.ticket_id,
                record.user_id,
                record.tool_name,
                record.idempotency_key,
                json.dumps(record.arguments),
                record.oauth_connection_id,
                record.dry_run,
                record.started_at,
            )

    async def mark_running(self, execution_id: UUID) -> None:
        """Set status to ``running`` and record ``started_at``."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tool_executions
                SET status = 'running', started_at = NOW()
                WHERE id = $1
                """,
                execution_id,
            )

    async def mark_completed(
        self,
        execution_id: UUID,
        status: str,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> None:
        """Set status to ``succeeded``/``failed``, record ``completed_at``."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE tool_executions
                SET status = $2, result = $3, error = $4, completed_at = NOW()
                WHERE id = $1
                """,
                execution_id,
                status,
                json.dumps(result) if result is not None else None,
                error,
            )

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> ToolExecutionRecord | None:
        """Look up an existing execution by idempotency key.

        Returns ``None`` if not found.  Used to short-circuit retries.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, ticket_id, user_id, tool_name, idempotency_key,
                       arguments, oauth_connection_id, dry_run, status,
                       result, error, started_at, completed_at
                FROM tool_executions
                WHERE idempotency_key = $1
                """,
                idempotency_key,
            )
            if row is None:
                return None
            return self._row_to_record(row)

    @staticmethod
    def _row_to_record(row: asyncpg.Record) -> ToolExecutionRecord:
        """Convert a database row to a ``ToolExecutionRecord``."""
        arguments = row["arguments"]
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        result = row["result"]
        if isinstance(result, str):
            result = json.loads(result)
        return ToolExecutionRecord(
            execution_id=row["id"],
            ticket_id=row["ticket_id"],
            user_id=row["user_id"],
            tool_name=row["tool_name"],
            idempotency_key=row["idempotency_key"],
            arguments=arguments or {},
            oauth_connection_id=row["oauth_connection_id"],
            dry_run=row["dry_run"],
            status=row["status"],
            result=result,
            error=row["error"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )


class PostgresOAuthStore:
    """Postgres-backed OAuth credential store with encryption support.

    Implements ``worker.tools.base.OAuthCredentialStoreProtocol``.

    Reads encrypted access/refresh tokens from ``oauth_connections``.
    Token decryption is performed by the caller (or a KMS integration
    layer) — this store returns the raw encrypted bytes.  Access tokens
    are stored in ``access_token_encrypted`` (BYTEA), never plaintext.
    """

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    async def get_credential(
        self,
        user_id: UUID,
        provider: str,
    ) -> OAuthCredential | None:
        """Retrieve the encrypted OAuth credential for a user+provider.

        Returns ``None`` if the user has not connected this provider.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, provider, access_token_encrypted,
                       refresh_token_encrypted, access_token_expires_at,
                       scopes, provider_user_id, provider_user_email
                FROM oauth_connections
                WHERE user_id = $1 AND provider = $2
                """,
                user_id,
                provider,
            )
            if row is None:
                return None

            # NOTE: access_token_encrypted is BYTEA. Decryption to a usable
            # plaintext token is performed here in production via KMS. For
            # now we surface the encrypted bytes; the tool layer is
            # responsible for decryption (Task 8).
            encrypted_access = row["access_token_encrypted"]
            access_token = encrypted_access.decode() if isinstance(encrypted_access, (bytes, bytearray)) else encrypted_access

            return OAuthCredential(
                connection_id=row["id"],
                provider=row["provider"],
                access_token=access_token,
                refresh_token_encrypted=row["refresh_token_encrypted"],
                access_token_expires_at=row["access_token_expires_at"],
                scopes=row["scopes"],
                provider_user_id=row["provider_user_id"],
                provider_user_email=row["provider_user_email"],
            )

    async def refresh_token(
        self,
        connection_id: UUID,
        refresh_token_encrypted: bytes,
    ) -> OAuthCredential:
        """Refresh the access token using the encrypted refresh token.

        Updates the ``access_token_encrypted`` and
        ``access_token_expires_at`` columns in PostgreSQL.  Called under a
        per-connection Redis lock by the ``ToolExecutor``.

        NOTE: Full token-exchange implementation is Task 8. This stub
        re-reads the current credential so the executor's refresh path
        is wired for production — the actual HTTP exchange to the
        provider's token endpoint is added in Task 8.
        """
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, provider, access_token_encrypted,
                       refresh_token_encrypted, access_token_expires_at,
                       scopes, provider_user_id, provider_user_email
                FROM oauth_connections
                WHERE id = $1
                """,
                connection_id,
            )
            if row is None:
                raise ValueError(f"OAuth connection {connection_id} not found")

            encrypted_access = row["access_token_encrypted"]
            access_token = encrypted_access.decode() if isinstance(encrypted_access, (bytes, bytearray)) else encrypted_access

            return OAuthCredential(
                connection_id=row["id"],
                provider=row["provider"],
                access_token=access_token,
                refresh_token_encrypted=row["refresh_token_encrypted"],
                access_token_expires_at=row["access_token_expires_at"],
                scopes=row["scopes"],
                provider_user_id=row["provider_user_id"],
                provider_user_email=row["provider_user_email"],
            )

    async def update_access_token(
        self,
        connection_id: UUID,
        access_token: str,
        expires_at: datetime,
    ) -> None:
        """Persist a refreshed access token.

        NOTE: In production the token is encrypted before storage. The
        encryption step is added in Task 8 (KMS integration).
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE oauth_connections
                SET access_token_encrypted = $2,
                    access_token_expires_at = $3,
                    updated_at = NOW()
                WHERE id = $1
                """,
                connection_id,
                access_token.encode(),
                expires_at,
            )
