"""Shared pytest configuration for the Agent1 project.

Sets a test JWT_SECRET so that ``gateway.app.config`` can be imported
without the production fail-fast validation raising. This secret is only
used in tests and is never valid in production.
"""
from __future__ import annotations

import os

# Set a test JWT secret before any gateway modules are imported. The
# config module validates at import time (module-level ``settings``) to
# fail fast in production, so tests must provide a valid secret.
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-pytest-only-32chars!")
