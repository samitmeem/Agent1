"""Pydantic schemas for tool argument validation.

These schemas are validated BEFORE tool execution to prevent command injection,
SSRF, and other injection attacks from malicious LLM outputs or prompt injection.

All tool arguments MUST be validated against these schemas.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class GitHubCreateIssueArgs(BaseModel):
    """Arguments for github.create_issue tool.

    Security: Repository format validated with regex to prevent injection.
    Title and body length limited to prevent DoS.
    """

    repository: str = Field(
        ...,
        min_length=3,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$",
        description="Repository in format owner/repo",
    )
    title: str = Field(..., min_length=1, max_length=256, description="Issue title")
    body: str = Field(default="", max_length=65536, description="Issue body (markdown)")

    @field_validator("repository")
    @classmethod
    def validate_repo_format(cls, v: str) -> str:
        """Ensure repository is in owner/repo format with no special chars."""
        if "/" not in v or v.count("/") > 1:
            raise ValueError("Repository must be in format owner/repo")
        owner, repo = v.split("/")
        if not owner or not repo:
            raise ValueError("Repository owner and name cannot be empty")
        return v


class GitHubCommentIssueArgs(BaseModel):
    """Arguments for github.comment_issue tool."""

    repository: str = Field(
        ...,
        min_length=3,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+$",
        description="Repository in format owner/repo",
    )
    issue_number: int = Field(..., ge=1, le=1_000_000, description="Issue number")
    body: str = Field(default="", max_length=65536, description="Comment body (markdown)")

    @field_validator("repository")
    @classmethod
    def validate_repo_format(cls, v: str) -> str:
        """Ensure repository is in owner/repo format with no special chars."""
        if "/" not in v or v.count("/") > 1:
            raise ValueError("Repository must be in format owner/repo")
        owner, repo = v.split("/")
        if not owner or not repo:
            raise ValueError("Repository owner and name cannot be empty")
        return v


class GitHubSearchArgs(BaseModel):
    """Arguments for github.search tool."""

    query: str = Field(..., min_length=1, max_length=256, description="Search query")
    max_results: int = Field(default=10, ge=1, le=100, description="Max results")


class GmailSendArgs(BaseModel):
    """Arguments for gmail.send tool.

    Security: Email address format validated. Subject and body length limited.
    """

    to: str = Field(
        ...,
        min_length=3,
        max_length=254,
        pattern=r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$",
        description="Recipient email address",
    )
    subject: str = Field(..., min_length=1, max_length=256, description="Email subject")
    body: str = Field(default="", max_length=65536, description="Email body (plain text)")

    @field_validator("to")
    @classmethod
    def validate_email(cls, v: str) -> str:
        """Basic email format validation."""
        if "@" not in v or v.count("@") > 1:
            raise ValueError("Invalid email address format")
        return v.lower()


class GmailSearchArgs(BaseModel):
    """Arguments for gmail.search tool."""

    query: str = Field(..., min_length=1, max_length=256, description="Gmail search query")
    max_results: int = Field(default=10, ge=1, le=100, description="Max results")


# Tool name to schema mapping. Tool arguments are validated against the
# matching schema before execution to prevent injection attacks.
TOOL_SCHEMAS = {
    "github.create_issue": GitHubCreateIssueArgs,
    "github.comment_issue": GitHubCommentIssueArgs,
    "github.search": GitHubSearchArgs,
    "gmail.send_email": GmailSendArgs,
    "gmail.search_messages": GmailSearchArgs,
}
