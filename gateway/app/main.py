"""Ticket Assistant Gateway — FastAPI application.

Wires together the lifespan (asyncpg + Redis), JWT auth, ticket REST
endpoints, and OAuth endpoints. Adds request IDs, correlation IDs, and
structured logging middleware. The WebSocket endpoint is Task 5.
"""
from __future__ import annotations

import logging
import uuid as _uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from gateway.app.lifespan import lifespan
from gateway.app.oauth import router as oauth_router
from gateway.app.tickets import router as tickets_router
from gateway.app.websocket import router as websocket_router

logger = logging.getLogger("gateway")


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request id and correlation id to every request/response.

    The `X-Request-Id` header is generated if not provided. `X-Correlation-Id`
    is propagated if present, otherwise set to the request id.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        request_id = request.headers.get("x-request-id") or f"req_{_uuid.uuid4().hex}"
        correlation_id = (
            request.headers.get("x-correlation-id") or request_id
        )
        request.state.request_id = request_id
        request.state.correlation_id = correlation_id

        logger.info(
            "request.start",
            extra={
                "request_id": request_id,
                "correlation_id": correlation_id,
                "method": request.method,
                "path": request.url.path,
            },
        )

        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Correlation-Id"] = correlation_id
        return response


def create_app() -> FastAPI:
    """Application factory — used by uvicorn and tests."""
    app = FastAPI(
        title="Ticket Assistant Gateway",
        version="0.2.0",
        lifespan=lifespan,
    )

    # Middleware (order: outermost first).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIdMiddleware)

    # Routers.
    app.include_router(tickets_router)
    app.include_router(oauth_router)
    app.include_router(websocket_router)

    @app.get("/healthz", tags=["health"])
    async def healthz(request: Request) -> dict[str, Any]:
        """Deep readiness probe — checks all backing services.

        Per Task 12 acceptance: ``GET /healthz`` checks postgres, redis,
        vllm, deepseek, stt, tts.  Returns per-service status so the
        client can degrade gracefully (e.g. offer text-only if STT/TTS
        are down).
        """
        checks: dict[str, str] = {}

        # PostgreSQL.
        try:
            pool = request.app.state.pg
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["postgres"] = "ok"
        except Exception:
            checks["postgres"] = "unavailable"

        # Redis.
        try:
            redis = request.app.state.redis
            await redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"

        # vLLM (Qwen primary).
        try:
            import httpx

            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{_vllm_health_url()}/health"
                )
                checks["vllm"] = "ok" if resp.status_code < 500 else "unavailable"
        except Exception:
            checks["vllm"] = "unavailable"

        # DeepSeek connectivity (just checks the API key is configured).
        deepseek_key = _deepseek_api_key()
        checks["deepseek"] = "ok" if deepseek_key else "unconfigured"

        # STT / TTS.
        stt_service = getattr(request.app.state, "stt_service", None)
        tts_service = getattr(request.app.state, "tts_service", None)
        if stt_service is not None:
            try:
                checks["stt"] = "ok" if await stt_service.health() else "unavailable"
            except Exception:
                checks["stt"] = "unavailable"
        else:
            checks["stt"] = "unavailable"
        if tts_service is not None:
            try:
                checks["tts"] = "ok" if await tts_service.health() else "unavailable"
            except Exception:
                checks["tts"] = "unavailable"
        else:
            checks["tts"] = "unavailable"

        return checks

    @app.get("/healthz/voice", tags=["health"])
    async def healthz_voice(request: Request) -> dict[str, Any]:
        """Readiness probe for voice services (STT + TTS).

        Reports per-service status so the client can decide whether to
        offer voice input.  A service is ``"ok"`` if its model can load;
        otherwise ``"unavailable"`` — the ticket still completes with text.
        """
        stt_service = getattr(request.app.state, "stt_service", None)
        tts_service = getattr(request.app.state, "tts_service", None)

        stt_ok = False
        tts_ok = False
        if stt_service is not None:
            try:
                stt_ok = await stt_service.health()
            except Exception:
                stt_ok = False
        if tts_service is not None:
            try:
                tts_ok = await tts_service.health()
            except Exception:
                tts_ok = False

        return {
            "stt": "ok" if stt_ok else "unavailable",
            "tts": "ok" if tts_ok else "unavailable",
        }

    @app.get("/metrics", tags=["observability"])
    async def metrics(request: Request) -> Any:
        """Prometheus metrics endpoint.

        Renders all registered counters, histograms, and gauges in the
        Prometheus text exposition format.
        """
        from starlette.responses import PlainTextResponse

        metrics_registry = getattr(request.app.state, "metrics", None)
        if metrics_registry is None:
            return PlainTextResponse("", status_code=200)
        return PlainTextResponse(
            metrics_registry.render(),
            media_type="text/plain; version=0.0.4",
        )

    return app


def _vllm_health_url() -> str:
    """Get the vLLM base URL for health checks."""
    import os

    return os.getenv("VLLM_BASE_URL", "http://vllm:8000/v1").rstrip("/v1")


def _deepseek_api_key() -> str:
    """Get the DeepSeek API key (checks if configured)."""
    import os

    return os.getenv("DEEPSEEK_API_KEY", "")


app = create_app()
