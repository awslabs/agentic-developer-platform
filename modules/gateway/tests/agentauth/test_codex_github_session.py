"""Protected GitHub snapshot and effect journal, using real DynamoDB transactions."""

import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.codex_github_session import OperationRequest, frozen_context, operation
from tests.agentauth.test_task_harness import snapshot
from tests.tasks.test_store import AUTHORITY_TABLE, NOW, client  # noqa: F401


@pytest.fixture
def admitted(client, tmp_path):  # noqa: F811
    store = BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client)
    record = SimpleNamespace(tenant_id="tenant-a", invocation_id="github-run", current_attempt=1, repo="owner/repo")
    grant = SimpleNamespace(repo_scope={"owner/repo"}, expires_at=NOW + timedelta(hours=1), is_live=lambda now: now < NOW + timedelta(hours=1))
    file = tmp_path / "catalogue.json"
    file.write_text(json.dumps({"schemaVersion": 1, "snapshots": [snapshot("architect")]}))
    env = {"ADP_CODEX_PERSONA_CATALOG_FILE": str(file), "ADP_CODEX_GITHUB_PERSONAS": "agent-codex-architect"}
    client.put_item(
        TableName=AUTHORITY_TABLE,
        Item={
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#github-run"},
            "persona": {"S": "agent-codex-architect"},
            "issue_number": {"N": "12"},
            "provider_repository_id": {"N": "123"},
        },
    )
    return store, record, grant, env, file


def test_snapshot_is_frozen_across_configuration_changes(admitted):
    store, record, grant, env, file = admitted
    original = frozen_context(store, record, grant, env, now=NOW)
    assert original["persona"] == "agent-codex-architect"
    assert original["repository"] == "owner/repo"
    assert original["issue"] == 12
    assert original["capabilities"] == ["artifacts.publish", "repository.read", "story.create"]
    file.write_text("changed invalid configuration")
    assert frozen_context(store, record, grant, env, now=NOW) == original


@pytest.mark.parametrize("mode", ["disabled", "repository", "expired", "unsupported"])
def test_admission_refuses_missing_authority(admitted, mode):
    store, record, grant, env, file = admitted
    if mode == "disabled":
        env["ADP_CODEX_GITHUB_PERSONAS"] = ""
    elif mode == "repository":
        grant.repo_scope = {"another/repo"}
    elif mode == "expired":
        grant.is_live = lambda now: False
    else:
        file.write_text(json.dumps({"schemaVersion": 1, "snapshots": [snapshot("operations")]}))
    with pytest.raises(ValueError):
        frozen_context(store, record, grant, env, now=NOW)


def claim(kind="model", **kwargs):
    return OperationRequest(operation_id=uuid4(), request_digest="a" * 64, action="claim", kind=kind, **kwargs)


def test_effect_is_reserved_before_execution_and_unknown_result_blocks_replay(admitted):
    store, record, grant, env, _ = admitted
    frozen_context(store, record, grant, env, now=NOW)
    request = claim()
    assert operation(store, record, request) == {"status": "admitted"}
    with pytest.raises(ValueError, match="reconciliation"):
        operation(store, record, request)
    with pytest.raises(ClientError):
        operation(store, record, claim())
    settled = request.model_copy(update={"action": "settle", "result": '{"output":"verified"}'})
    receipt = operation(store, record, settled)
    assert receipt["status"] == "confirmed"
    assert operation(store, record, settled) == receipt
    assert operation(store, record, request) == receipt
    with pytest.raises(ValueError, match="conflict"):
        operation(store, record, settled.model_copy(update={"result": "changed"}))
    assert operation(store, record, claim()) == {"status": "admitted"}


def test_report_finalization_fences_all_further_model_work(admitted):
    store, record, grant, env, _ = admitted
    frozen_context(store, record, grant, env, now=NOW)
    request = claim("report")
    operation(store, record, request)
    operation(store, record, request.model_copy(update={"action": "settle", "result": "published"}))
    with pytest.raises(ClientError):
        operation(store, record, claim())


def test_replacement_worker_cannot_restart_an_interrupted_model_run(admitted):
    store, record, grant, env, _ = admitted
    frozen_context(store, record, grant, env, now=NOW)
    operation(store, record, claim())
    record.current_attempt = 2
    with pytest.raises(ValueError, match="reconciliation"):
        frozen_context(store, record, grant, env, now=NOW)


@pytest.mark.parametrize("kind", ["model", "tool"])
def test_operations_continue_beyond_legacy_ceilings(admitted, kind):
    store, record, grant, env, _ = admitted
    frozen_context(store, record, grant, env, now=NOW)
    for _ in range(40):
        request = claim(kind)
        operation(store, record, request)
        operation(store, record, request.model_copy(update={"action": "settle", "result": "done"}))
    assert operation(store, record, claim(kind)) == {"status": "admitted"}


def test_live_runtime_uses_process_environment_when_no_override(admitted, monkeypatch):
    store, record, grant, env, _ = admitted
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert frozen_context(store, record, grant, None, now=NOW)["persona"] == "agent-codex-architect"
    # An explicit empty override must not inherit ambient grants.
    with pytest.raises(ValueError, match="disabled"):
        frozen_context(store, record, grant, {}, now=NOW)


def test_planning_effect_receipts_survive_new_invocation(admitted):
    store, record, grant, env, _ = admitted
    frozen = frozen_context(store, record, grant, env, now=NOW)
    request = claim("planning", effect_key="story-create:audit")
    operation(store, record, request)
    receipt = operation(store, record, request.model_copy(update={"action": "settle", "result": '{"number":42}'}))
    other = SimpleNamespace(**{**vars(record), "invocation_id": "second-run"})
    store.client.put_item(
        TableName=store.table, Item={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "CODEX_GITHUB#second-run"}, "context": {"S": json.dumps(frozen)}}
    )
    assert operation(store, other, claim("planning", effect_key="story-create:audit")) == receipt
    with pytest.raises(ValueError, match="conflict"):
        operation(store, other, claim("planning", effect_key="story-create:audit").model_copy(update={"request_digest": "b" * 64}))


def test_planning_effects_are_persona_scoped_and_pending_is_not_replayed(admitted):
    store, record, grant, env, _ = admitted
    frozen_context(store, record, grant, env, now=NOW)
    with pytest.raises(ValueError, match="unavailable"):
        operation(store, record, claim("planning", effect_key="dispatch:42"))
    operation(store, record, claim("planning", effect_key="story-create:audit"))
    with pytest.raises(ValueError, match="reconciliation"):
        operation(store, record, claim("planning", effect_key="story-create:audit"))
