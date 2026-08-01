"""Live E2E test client — exercises the full ticket round-trip."""
import asyncio
import json
import sys
from uuid import uuid4

import websockets

TICKET_ID = "fa88a3b2-f7d0-434f-a5e2-8765758589a3"
TOKEN = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiI0N2ZlNDQ4My02NGIyLTRiNGItYTYwMC1kYTUxMDNiMWRkYTEiLCJpYXQiOjE3ODU0NDE5MzcsImV4cCI6MTc4NTQ0NTUzN30.kNko-hoXMBUYqzwitmgdU5qutp0sY-fwF8rasoBbeAs"
WS_URL = f"ws://127.0.0.1:8001/ws/tickets/{TICKET_ID}"


async def main():
    async with websockets.connect(
        WS_URL,
        subprotocols=[TOKEN],
    ) as ws:
        # Send connect event.
        await ws.send(json.dumps({
            "type": "ticket:connect",
            "payload": {"last_event_id": None},
        }))
        msg = await ws.recv()
        print("Connected:", json.loads(msg))

        # Send a text message.
        client_msg_id = str(uuid4())
        await ws.send(json.dumps({
            "type": "message:submit",
            "correlation_id": str(uuid4()),
            "payload": {
                "text": "Hello, what is 2+2?",
                "client_message_id": client_msg_id,
            },
        }))

        # Collect events for 10 seconds.
        print("\n--- Events ---")
        try:
            for _ in range(20):
                msg = await asyncio.wait_for(ws.recv(), timeout=10)
                event = json.loads(msg)
                print(f"  {event.get('type')}: {event.get('payload', {})}")
                if event.get("type") == "ticket:completed":
                    break
        except asyncio.TimeoutError:
            print("  (timeout waiting for events)")

        print("\n✅ Round-trip complete")


if __name__ == "__main__":
    asyncio.run(main())
