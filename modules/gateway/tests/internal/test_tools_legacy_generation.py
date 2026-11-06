"""All existing connector dispatch paths honor the coordinated group release."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.internal import tools_github, tools_jira, tools_slack
from src.shared.models.base import Base
from src.shared.models.vault import ToolAdapterActivation

RELEASE = {
    "contract_version": "1.0",
    "source_revision": "a" * 40,
    "image_digest": "sha256:" + "b" * 64,
    "group": "atlassian-foundation",
    "group_generation": 1,
    "backend_generation": 1,
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,selected",
    [
        (tools_github, "selected_github_connection"),
        (tools_jira, "selected_jira_connection"),
        (tools_slack, "selected_slack_connection"),
    ],
)
@pytest.mark.parametrize("state", ["absent", "fenced", "active", "legacy_stale", "legacy_current"])
async def test_legacy_dispatch_checks_durable_group_before_credential_use(monkeypatch, module, selected, state):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if state != "absent":
        async with factory() as db:
            db.add(ToolAdapterActivation(group=RELEASE["group"], status=state.split("_")[0], release=dict(RELEASE)))
            await db.commit()
    monkeypatch.setenv("ADP_TOOL_ADAPTER_RELEASE", json.dumps(RELEASE))
    monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(module, "check_policy", AsyncMock())
    monkeypatch.setattr(module, "current_tools_human", AsyncMock(return_value=SimpleNamespace(id="alice", org_id="org-acme")))
    selected_connection = AsyncMock(return_value=(SimpleNamespace(id="owned"), SimpleNamespace(), None))
    monkeypatch.setattr(module, selected, selected_connection)
    monkeypatch.setattr(module, "check_fresh", lambda claims: None)
    claims = {
        "principal": {"user_id": "alice", "tenant_id": "org-acme", "team_id": "team-eng"},
        "adapter_release": RELEASE if state == "legacy_current" else {**RELEASE, "group_generation": 0},
        selected.removeprefix("selected_"): {"connection_id": "owned"},
    }
    if state == "absent":
        claims.pop("adapter_release")
    try:
        if state in {"absent", "legacy_current"}:
            async with module.current_authority(claims, SimpleNamespace()):
                pass
            selected_connection.assert_awaited_once()
        else:
            with pytest.raises(HTTPException) as refused:
                async with module.current_authority(claims, SimpleNamespace()):
                    pass
            assert refused.value.status_code == 403
            selected_connection.assert_not_awaited()
    finally:
        await engine.dispose()
