"""Request/response schemas for the org GitHub-connection lifecycle.

Issue #4842 (EPIC #4839 · C3).
"""

from pydantic import BaseModel, ConfigDict, Field


class GitHubConnectionAttachRequest(BaseModel):
    """POST /admin/organizations/{org_id}/connections/github request body."""

    installation_id: int = Field(..., gt=0, description="The GitHub App installation id to bind to this organization.")
    restore_revoked: bool = Field(False, description="Explicit platform-admin restoration of a completed local-only detach owned by this tenant.")
    github_org_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Stable numeric GitHub org id. Recorded on the organization when supplied. "
            "Worth supplying: it is what lets a later ownership check ATTEST this claim "
            "against GitHub, and a claim nobody can attest is refused fail-closed."
        ),
    )
    github_org_login: str | None = Field(
        default=None,
        max_length=255,
        description="GitHub org login, stored for display in the Connections tab.",
    )


class ReconcileRoutingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_projection_org_id: str = Field(..., min_length=1, description="Exact owner observed on the stale forward routing row.")


class ReconcileRoutingResponse(BaseModel):
    installation_id: int
    observed_projection_org_id: str
    authoritative_org_id: str
    outcome: str


class GitHubConnectionResponse(BaseModel):
    """A single GitHub connection as the Connections tab renders it."""

    org_id: str
    installation_id: str
    github_org_id: str | None = None
    github_org_login: str | None = None
    # True when a channel_tenant_map row records this binding. That table is the
    # only claim that can GRANT installation ownership; the org's own
    # github_installation_ids list is an assertion a tenant makes about itself.
    # A connection routable=False is visible in the UI but will not resolve.
    routable: bool = True


class GitHubConnectionListResponse(BaseModel):
    """GET /admin/organizations/{org_id}/connections/github response."""

    connections: list[GitHubConnectionResponse] = Field(default_factory=list)
    total: int = 0


class GitHubConnectionDetachResponse(BaseModel):
    """DELETE /admin/organizations/{org_id}/connections/github/{installation_id}."""

    detached: bool
    org_id: str
    installation_id: str
    # Stated in the response body, not just in a log line: detaching stops
    # webhook dispatch for this GitHub org, and that is by design (fail-closed).
    # An operator who detaches to "clean up" needs to see the consequence at the
    # moment they do it.
    warning: str
    residual: list[str] = Field(default_factory=list)
