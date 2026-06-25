"""Pydantic schemas for the repo picker endpoint.

Issue #1793 (Story D of E10 #1736): GET /api/agent-context/github/accessible-repos.
Contract per design docs/agent-context/design-1736-knowledge-asset-registry.md §8.4.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AccessibleRepo(BaseModel):
    """A single repository accessible via the tenant's GitHub App installation."""

    full_name: str = Field(..., description="Owner/repo, e.g. 'acme/my-service'")
    private: bool = Field(..., description="Whether the repo is private")
    url: str = Field(..., description="HTML URL of the repo")


class AccessibleReposResponse(BaseModel):
    """Paginated response for the repo picker endpoint."""

    repos: list[AccessibleRepo] = Field(default_factory=list)
    total: int = Field(0, description="Total repos accessible (before search filter)")
    page: int = Field(1)
    has_more: bool = Field(False)
