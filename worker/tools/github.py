"""GitHub tool connectors — create_issue and comment_issue.

Both tools call the GitHub REST API via ``httpx`` with the user's OAuth
access token.  They are timeout-bounded and return structured results.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from contracts.models import ToolPayload
from worker.tools.base import OAuthCredential, Tool, ToolResult

logger = logging.getLogger("worker.tools.github")

GITHUB_API_BASE = "https://api.github.com"


class GitHubCreateIssue(Tool):
    """Create a GitHub issue in a repository."""

    name = "github.create_issue"

    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        cred = self._require_credential(credential, self.name)
        args = payload.arguments
        repository = args.get("repository", "")
        if "/" not in repository:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error="arguments.repository must be 'owner/repo'",
            )

        title = args.get("title", "")
        body = args.get("body", "")
        labels = args.get("labels", [])

        if not title or not body:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error="arguments.title and arguments.body are required",
            )

        if payload.dry_run:
            return ToolResult(
                success=True,
                tool_name=self.name,
                data={
                    "dry_run": True,
                    "repository": repository,
                    "title": title,
                    "body_preview": body[:100],
                },
            )

        url = f"{GITHUB_API_BASE}/repos/{repository}/issues"
        headers = {
            "Authorization": f"Bearer {cred.access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        issue_data: dict[str, Any] = {"title": title, "body": body}
        if labels:
            issue_data["labels"] = labels

        response = await self._http.post(url, headers=headers, json=issue_data)
        response.raise_for_status()
        issue = response.json()

        return ToolResult(
            success=True,
            tool_name=self.name,
            data={
                "issue_number": issue.get("number"),
                "issue_url": issue.get("html_url"),
                "repository": repository,
            },
        )


class GitHubCommentIssue(Tool):
    """Comment on an existing GitHub issue."""

    name = "github.comment_issue"

    async def _invoke(
        self,
        payload: ToolPayload,
        credential: OAuthCredential | None,
    ) -> ToolResult:
        cred = self._require_credential(credential, self.name)
        args = payload.arguments
        repository = args.get("repository", "")
        issue_number = args.get("issue_number")
        body = args.get("body", "")

        if "/" not in repository:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error="arguments.repository must be 'owner/repo'",
            )
        if issue_number is None or not body:
            return ToolResult(
                success=False,
                tool_name=self.name,
                error="arguments.issue_number and arguments.body are required",
            )

        if payload.dry_run:
            return ToolResult(
                success=True,
                tool_name=self.name,
                data={
                    "dry_run": True,
                    "repository": repository,
                    "issue_number": issue_number,
                    "body_preview": body[:100],
                },
            )

        url = f"{GITHUB_API_BASE}/repos/{repository}/issues/{issue_number}/comments"
        headers = {
            "Authorization": f"Bearer {cred.access_token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        response = await self._http.post(url, headers=headers, json={"body": body})
        response.raise_for_status()
        comment = response.json()

        return ToolResult(
            success=True,
            tool_name=self.name,
            data={
                "comment_id": comment.get("id"),
                "comment_url": comment.get("html_url"),
                "repository": repository,
                "issue_number": issue_number,
            },
        )
