"""Explicit system identity for core-authorized Jira MCP reads; no user impersonation."""

from typing import Literal

from pydantic import Field

from adp_tools.adapter_contracts import AdapterRelease, Reference, Timestamp, WireModel


class JiraSystemRequest(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    purpose: Literal["jira_system"] = "jira_system"
    connection_id: Reference
    connection_revision: Reference
    tenant_id: Reference
    site_id: Reference
    workload: Reference
    provider: Reference
    connection_method: Literal["service_account", "forge_app"] = "service_account"
    forge_app_id: str | None = Field(default=None, max_length=255)
    forge_environment_id: str | None = Field(default=None, max_length=255)
    forge_installation_id: str | None = Field(default=None, max_length=255)
    forge_action: Literal["pair", "refresh"] | None = None
    forge_invocation: str | None = Field(default=None, max_length=16384, repr=False)
    forge_token: str | None = Field(default=None, max_length=32768, repr=False)
    operation: Literal["verify", "projects", "boards", "issues", "backlog", "board", "sprints"]
    arguments: dict = Field(default_factory=dict)
    schemas: dict = Field(default_factory=dict)
    release: AdapterRelease
    deadline_at_ms: Timestamp
    signed_assertion: str = Field(min_length=1, max_length=16384, repr=False)
