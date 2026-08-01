import asyncio

import pytest
from fastapi.testclient import TestClient

from gateway.app.lifespan import MAX_TICKET_WS_CONNECTIONS, TicketConnections
from gateway.app.main import app
from gateway.app.websocket import _extract_token_from_ws


class DummyWebSocket:
    def __init__(self, cookies: dict[str, str], headers: dict[str, str]):
        self.cookies = cookies
        self.headers = headers


def test_websocket_route_registered() -> None:
    found = any(
        type(r).__name__ == "_IncludedRouter"
        and any(getattr(sub, "path", None) == "/ws/tickets/{ticket_id}" for sub in r.original_router.routes)
        for r in app.routes
    )
    assert found, "WebSocket route /ws/tickets/{ticket_id} should be registered"


def test_extract_token_from_ws_cookie() -> None:
    ws = DummyWebSocket(cookies={"access_token": "abc"}, headers={})
    assert _extract_token_from_ws(ws) == "abc"


def test_extract_token_from_ws_subprotocol() -> None:
    ws = DummyWebSocket(cookies={}, headers={"sec-websocket-protocol": "jwt-token,graphql-ws"})
    assert _extract_token_from_ws(ws) == "jwt-token"


def test_ticket_list_requires_auth() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/api/tickets")
    assert response.status_code == 401


def test_healthz_returns_ok() -> None:
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    # Deep health check returns per-service status.
    assert "postgres" in data
    assert "redis" in data
    assert "vllm" in data
    assert "deepseek" in data
    assert "stt" in data
    assert "tts" in data


def test_ticket_connections_limit() -> None:
    connections = TicketConnections()
    ticket_id = "ticket-1"

    for _ in range(MAX_TICKET_WS_CONNECTIONS):
        asyncio.run(connections.add(ticket_id, object()))

    with pytest.raises(RuntimeError):
        asyncio.run(connections.add(ticket_id, object()))


def test_ticket_connections_remove_cleans_empty_bucket() -> None:
    connections = TicketConnections()
    ticket_id = "ticket-1"
    websocket = object()

    asyncio.run(connections.add(ticket_id, websocket))
    assert connections.count(ticket_id) == 1

    connections.remove(ticket_id, websocket)
    assert connections.count(ticket_id) == 0
    assert ticket_id not in connections._sockets


# ---------------------------------------------------------------------------
# Voice health endpoint tests (Task 9).
# ---------------------------------------------------------------------------
def test_voice_health_endpoint_returns_status() -> None:
    """The /healthz/voice endpoint reports STT/TTS status."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/healthz/voice")
    assert response.status_code == 200
    data = response.json()
    assert "stt" in data
    assert "tts" in data
    # Without faster_whisper/kokoro installed, both report unavailable.
    assert data["stt"] in ("ok", "unavailable")
    assert data["tts"] in ("ok", "unavailable")


def test_voice_health_endpoint_registered() -> None:
    """The /healthz/voice route is registered on the app."""
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/healthz/voice" in paths


# ---------------------------------------------------------------------------
# Observability endpoint tests (Task 12).
# ---------------------------------------------------------------------------
def test_metrics_endpoint_returns_text() -> None:
    """The /metrics endpoint returns Prometheus text format."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/metrics")
    assert response.status_code == 200
    # Without prometheus_client installed, returns empty string (still 200).
    assert response.text is not None


def test_metrics_endpoint_registered() -> None:
    """The /metrics route is registered on the app."""
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/metrics" in paths


def test_healthz_checks_all_services() -> None:
    """The deep /healthz checks postgres, redis, vllm, deepseek, stt, tts."""
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/healthz")
    assert response.status_code == 200
    data = response.json()
    # All 6 services must be present in the response.
    expected = {"postgres", "redis", "vllm", "deepseek", "stt", "tts"}
    assert expected.issubset(set(data.keys()))
    # Without backing services, they report unavailable (not crash).
    for service in expected:
        assert data[service] in ("ok", "unavailable", "unconfigured")
