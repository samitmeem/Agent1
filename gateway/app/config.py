"""Runtime configuration for the Gateway.

Values are read from environment variables with sensible dev defaults. In
production these are injected by the deploy layer (see `deploy/`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _validate_jwt_secret() -> str:
    """Validate JWT_SECRET at startup to prevent production auth bypass.

    Fails fast if:
    - JWT_SECRET is not set
    - JWT_SECRET is the known insecure default value
    - JWT_SECRET is too short (<32 characters)

    Raises:
        RuntimeError: If JWT_SECRET is invalid or insecure
    """
    secret = os.getenv("JWT_SECRET")

    if not secret:
        raise RuntimeError(
            "CRITICAL: JWT_SECRET environment variable is not set. "
            "Set a secure random secret (min 32 characters) before starting the gateway."
        )

    if secret == "dev-secret-change-me" or secret == "change-me-in-production":
        raise RuntimeError(
            "CRITICAL: JWT_SECRET is set to a known insecure default value. "
            "Generate a secure random secret: python -c 'import secrets; print(secrets.token_urlsafe(48))'"
        )

    if len(secret) < 32:
        raise RuntimeError(
            f"CRITICAL: JWT_SECRET is too short ({len(secret)} chars). "
            "Use at least 32 characters for production security."
        )

    return secret


@dataclass(frozen=True)
class Settings:
    """Gateway configuration loaded from the environment."""

    # PostgreSQL
    database_dsn: str
    pg_min_size: int
    pg_max_size: int
    pg_command_timeout: int

    # Redis
    redis_url: str

    # JWT
    jwt_secret: str
    jwt_algorithm: str
    jwt_expires_minutes: int

    # OAuth
    github_client_id: str
    github_client_secret: str
    gmail_client_id: str
    gmail_client_secret: str

    # ARQ
    arq_redis_url: str

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_dsn=os.getenv(
                "DATABASE_DSN",
                "postgresql://ticket:ticket@localhost:5432/ticketassistant",
            ),
            pg_min_size=int(os.getenv("PG_MIN_SIZE", "5")),
            pg_max_size=int(os.getenv("PG_MAX_SIZE", "25")),
            pg_command_timeout=int(os.getenv("PG_COMMAND_TIMEOUT", "15")),
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            jwt_secret=_validate_jwt_secret(),
            jwt_algorithm=os.getenv("JWT_ALGORITHM", "HS256"),
            jwt_expires_minutes=int(os.getenv("JWT_EXPIRES_MINUTES", "60")),
            github_client_id=os.getenv("GITHUB_CLIENT_ID", ""),
            github_client_secret=os.getenv("GITHUB_CLIENT_SECRET", ""),
            gmail_client_id=os.getenv("GMAIL_CLIENT_ID", ""),
            gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", ""),
            arq_redis_url=os.getenv("ARQ_REDIS_URL", "redis://localhost:6379/1"),
        )


settings = Settings.from_env()
