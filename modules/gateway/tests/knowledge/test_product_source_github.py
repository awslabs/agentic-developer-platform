"""Provider attestation protects against an installation or repository mix-up."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.knowledge import product_sources as sources


@pytest.mark.parametrize("mutation", [None, "installation", "account", "personal", "suspended", "repo", "owner", "name"])
async def test_native_attestation_checks_selected_installation_account_and_repository(monkeypatch, mutation):
    monkeypatch.setattr(sources, "app_credentials", lambda: ("505", "synthetic-key"))
    source = SimpleNamespace(
        connection=SimpleNamespace(installation_id="202", provider_scope_id="101"),
        binding=SimpleNamespace(namespace="101", native_id="303"),
    )
    installation = {"id": 202, "account": {"id": 101, "login": "fixture", "type": "Organization"}}
    repository = {"id": 303, "owner": {"id": 101}, "full_name": "fixture/service"}
    if mutation == "installation":
        installation["id"] = 404
    elif mutation == "account":
        installation["account"]["id"] = 404
    elif mutation == "personal":
        installation["account"]["type"] = "User"
    elif mutation == "suspended":
        installation["suspended_at"] = "2026-01-01T00:00:00Z"
    elif mutation == "repo":
        repository["id"] = 404
    elif mutation == "owner":
        repository["owner"]["id"] = 404
    elif mutation == "name":
        repository["full_name"] = "fixture/../other"
    client = AsyncMock()
    client.get_installation.return_value = installation
    client.get_installation_repository.return_value = repository
    if mutation:
        with pytest.raises(HTTPException):
            await sources.describe_source(source, client=client)
    else:
        result = await sources.describe_source(source, client=client)
        assert result["source_ref"] == "https://github.com/fixture/service"
        assert result["connection_name"] == "fixture"
        client.get_installation.assert_awaited_once_with(202)
        client.get_installation_repository.assert_awaited_once_with(202, 303)
