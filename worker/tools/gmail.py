"""Gmail tool connectors — send_email and search_messages.

Both tools call the Gmail API via ``httpx`` with the user's OAuth access
token.  ``send_email`` is a sensitive action that requires user confirmation
unless an automation policy flag is set.
"""
from __future__ import annotations

import logging
import base64
from typing import Any

import httpx

from contracts.models import ToolPayload
from worker.tools.base import OAuthCredential, Tool, ToolResult

logger = logging.getLogger("worker.tools.gmail")

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"


class GmailSendEmail(Tool):
    """Send an email via Gmail.

    This is a **sensitive action** — the ``ToolExecutor`` checks
    ``UserConfirmationProtocol`` before invoking this tool.
    """

    name = "gmail.send_email"

    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        cred = self._require_credential(credential, self.name)
        args = payload.arguments
        to = args.get("to", "")
        subject = args.get("subject", "")
        body = args.get("body", "")

        if not to or not subject or not body:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error="arguments.to, arguments.subject, and arguments.body are required",
            )

        if payload.dry_run:
            return ToolResult(
                success=True,
                tool_name=self.name,
                data={
                    "dry_run": True,
                    "to": to,
                    "subject": subject,
                    "body_preview": body[:100],
                },
            )

        # Build RFC 2822 message and base64url-encode it.
        raw_message = (
            f"To: {to}\r\n"
            f"Subject: {subject}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}"
        )
        encoded = base64.urlsafe_b64encode(raw_message.encode("utf-8")).decode("ascii")

        url = f"{GMAIL_API_BASE}/users/me/messages/send"
        headers = {
            "Authorization": f"Bearer {cred.access_token}",
            "Accept": "application/json",
        }

        response = await self._http.post(
            url, headers=headers, json={"raw": encoded}
        )
        response.raise_for_status()
        result = response.json()

        return ToolResult(
            success=True,
            tool_name=self.name,
            data={
                "message_id": result.get("id"),
                "thread_id": result.get("threadId"),
                "to": to,
                "subject": subject,
            },
        )


class GmailSearchMessages(Tool):
    """Search Gmail messages with a query string."""

    name = "gmail.search_messages"

    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        cred = self._require_credential(credential, self.name)
        args = payload.arguments
        query = args.get("query", "")
        max_results = args.get("max_results", 10)

        if not query:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error="arguments.query is required",
            )

        if payload.dry_run:
            return ToolResult(
                success=True,
                tool_name=self.name,
                data={
                    "dry_run": True,
                    "query": query,
                    "max_results": max_results,
                },
            )

        url = f"{GMAIL_API_BASE}/users/me/messages"
        headers = {
            "Authorization": f"Bearer {cred.access_token}",
            "Accept": "application/json",
        }
        params: dict[str, Any] = {"q": query, "maxResults": max_results}

        response = await self._http.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()

        messages = data.get("messages", [])
        # Return just the message IDs — full fetch is a separate call.
        return ToolResult(
            success=True,
            tool_name=self.name,
            data={
                "message_ids": [m.get("id") for m in messages],
                "result_size_estimate": data.get("resultSizeEstimate", len(messages)),
                "query": query,
            },
        )
