"""CLI-20 reviewed state, exact target and uncertain transport regressions."""

import importlib.util
import json
import uuid
from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from src.admin.bedrock_routing.schemas import (
    EffectiveMappingResponse,
    MappingUpsertRequest,
    MySelectionRequest,
    MySelectionResponse,
    RegisterSharedConnectionDestination,
    SelectableConnection,
)

spec = importlib.util.spec_from_file_location("bedrock_lifecycle", Path(__file__).parents[2] / "cli/adp-bedrock.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
REVISION = "a" * 64
ACCOUNT = "123456789012"


def state(**changes):
    # Serialize the production model, rather than inventing a permissive endpoint.
    value = MySelectionResponse(
        revision=REVISION,
        effective=EffectiveMappingResponse(
            user_id="user-canonical", rung="platform", account_id=None, destination_id=None, destination_label=None, source=None
        ),
        connections=[
            SelectableConnection(credential_id="connection", label="Own account", account_id=ACCOUNT, status="verified", selectable=True, reason=None)
        ],
    ).model_dump(mode="json")
    value.update(changes)
    return value


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(c.common, "state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(c.common, "authenticated_scope", lambda: {"identity": "alice", "tenant": "tenant-a"})
    monkeypatch.setattr(c.common, "ensure_can_mutate", Mock())
    return Mock(base="https://gateway.test/api")


def args(*argv):
    return c.parser().parse_args(argv)


def test_select_serializes_real_api_account_precondition_and_reads_billing(api):
    before = state()
    after = state(
        own_selection_credential_id="connection",
        own_selection_account_id=ACCOUNT,
        own_selection_destination_id="destination",
        own_selection_active=True,
    )
    after["effective"].update(rung="user", destination_id="destination", account_id=ACCOUNT, source="self")
    api.request.side_effect = [before, after, after]
    key = str(uuid.uuid4())
    result = c.run(args("select", "--connection", "connection", "--yes", "--expect-revision", REVISION, "--operation-id", key), api)
    assert result["status"] == "configured"
    request = api.request.call_args_list[1]
    assert request.args[:2] == ("PUT", c.SELF_ROUTING + "?expected_revision=" + REVISION)
    wire = MySelectionRequest.model_validate(request.args[2])
    assert wire.credential_id == "connection" and wire.expected_account_id == ACCOUNT
    assert result["detail"]["before"]["effective"]["rung"] == "platform"
    assert result["detail"]["after"]["effective"]["account_id"] == ACCOUNT


def test_dry_run_never_probes_or_writes(api):
    api.request.return_value = state()
    result = c.run(args("select", "--connection", "connection", "--dry-run"), api)
    assert result["status"] == "dry_run"
    api.request.assert_called_once_with("GET", c.SELF_ROUTING)
    c.common.ensure_can_mutate.assert_not_called()


@pytest.mark.parametrize("override", [{"pinned_by_platform_admin": True}, {"connections": []}])
def test_admin_pin_and_foreign_connection_refused_before_put(api, override):
    api.request.return_value = state(**override)
    with pytest.raises(c.CliError):
        c.run(args("select", "--connection", "connection", "--yes"), api)
    assert api.request.call_count == 1


def test_yes_does_not_approve_an_unseen_revision(api):
    api.request.return_value = state()
    with pytest.raises(c.CliError, match="reviewed"):
        c.run(args("reset", "--yes"), api)
    api.request.assert_called_once_with("GET", c.SELF_ROUTING)


def test_stale_preview_reads_fresh_effective_state_without_write(api):
    api.request.return_value = state()
    result = c.run(args("reset", "--yes", "--expect-revision", "b" * 64, "--operation-id", str(uuid.uuid4())), api)
    assert result["status"] == "pending"
    assert result["detail"]["conflict"] == "stale_revision"
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


def test_unknown_delivery_reuses_receipt_without_resending(api):
    before = state()
    api.request.side_effect = [before, c.CliError("Lost response", "unknown_mutation_outcome", 4), before]
    command = args("reset", "--yes", "--expect-revision", REVISION, "--operation-id", str(uuid.uuid4()))
    first = c.run(command, api)
    assert first["status"] == "pending"
    api.request.side_effect = None
    api.request.return_value = before
    api.request.reset_mock()
    replay = c.run(command, api)
    assert replay["detail"]["replayed_without_write"]
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


def test_malformed_reset_ack_does_not_become_success_from_matching_readback(api):
    api.request.side_effect = [state(), {}, state()]
    result = c.run(args("reset", "--yes", "--expect-revision", REVISION, "--operation-id", str(uuid.uuid4())), api)
    assert result["status"] == "pending"
    assert result["detail"]["reason"] == "unknown_mutation_outcome"


def test_old_server_without_revision_is_unavailable(api):
    old = state()
    old.pop("revision")
    api.request.return_value = old
    with pytest.raises(c.CliError, match="revision"):
        c.run(args("reset", "--dry-run"), api)
    assert api.request.call_count == 1


def test_mapping_requires_exact_parent_and_preserves_target(api):
    with pytest.raises(c.CliError, match="--org"):
        c.run(args("mappings", "show", "--scope", "team", "--target", "team-id"), api)
    api.request.assert_not_called()
    api.request.return_value = {"items": [{"scope": "user:other", "destination_id": "dest", "revision": REVISION}], "has_more": False}
    with pytest.raises(c.CliError, match="requested target"):
        c.run(args("mappings", "show", "--scope", "user", "--target", "target-id"), api)


def test_source_and_connection_schema_do_not_accept_billing_substitution():
    with pytest.raises(ValidationError):
        MySelectionRequest(credential_id="connection", expected_account_id="malformed")
    with pytest.raises(ValidationError):
        MappingUpsertRequest(destination_id="dest", expected_destination_revision="malformed")
    wire = RegisterSharedConnectionDestination(source="shared_connection", credential_id="connection", link_to_org_id="org", destination_id="exact")
    assert wire.model_dump()["destination_id"] == "exact"


def test_safe_output_drops_role_and_secret_fields():
    output = c.safe_routing(
        {
            "account_id": ACCOUNT,
            "role_arn": "secret-arn",
            "secret_arn": "secret",
            "external_id": "secret",
            "effective": {"rung": "org", "secret": "value"},
        }
    )
    assert "secret" not in json.dumps(output)


def test_receipt_cannot_cross_actor_or_tenant(api, monkeypatch):
    api.request.return_value = state()
    command = args("reset", "--yes", "--expect-revision", REVISION, "--operation-id", str(uuid.uuid4()))
    assert c.run(command, api)["status"] == "configured"
    monkeypatch.setattr(c.common, "authenticated_scope", lambda: {"identity": "bob", "tenant": "tenant-b"})
    api.request.reset_mock()
    with pytest.raises(c.CliError, match="different routing inputs or scope"):
        c.run(command, api)
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


def test_same_operation_replay_never_overwrites_a_later_route(api):
    api.request.return_value = state()
    command = args("reset", "--yes", "--expect-revision", REVISION, "--operation-id", str(uuid.uuid4()))
    assert c.run(command, api)["status"] == "configured"
    newer = state(revision="b" * 64, own_selection_destination_id="newer", own_selection_account_id="222222222222")
    api.request.return_value = newer
    api.request.reset_mock()
    result = c.run(command, api)
    assert result["status"] == "pending"
    assert result["detail"]["current"]["own_selection_destination_id"] == "newer"
    assert result["detail"]["acknowledged"] is True
    assert all(call.args[0] == "GET" for call in api.request.call_args_list)


@pytest.mark.parametrize(
    "connection", [None, [], "bad", {}, {"credential_id": "connection", "selectable": True, "status": "verified", "account_id": None}]
)
def test_malformed_selection_connection_refused_before_write(api, connection):
    api.request.return_value = state(connections=[connection])
    with pytest.raises(c.CliError) as exc:
        c.run(args("select", "--connection", "connection", "--yes", "--expect-revision", REVISION, "--operation-id", str(uuid.uuid4())), api)
    assert exc.value.code == "invalid_response"
    api.request.assert_called_once_with("GET", c.SELF_ROUTING)


def destination(**changes):
    return {
        "id": "destination",
        "revision": REVISION,
        "account_id": ACCOUNT,
        "usable_for_routing": True,
        "used_by": 0,
        "source_connection_id": "connection",
        "connection_id": None,
        "owner_org_id": "org",
        **changes,
    }


def link_args():
    return args(
        "connection-link",
        "add",
        "--destination",
        "destination",
        "--connection",
        "connection",
        "--yes",
        "--expect-revision",
        REVISION,
        "--operation-id",
        str(uuid.uuid4()),
    )


@pytest.mark.parametrize("reply", [None, {}, [None], [{}], [destination(account_id={})]])
def test_malformed_destination_inventory_refused_before_link(api, reply):
    api.request.return_value = reply
    with pytest.raises(c.CliError) as exc:
        c.run(link_args(), api)
    assert exc.value.code == "invalid_response"
    api.request.assert_called_once_with("GET", c.ROUTING + "/destinations")


@pytest.mark.parametrize("reply", [None, {}, {"destination": None}, {"destination": []}, {"destination": {}}])
def test_malformed_link_ack_is_unknown_without_crashing(api, reply):
    api.request.side_effect = [[destination()], reply, [destination()]]
    result = c.run(link_args(), api)
    assert result["status"] == "pending"
    assert result["detail"]["reason"] == "unknown_mutation_outcome"
    assert sum(call.args[0] == "POST" for call in api.request.call_args_list) == 1


def test_malformed_link_readback_does_not_claim_success(api):
    api.request.side_effect = [[destination()], {"destination": destination(connection_id="connection")}, [None]]
    result = c.run(link_args(), api)
    assert result["status"] == "pending"
    assert result["detail"]["acknowledged"] is True
    assert result["detail"]["readback_unavailable"] is True
