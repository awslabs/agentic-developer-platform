"""Canonical model policy requests, lost acknowledgements and cost certainty."""

import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), Path(__file__).parents[2] / "cli" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


policy = load("adp-model-policy.py")
models = load("adp-models.py")
CLASS = "claude-agent-sdk"
OP = "5d8a9512-5d7f-44aa-9d69-7aeb1c76bfe1"


def state(family="posture", revision=1):
    if family == "default":
        return dict(
            compatibility_class=CLASS,
            candidate_default_model_id="model",
            active_default_model_id="model",
            harness_contract_revision="h1",
            revision=revision,
        )
    return dict(
        compatibility_class=CLASS,
        posture="enforcing",
        posture_revision=revision,
        supported_postures=["disabled", "report_only", "enforcing"],
        propagation_bound_seconds=60,
    )


def args(action="set", *extra):
    flags = ["--to-version", "1"] if action == "rollback" else ["--posture", "enforcing"]
    return policy.parser().parse_args(["posture", action, "--compatibility-class", CLASS, *flags, *extra])


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(policy.common, "ensure_can_mutate", Mock())
    return Mock()


def test_dry_run_overrides_yes(client):
    client.request.side_effect = [state(), state("default")]
    result = policy.execute(args("set", "--dry-run", "--yes"), client)
    assert result["status"] == "dry_run"
    assert all(c.args[0] == "GET" for c in client.request.call_args_list)
    policy.common.ensure_can_mutate.assert_not_called()


def test_set_replays_same_operation_body_and_checks_ack(client):
    client.request.side_effect = [state(), state("default"), state(revision=2), state(revision=2)]
    result = policy.execute(args("set", "--yes", "--expect-version", "1", "--operation-id", OP, "--reason", "reviewed"), client)
    assert result["status"] == "ok"
    method, path, body = client.request.call_args_list[2].args
    assert method == "PUT" and path.endswith(CLASS)
    assert body == dict(posture="enforcing", expected_revision=1, operation_id=OP, reason="reviewed")


@pytest.mark.parametrize(
    "ack",
    [
        None,
        {},
        {"compatibility_class": "foreign"},
        {"compatibility_class": CLASS, "posture_revision": 2, "posture": "enforcing", "supported_postures": [{}], "propagation_bound_seconds": 60},
    ],
)
def test_malformed_ack_is_unknown_never_resent(client, ack):
    client.request.side_effect = [state(), state("default"), ack]
    result = policy.execute(args("set", "--yes", "--expect-version", "1", "--operation-id", OP, "--reason", "reviewed"), client)
    assert result["status"] == "pending"
    assert result["detail"]["outcome"] == "unknown"
    assert client.request.call_count == 3


def test_rollback_sends_historical_version_not_client_posture(client):
    history = dict(compatibility_class=CLASS, posture="enforcing", posture_revision=1, audit_id="audit")
    client.request.side_effect = [state(revision=2), state("default"), history, state(revision=3), state(revision=3)]
    result = policy.execute(args("rollback", "--yes", "--expect-version", "2", "--operation-id", OP, "--reason", "restore"), client)
    assert result["status"] == "ok"
    method, path, body = client.request.call_args_list[3].args
    assert method == "POST" and path.endswith("/rollback")
    assert body["historical_revision"] == 1 and "posture" not in body


def test_bad_prewrite_state_refuses(client):
    row = state()
    row["compatibility_class"] = "foreign"
    client.request.return_value = row
    with pytest.raises(policy.common.CliError):
        policy.execute(args("set", "--dry-run"), client)
    assert client.request.call_count == 1


def test_managed_catalog_uses_selected_principal(client):
    client.machine = False
    client.request.return_value = dict(tenant_id="org", persona_key="architect", compatibility_class=CLASS, models=[])
    parsed = models.parser().parse_args(["catalog", "--persona", "architect", "--service-principal", "sp"])
    models.run(parsed, client)
    assert client.request.call_args.args == ("GET", "/service-principals/sp/persona-models/catalog?persona_key=architect")


def test_cost_wire_schema_preserves_unknown_and_tenant(client):
    from src.admin.persona_models.schemas import PersonaCostResponse

    report = PersonaCostResponse(
        tenant_id="org",
        principal_kind="human",
        principal_id="user",
        chain_id=None,
        status="unknown",
        amount_usd=None,
        call_count=1,
        unpriced_call_count=1,
        estimated_call_count=0,
        estimate_reasons=[],
        partial=True,
        scope="owner",
        caveat="unknown amount",
        entries=[],
        preferences=[],
    ).model_dump(mode="json")
    client.machine = False
    client.request.side_effect = [dict(tenant_id="org", principal_kind="human", principal_id="user"), report]
    parsed = models.parser().parse_args(["costs", "--persona", "architect"])
    result = models.run(parsed, client)
    assert result["detail"]["amount_usd"] is None
    assert result["detail"]["aggregate_scope"] == "all_personas_for_selected_owner_and_chain"
    report["tenant_id"] = "foreign"
    client.request.side_effect = [dict(tenant_id="org", principal_kind="human", principal_id="user"), report]
    with pytest.raises(models.CliError):
        models.run(parsed, client)


@pytest.mark.parametrize("evidence", [None, {}, {"account_id": "123"}])
def test_ready_default_without_real_evidence_is_refused(client, evidence):
    current = state("default")
    preview = dict(
        compatibility_class=CLASS,
        canonical_model_id="model",
        current=current,
        current_posture=dict(posture="enforcing", posture_revision=1),
        ready=True,
        evidence=evidence,
    )
    client.request.side_effect = [current, preview]
    parsed = policy.parser().parse_args(["default", "set", "--compatibility-class", CLASS, "--model", "model", "--dry-run"])
    with pytest.raises(policy.common.CliError, match="evidence"):
        policy.execute(parsed, client)
    assert client.request.call_count == 2
