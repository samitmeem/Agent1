"""Tool execution package — GitHub and Gmail connectors.

Implements the 4 tools per `AgenticProject.md` §1.9 and §3 (Tool/OAuth safety):

- ``github.create_issue``
- ``github.comment_issue``
- ``gmail.send_email``
- ``gmail.search_messages``

All tools are:
- **Allow-listed**: only the 4 names above are accepted.
- **Idempotent**: retry reuses the same ``execution_id`` / ``idempotency_key``,
  preventing duplicate issues, comments, or emails.
- **Timeout-bounded**: every ``httpx`` call has a timeout.
- **Rate-limited**: a simple token-bucket per ``user_id`` + ``tool_name``.
- **Logged**: a ``tool_executions`` row is inserted before AND after each
  invocation.

OAuth credentials are retrieved from PostgreSQL, refreshed under a
per-connection Redis lock, and never stored in LangGraph state — only
``oauth_connection_id`` is passed through.
"""
from __future__ import annotations

from worker.tools.base import (
    ALLOWED_TOOLS,
    OAuthCredential,
    OAuthCredentialStoreProtocol,
    RateLimiterProtocol,
    Tool,
    ToolExecutionRecord,
    ToolExecutionStoreProtocol,
    ToolResult,
    UserConfirmationProtocol,
)
from worker.tools.executor import ToolExecutor
from worker.tools.gmail import (
    GmailSendEmail,
    GmailSearchMessages,
)
from worker.tools.github import (
    GitHubCommentIssue,
    GitHubCreateIssue,
)

__all__ = [
    "ALLOWED_TOOLS",
    "GitHubCreateIssue",
    "GitHubCommentIssue",
    "GmailSendEmail",
    "GmailSearchMessages",
    "OAuthCredential",
    "OAuthCredentialStoreProtocol",
    "RateLimiterProtocol",
    "Tool",
    "ToolExecutionRecord",
    "ToolExecutionStoreProtocol",
    "ToolExecutor",
    "ToolResult",
    "UserConfirmationProtocol",
]
