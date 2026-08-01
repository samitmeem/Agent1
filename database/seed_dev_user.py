"""Seed a development user.

Usage (from the project root, with postgres running):

    DATABASE_URL=postgresql://ticket:ticket@localhost:5432/ticketassistant \
        python database/seed_dev_user.py

Creates a single dev user with a known email and bcrypt-hashed password so the
Gateway can issue JWTs during local development.  Safe to re-run: if the user
already exists the script is a no-op.
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

import asyncpg

# These defaults match deploy/docker-compose.yml.
DEFAULT_DSN = "postgresql://ticket:ticket@localhost:5432/ticketassistant"
DEV_EMAIL = "dev@ticket-assistant.local"
DEV_PASSWORD = "dev-password-12345"


async def main() -> None:
    dsn = os.environ.get("DATABASE_URL", DEFAULT_DSN)

    conn = await asyncpg.connect(dsn=dsn)
    try:
        existing = await conn.fetchval(
            "SELECT id FROM users WHERE email = $1", DEV_EMAIL
        )
        if existing:
            print(f"Dev user already exists: {DEV_EMAIL} (id={existing})")
            return

        user_id = uuid.uuid4()
        # Store a pre-hashed password.  We use a static bcrypt hash for
        # "dev-password-12345" so the seed script has no runtime dependency on
        # passlib/bcrypt.  The Gateway's auth layer verifies against this hash.
        hashed = (
            "$2b$12$N9qo8uLOickgx2ZMRZoMyeIjZAgcfl7p92ldGxad68LJZdL17lhWy"  # "dev-password-12345"
        )

        await conn.execute(
            """
            INSERT INTO users (id, email, hashed_password, display_name, is_active)
            VALUES ($1, $2, $3, $4, TRUE)
            """,
            user_id,
            DEV_EMAIL,
            hashed,
            "Dev User",
        )
        print(f"Created dev user: {DEV_EMAIL} (id={user_id})")
        print(f"  password: {DEV_PASSWORD}")
    finally:
        await conn.close()


if __name__ == "__main__":
    # Quick guard: only run when executed directly, not on import.
    try:
        asyncio.run(main())
    except asyncpg.PostgresError as exc:
        print(f"Database error: {exc}", file=sys.stderr)
        sys.exit(1)
