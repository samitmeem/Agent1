"""Ticket REST endpoints.

Implements `POST /api/tickets`, `GET /api/tickets`, and
`GET /api/tickets/{ticket_id}` per `AgenticProject.md` §1 step 2 and §4
(Gateway task breakdown). Ticket-level authorization enforces owner-only
access.
"""
from __future__ import annotations

from uuid import UUID, uuid4

from asyncpg import Pool
from fastapi import APIRouter, Depends, HTTPException, Request, status

from gateway.app.auth import CurrentUser

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


def _pg(request: Request) -> Pool:
    """Return the shared asyncpg pool from app state."""
    return request.app.state.pg


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_ticket(
    request: Request,
    user: CurrentUser,
) -> dict:
    """Create a new ticket with `status='open'`."""
    pool: Pool = _pg(request)
    ticket_id = uuid4()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tickets (id, user_id, status, agent_status)
            VALUES ($1, $2, 'open', 'idle')
            RETURNING id, user_id, status, agent_status, created_at, updated_at
            """,
            ticket_id,
            user,
        )
    return dict(row)


@router.get("")
async def list_tickets(
    request: Request,
    user: CurrentUser,
) -> list[dict]:
    """Return only tickets owned by the authenticated user."""
    pool: Pool = _pg(request)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, user_id, status, agent_status, created_at, updated_at
            FROM tickets
            WHERE user_id = $1
            ORDER BY created_at DESC
            """,
            user,
        )
    return [dict(r) for r in rows]


@router.get("/{ticket_id}")
async def get_ticket(
    request: Request,
    ticket_id: UUID,
    user: CurrentUser,
) -> dict:
    """Return a single ticket. Returns 403 for non-owner."""
    pool: Pool = _pg(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, user_id, status, agent_status, created_at, updated_at
            FROM tickets
            WHERE id = $1
            """,
            ticket_id,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ticket not found",
            )
        if row["user_id"] != user:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to access this ticket",
            )
    return dict(row)
