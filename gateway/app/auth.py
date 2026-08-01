"""JWT authentication and ticket-level authorization.

JWT is sent via a cookie or the `Sec-WebSocket-Protocol` header — never via
query parameters (see `AgenticProject.md` §1 step 1). This module provides
the dependency that validates the token and populates
`request.state.user_id`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from jose import JWTError, jwt

from gateway.app.config import settings


def _extract_token(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> str | None:
    """Extract a JWT from a cookie, Authorization header, or subprotocol."""
    # 1. Cookie (preferred for browser clients).
    token = request.cookies.get("access_token")
    if token:
        return token

    # 2. Authorization: Bearer <token>
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1]

    # 3. Sec-WebSocket-Protocol — the token is sent as the first subprotocol
    #    value when opening a WebSocket (browsers can't set custom WS headers).
    if request.headers.get("sec-websocket-protocol"):
        # The first token in the subprotocol list is the JWT.
        proto = request.headers["sec-websocket-protocol"].split(",")[0].strip()
        if proto:
            return proto

    return None


def create_access_token(user_id: UUID) -> str:
    """Issue a JWT for `user_id`. Used by a login endpoint (not in this task)."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expires_minutes),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


async def get_current_user(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> UUID:
    """Validate the JWT and populate `request.state.user_id`.

    Raises 401 on missing/invalid tokens. The resolved user id is returned
    for direct use in route handlers and also stored on request state.
    """
    token = _extract_token(request, authorization)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        user_id_str = payload.get("sub")
        if not user_id_str:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token: missing subject",
            )
        user_id = UUID(user_id_str)
    except (JWTError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    request.state.user_id = user_id
    return user_id


CurrentUser = Annotated[UUID, Depends(get_current_user)]
"""FastAPI dependency yielding the authenticated user's UUID."""
