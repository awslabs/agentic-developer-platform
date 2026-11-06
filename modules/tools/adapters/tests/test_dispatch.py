"""The adapter cannot turn observation or stale authority into tool dispatch."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from adp_tools.adapter_contracts import Admission
from fastapi import HTTPException

from tool_adapters import dispatch


@pytest.mark.parametrize("change", ["observation", "old_release", "expired", "wrong_identity", "foundation"])
def test_dispatch_refuses_wrong_purpose_generation_or_identity_before_core(monkeypatch, release, binding, change):
    now = int(time.time() * 1000)
    admission = Admission.model_validate(
        {
            "schema_version": "1.0",
            "admission_id": "admission-1",
            "binding": binding,
            "purpose": "tool_call",
            "connector": "atlassian",
            "operation": "get_jira_issue",
            "identity": {
                "workload": "adp-tools-dev-atlassian-mcp-oauth-user",
                "provider": "adp-tools-dev-atlassian-mcp-user",
                "canonical_user_id": "alice",
            },
            "context": {
                "tenant_id": "org-acme",
                "canonical_user_id": "alice",
                "connection_id": "owned",
                "binding_revision": "revision-1",
                "site_id": "site-1",
                "scopes": ["read:jira:agent-interface"],
            },
            "issued_at_ms": now,
            "expires_at_ms": now + 5000,
            "deadline_at_ms": now + 10000,
        }
    )
    configuration = SimpleNamespace(
        mode="observation",
        release=admission.binding.release,
        observation=SimpleNamespace(
            core_authorize_url="https://core.example.test/internal/v1/tool-adapters/authorize",
            identity_workload="adp-tools-dev-atlassian-mcp-oauth-user",
            identity_provider="adp-tools-dev-atlassian-mcp-user",
        ),
    )
    if change == "observation":
        admission = admission.model_copy(update={"purpose": "connection_observe"})
    elif change == "old_release":
        configuration.release = release | {"group_generation": 2}
    elif change == "expired":
        admission = admission.model_copy(update={"expires_at_ms": now - 1})
    elif change == "wrong_identity":
        configuration.observation.identity_provider = "other-provider"
    elif change == "foundation":
        configuration.mode = "foundation"
    outbound = AsyncMock()
    monkeypatch.setattr(dispatch.httpx.AsyncClient, "post", outbound)
    with pytest.raises(HTTPException) as refused:
        asyncio.run(dispatch.consume_dispatch(admission, configuration))
    assert refused.value.status_code == 403
    outbound.assert_not_awaited()
