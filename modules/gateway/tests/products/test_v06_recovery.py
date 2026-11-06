"""Owned source recovery with bounded repository paging and GET-only provider fixtures."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from src.products.models import ProductSourceBinding
from tests.products.source_recovery import assert_owned_unlink, assert_resumed_discovery
from tests.products.test_i04 import GatewayClient, cli
from tests.products.test_i08_api import mutation, setup_source
from tests.products.test_i13 import ReadOnlyActions


async def prepare_sources(test_client, db_session, source_member, monkeypatch):
    path, selection = await setup_source(test_client, db_session, source_member)
    provider = ReadOnlyActions()
    provider.assets["second"] = {"id": 36, "owner": {"id": 27, "login": "example"}, "name": "another-existing", "updated_at": "peer-rev"}

    async def repositories_page(installation_id, page, limit):
        provider.calls.append(("GET repositories", page, limit))
        available = [provider.assets["repository"], provider.assets["second"]]
        return {"total_count": 2, "repositories": available[(page - 1) * limit : page * limit]}

    async def repository(installation_id, repository_id):
        provider.calls.append(("GET repository", repository_id))
        return provider.assets["repository"] if repository_id == 35 else provider.assets["second"]

    monkeypatch.setattr(provider, "list_installation_repositories_page", repositories_page)
    monkeypatch.setattr(provider, "get_installation_repository", repository)
    monkeypatch.setattr("src.admin.connections.service._get_github_app_credentials", lambda: ("fixture-app", "fixture-key"))
    monkeypatch.setattr("src.product_onboarding.source_discovery.GitHubAppClient", lambda *args: provider)

    async def read_only_observation(binding, installation):
        observed = await provider.get_installation_repository(int(installation.installation_id), int(binding.native_id))
        assert str(observed["id"]) == binding.native_id
        observed_at = datetime.now(UTC)
        return {
            "coverage": "complete",
            "status": "healthy",
            "observed": {
                "observed_at": observed_at.isoformat(),
                "stale_after": (observed_at + timedelta(minutes=15)).isoformat(),
                "source_revision": observed["updated_at"],
                "namespace": str(observed["owner"]["id"]),
                "native_id": str(observed["id"]),
            },
        }

    monkeypatch.setattr("src.products.source_routes.observe_binding", read_only_observation)
    return path, selection, provider


async def source_page(test_client, path, cursor=None):
    parameters = {"connection_kind": "github_installation", "connection_id": "run-owned-connection", "limit": 1}
    if cursor is not None:
        parameters["resource_cursor"] = cursor
    response = await test_client.get(path.removesuffix("/bindings") + "/sources", params=parameters)
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


@pytest.mark.asyncio
async def test_interrupted_cursor_and_verification_replay_keep_one_native_binding(
    test_client,
    db_session,
    source_member,
    monkeypatch,
):
    path, selection, provider = await prepare_sources(test_client, db_session, source_member, monkeypatch)
    baseline = deepcopy(provider.assets)
    first = await source_page(test_client, path)
    resumed = await source_page(test_client, path, first["resource_cursor"])
    repeated = await source_page(test_client, path, first["resource_cursor"])
    assert_resumed_discovery(first, resumed, repeated)
    assert [item["native_id"] for item in first["resources"] + resumed["resources"]] == ["35", "36"]
    selection["native_id"] = first["resources"][0]["native_id"]
    headers = mutation(1, "v06_owned_binding_12345")
    created = await test_client.post(path, json=selection, headers=headers)
    assert created.status_code == 201, created.text
    replay = await test_client.post(path, json=selection, headers=headers)
    duplicate = await test_client.post(path, json=selection, headers=mutation(2, "v06_repeat_binding_12345"))
    assert replay.status_code == duplicate.status_code == 200
    assert created.json()["id"] == replay.json()["id"] == duplicate.json()["id"]
    binding_path = path + "/" + created.json()["id"] + "/verify"
    verification = await test_client.post(binding_path, headers=mutation(2, "v06_verify_owned_12345"))
    again = await test_client.post(binding_path, headers=mutation(2, "v06_verify_owned_12345"))
    assert verification.status_code == again.status_code == 202, verification.text
    assert verification.json() == again.json()
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 1
    assert provider.assets == baseline and all(call[0].startswith("GET") for call in provider.calls)


@pytest.mark.asyncio
async def test_unlink_one_owned_binding_preserves_other_confirmed_values_and_provider(
    test_client,
    db_session,
    source_member,
    monkeypatch,
):
    path, selection, provider = await prepare_sources(test_client, db_session, source_member, monkeypatch)
    baseline = deepcopy(provider.assets)
    first = await source_page(test_client, path)
    second = await source_page(test_client, path, first["resource_cursor"])
    selection["native_id"] = first["resources"][0]["native_id"]
    owned = await test_client.post(path, json=selection, headers=mutation(1, "v06_select_first_12345"))
    assert owned.status_code == 201, owned.text
    peer_selection = {**selection, "native_id": second["resources"][0]["native_id"]}
    peer = await test_client.post(path, json=peer_selection, headers=mutation(2, "v06_select_second_12345"))
    assert peer.status_code == 201, peer.text
    verified = await test_client.post(path + "/" + peer.json()["id"] + "/verify", headers=mutation(3, "v06_verify_second_12345"))
    assert verified.status_code == 202 and verified.json()["coverage"] == "complete", verified.text
    root = path.removesuffix("/bindings")
    before = (await test_client.get(root)).json()
    before["bindings"] = (await test_client.get(path)).json()["items"]
    assert before["product"]["purpose"] == "Coordinate small releases"
    removed = await test_client.delete(path + "/" + owned.json()["id"], headers=mutation(3, "v06_unlink_owned_12345"))
    assert removed.status_code == 200 and removed.json()["status"] == "unlinked", removed.text
    after = (await test_client.get(root)).json()
    after["bindings"] = (await test_client.get(path)).json()["items"]
    assert_owned_unlink(before, after, owned.json()["id"], peer.json()["id"], baseline, provider.assets, provider.calls)
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 2
    cleaned = await test_client.delete(path + "/" + peer.json()["id"], headers=mutation(4, "v06_unlink_second_12345"))
    assert cleaned.status_code == 200 and cleaned.json()["status"] == "unlinked", cleaned.text
    assert provider.assets == baseline and all(call[0].startswith("GET") for call in provider.calls)


@pytest.mark.asyncio
async def test_cli_resumes_source_cursor_and_reads_only_owned_cleanup(
    test_client,
    db_session,
    source_member,
    monkeypatch,
    tmp_path,
):
    path, selection, provider = await prepare_sources(test_client, db_session, source_member, monkeypatch)
    baseline = deepcopy(provider.assets)
    gateway = GatewayClient(test_client, asyncio.get_running_loop())
    scope = ("--product", path.split("/")[3], "--project", path.split("/")[5])

    async def invoke(*arguments):
        return (await asyncio.to_thread(cli.execute, cli.parser().parse_args(arguments), gateway))["detail"]

    source_args = ("sources", *scope, "--connection-kind", "github_installation", "--connection-id", "run-owned-connection", "--limit", "1")
    first = (await invoke(*source_args))["items"][0]
    resumed = (await invoke(*source_args, "--resource-cursor", first["resource_cursor"]))["items"][0]
    repeated = (await invoke(*source_args, "--resource-cursor", first["resource_cursor"]))["items"][0]
    assert_resumed_discovery(first, resumed, repeated)
    assert [item["native_id"] for item in first["resources"] + resumed["resources"]] == ["35", "36"]

    selection["native_id"] = resumed["resources"][0]["native_id"]
    input_path = tmp_path / "owned-selection.json"
    input_path.write_text(json.dumps(selection), encoding="utf-8")
    bind_args = ("source-bind", *scope, "--revision", "1", "--idempotency-key", "v06_cli_binding_12345", "--input", str(input_path))
    saved = await invoke(*bind_args)
    assert saved["selection"] == selection
    assert (await invoke(*bind_args))["id"] == saved["id"]
    assert [item["id"] for item in (await invoke("source-bindings", *scope))["items"]] == [saved["id"]]
    verification = await test_client.post(path + "/" + saved["id"] + "/verify", headers=mutation(2, "v06_cli_verify_12345"))
    assert verification.status_code == 202 and verification.json()["coverage"] == "complete", verification.text
    assert (await test_client.post(path + "/" + saved["id"] + "/verify", headers=mutation(2, "v06_cli_verify_12345"))).json() == verification.json()

    removed = await test_client.delete(path + "/" + saved["id"], headers=mutation(2, "v06_cli_unlink_12345"))
    assert removed.status_code == 200 and removed.json()["status"] == "unlinked", removed.text
    bindings = (await invoke("source-bindings", *scope))["items"]
    assert len(bindings) == 1 and bindings[0]["id"] == saved["id"] and bindings[0]["status"] == "unlinked"
    assert (await db_session.scalar(select(func.count()).select_from(ProductSourceBinding))) == 1
    assert provider.assets == baseline and all(call[0].startswith("GET") for call in provider.calls)
