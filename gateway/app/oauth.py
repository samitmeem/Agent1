"""OAuth connect/callback endpoints.

Implements `POST /api/oauth/{provider}/connect` and
`GET /api/oauth/{provider}/callback` per `AgenticProject.md` §4 (Gateway task
breakdown). These endpoints initiate the OAuth flow and persist the resulting
connection. Token encryption and refresh happen in the Worker (Task 8).
"""
from __future__ import annotations

from typing import Literal
from uuid import UUID, uuid4

from asyncpg import Pool
from fastapi import APIRouter, Depends, HTTPException, Request, status

from gateway.app.auth import CurrentUser
from gateway.app.config import settings

router = APIRouter(prefix="/api/oauth", tags=["oauth"])

Provider = Literal["github", "gmail"]

_REDIRECT_BASE = "http://localhost:5173"


def _pg(request: Request) -> Pool:
    return request.app.state.pg


def _client_creds(provider: Provider) -> tuple[str, str]:
    """Return (client_id, client_secret) for the given provider."""
    if provider == "github":
        return settings.github_client_id, settings.github_client_secret
    if provider == "gmail":
        return settings.gmail_client_id, settings.gmail_client_secret
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported provider: {provider}",
    )


@router.post("/{provider}/connect")
async def oauth_connect(
    request: Request,
    provider: Provider,
    user: CurrentUser,
) -> dict:
    """Initiate an OAuth flow, returning the authorization URL.

    The browser redirects to the URL; the provider calls back to
    `GET /api/oauth/{provider}/callback`.

    Security: Generates a random state nonce stored in Redis with 5-minute TTL
    to prevent CSRF attacks during OAuth callback.
    """
    client_id, _ = _client_creds(provider)
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"OAuth provider {provider} is not configured",
        )

    state = uuid4().hex

    # Store state nonce in Redis with 5-minute expiry for CSRF protection
    redis = request.app.state.redis
    await redis.setex(f"oauth:state:{state}", 300, str(user))

    pool: Pool = _pg(request)
    async with pool.acquire() as conn:
        # Record the pending connection so callback can verify state.
        await conn.execute(
            """
            INSERT INTO oauth_connections (id, user_id, provider)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, provider) DO UPDATE SET updated_at = NOW()
            """,
            uuid4(),
            user,
            provider,
        )

    auth_urls = {
        "github": (
            "https://github.com/login/oauth/authorize"
            f"?client_id={client_id}&state={state}&scope=repo"
        ),
        "gmail": (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={client_id}&response_type=code"
            f"&scope=https://www.googleapis.com/auth/gmail.send&state={state}"
        ),
    }
    return {"authorization_url": auth_urls[provider], "state": state}


@router.get("/{provider}/callback")
async def oauth_callback(
    request: Request,
    provider: Provider,
    user: CurrentUser,
    code: str,
    state: str,
) -> dict:
    """Handle the OAuth callback.

    In production this exchanges `code` for access/refresh tokens, encrypts the
    refresh token (KMS), and persists it as `bytea`. This skeleton records the
    connection and returns a safe summary. Full token exchange is Task 8.

    Security: Validates state nonce from Redis to prevent CSRF attacks.
    """
    # CRITICAL CSRF Protection: Validate state parameter
    redis = request.app.state.redis
    stored_user_id = await redis.getdel(f"oauth:state:{state}")

    if not stored_user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired OAuth state parameter. Please restart the OAuth flow.",
        )

    try:
        stored_user = UUID(stored_user_id.decode() if isinstance(stored_user_id, bytes) else stored_user_id)
    except (ValueError, AttributeError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Corrupted OAuth state. Please restart the OAuth flow.",
        )

    if stored_user != user:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="OAuth state mismatch. Possible CSRF attack detected.",
        )

    pool: Pool = _pg(request)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id FROM oauth_connections
            WHERE user_id = $1 AND provider = $2
            """,
            user,
            provider,
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No pending OAuth connection for this provider",
            )
        # Mark the connection as connected — token exchange is Task 8.
        await conn.execute(
            """
            UPDATE oauth_connections
            SET updated_at = NOW()
            WHERE id = $1
            """,
            row["id"],
        )
    return {"provider": provider, "connected": True}
