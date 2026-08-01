"""Gateway-side Redis Stream subscriber for ticket event fan-out.

Subscribes to the Redis Stream ``ticket:{ticket_id}:events`` once per
active ticket and fans events out to all authorized WebSocket clients via
the ``TicketConnections`` registry.

Key design:
- One consumer task per active ticket (not per WebSocket).
- Uses ``XREAD`` with a ``$`` start ID (only new entries).
- On client disconnect, the consumer keeps running until all sockets
  for the ticket are gone, then stops.
- Events are parsed from the Redis Stream entry format and sent as JSON
  to each WebSocket.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID

from redis.asyncio import Redis

from gateway.app.lifespan import TicketConnections

logger = logging.getLogger("gateway.streaming")

# Stream key helpers (must match worker.streaming).
STREAM_PREFIX = "ticket:"
STREAM_SUFFIX = ":events"


def stream_key(ticket_id: str | UUID) -> str:
    """Build the Redis Stream key for a ticket."""
    return f"{STREAM_PREFIX}{ticket_id}{STREAM_SUFFIX}"


class StreamSubscriber:
    """Manages Redis Stream consumer tasks per active ticket.

    The Gateway creates one ``StreamSubscriber`` at startup (stored on
    ``app.state``).  When a WebSocket connects to a ticket, the Gateway
    calls ``ensure_subscriber(ticket_id)`` to start the consumer if it
    isn't already running.  When the last socket disconnects, the
    consumer is stopped.
    """

    def __init__(self, redis: Redis, connections: TicketConnections) -> None:
        self._redis = redis
        self._connections = connections
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def ensure_subscriber(self, ticket_id: str) -> None:
        """Start a consumer task for ``ticket_id`` if not already running."""
        async with self._lock:
            if ticket_id in self._tasks and not self._tasks[ticket_id].done():
                return
            task = asyncio.create_task(self._consume(ticket_id))
            self._tasks[ticket_id] = task
            logger.info("started stream consumer for ticket %s", ticket_id)

    async def stop_subscriber(self, ticket_id: str) -> None:
        """Stop the consumer task for ``ticket_id``."""
        async with self._lock:
            task = self._tasks.pop(ticket_id, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("stopped stream consumer for ticket %s", ticket_id)

    async def stop_all(self) -> None:
        """Stop all consumer tasks (called on shutdown)."""
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _consume(self, ticket_id: str) -> None:
        """Consume events from the Redis Stream and fan out to WebSockets.

        Runs until cancelled or until all sockets for the ticket are gone.
        """
        key = stream_key(ticket_id)
        # Start reading from the latest entry ($ = only new messages).
        last_id = "$"

        try:
            while True:
                # Check if there are still active connections.
                if self._connections.count(ticket_id) == 0:
                    logger.info(
                        "no more connections for ticket %s — stopping consumer",
                        ticket_id,
                    )
                    break

                # Block on XREAD for up to 5 seconds.
                result = await self._redis.xread({key: last_id}, count=10, block=5000)
                if not result:
                    continue

                for _stream, entries in result:
                    for entry_id, fields in entries:
                        last_id = entry_id
                        message = self._parse_entry(ticket_id, entry_id, fields)
                        if message is not None:
                            await self._fanout(ticket_id, message)

        except asyncio.CancelledError:
            logger.debug("consumer for ticket %s cancelled", ticket_id)
            raise
        except Exception:
            logger.exception("consumer for ticket %s crashed", ticket_id)

    @staticmethod
    def _parse_entry(
        ticket_id: str,
        entry_id: str,
        fields: dict[bytes | str, bytes | str],
    ) -> dict[str, Any] | None:
        """Parse a Redis Stream entry into a WebSocket JSON message.

        Redis stores stream field values as bytes; we decode them here.
        """
        def decode(val: bytes | str) -> str:
            if isinstance(val, bytes):
                return val.decode("utf-8")
            return val

        try:
            event_type = decode(fields.get(b"type", fields.get("type", b"")))
            if not event_type:
                return None

            payload_raw = decode(fields.get(b"payload", fields.get("payload", b"{}")))
            payload = json.loads(payload_raw) if payload_raw else {}

            sequence = decode(fields.get(b"sequence", fields.get("sequence", b"0")))
            correlation_id = decode(
                fields.get(b"correlation_id", fields.get("correlation_id", b""))
            )

            return {
                "event_id": entry_id,
                "type": event_type,
                "ticket_id": ticket_id,
                "sequence": int(sequence),
                "correlation_id": correlation_id or None,
                "payload": payload,
            }
        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            logger.warning("failed to parse stream entry for ticket %s: %s", ticket_id, exc)
            return None

    async def _fanout(self, ticket_id: str, message: dict[str, Any]) -> None:
        """Send a message to all WebSocket clients for a ticket."""
        sockets = self._connections.get(ticket_id)
        if not sockets:
            return

        dead: list = []
        for ws in sockets:
            try:
                await ws.send_json(message)
            except Exception:
                logger.debug("socket send failed — marking for removal")
                dead.append(ws)

        for ws in dead:
            self._connections.remove(ticket_id, ws)
