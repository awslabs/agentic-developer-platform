"""The actual Lambda writer's records must bootstrap in the gateway."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore, envelope_digest
from src.agentauth.workload import VerifiedPod

_path = Path(__file__).resolve().parents[3] / "agent-factory/webhook-ingress/lambda/common/agent_authority.py"
_spec = importlib.util.spec_from_file_location("adp_human_dispatch_contract", _path)
webhook = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = webhook
_spec.loader.exec_module(webhook)

_identity_spec = importlib.util.spec_from_file_location("adp_human_dispatch_identity", _path.with_name("identity_resolver.py"))
identity_resolver = importlib.util.module_from_spec(_identity_spec)
sys.modules[_identity_spec.name] = identity_resolver
_identity_spec.loader.exec_module(identity_resolver)


@pytest.fixture
def store(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "authority")
        yield BootstrapStore(table_name="authority", dynamodb_client=ddb)


def resolved_human(**overrides):
    """A provider-confirmed human in the tenant authorizing this dispatch."""
    fields = {
        "tenant_id": "tenant",
        "org_id": "tenant",
        "user_id": "human",
        "user_provisioning_mode": "strict",
        "user_kind": "human",
        "verification_method": "oauth",
    }
    return identity_resolver.ResolvedIdentity(**(fields | overrides))


def event():
    return webhook.VerifiedHumanEvent.from_verified_webhook(
        body=b'{"sender":{"type":"User"},"comment":{"id":1}}',
        event_type="issue_comment",
        resolved=resolved_human(),
        sender={"type": "User"},
        tenant_id="tenant",
        repo="org/repo",
    )


def envelope():
    return {
        "message_id": "original-random-id",
        "tenant_id": "tenant",
        "persona": "developer",
        "arrived_at": "2026-09-13T00:00:00Z",
        "source_ref": {"repo": "org/repo", "issue": 42},
        "correlation": {"parent_invocation_id": "worker-controlled"},
        "payload": {"comment": {"id": 1}},
    }


_SNAPSHOT_NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _report_only_snapshot():
    """A minimal frozen report-only snapshot for the developer persona."""
    from src.agentauth.model_policy import SNAPSHOT_AUDIENCE, SNAPSHOT_SCHEMA_VERSION, ModelPolicySnapshot

    sonnet = "global.anthropic.claude-sonnet-4-6"
    return ModelPolicySnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        tenant_id="tenant",
        principal_kind="human",
        principal_id="human",
        # A saved mapping is deliberately present and DIFFERENT from the
        # directive, so a dropped override would resolve to this instead of
        # failing -- that is what makes the assertions above meaningful.
        mappings={"developer": "global.anthropic.claude-opus-4-1"},
        class_defaults={
            "claude-agent-sdk": {
                "model_id": sonnet,
                "revision": 7,
                "posture": "report_only",
                "posture_revision": 2,
                "harness_contract_revision": "0.3.283",
            }
        },
        persona_contracts={
            "developer": {
                "compatibility_class": "claude-agent-sdk",
                "harness_contract_revision": "0.3.283",
            }
        },
        policy_revision="policy-7",
        allowlist_policy_revision="allowlist-4",
        catalogue_revision="catalogue-3",
        correlation_id="chain-a",
        root_invocation_id="root-a",
        issued_at=_SNAPSHOT_NOW,
        expires_at=_SNAPSHOT_NOW + timedelta(hours=2),
        audience=SNAPSHOT_AUDIENCE,
        source="live",
    )


def test_actual_human_writer_bootstraps_and_ignores_advisory_parent(store):
    now = datetime.now(UTC)
    final = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now)
    pod = VerifiedPod("pod-uid", "worker", "adp-agents", "agent-scaledjob-sa", "10.1.2.3")
    record = store.bind(invocation_id=final["message_id"], digest=envelope_digest(final), pod=pod, now=now)
    assert record.workload_binding == "pod-uid"
    assert record.parent_principal is None
    assert record.flow_id == event().reference_id
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=now)
    assert grant.authority.human_id == "human"
    assert "credential" not in final


def test_actual_human_writer_carries_a_model_directive_to_the_resolver(store):
    """PMM-07: a `/model` directive must survive the real Lambda writer.

    Asserted against the writer the webhook actually calls and the attribute
    names ``model_policy`` actually reads. The regression this pins is silent:
    when the writer dropped these two fields, every request resolved from the
    saved mapping instead, ``resolution_source`` stayed ``principal-mapping``,
    and the user's explicit choice vanished with no refusal anywhere -- the
    resolver could not even reach its ``direct_override_unresolved`` path.
    """
    from src.agentauth.model_policy import resolve_decision

    directive = {
        **envelope(),
        "model_requested": "sonnet46",
        # A published alias: the legacy assignment the worker executes and the
        # canonical *proposed* value agree, which is the ordinary case.
        "model_resolved": "global.anthropic.claude-sonnet-4-6",
        "model_canonical": "global.anthropic.claude-sonnet-4-6",
    }
    final = webhook.provision_human_dispatch(envelope=directive, event=event(), client=store.client)
    execution = store._read("TENANT#tenant", f"EXEC#{final['message_id']}")

    assert execution["direct_model_requested"] == {"S": "sonnet46"}
    assert execution["direct_model_override"] == {"S": "global.anthropic.claude-sonnet-4-6"}

    # The requested alias and the resolved ID stay separate all the way into
    # the decision, so attribution records what the user asked for.
    decision = resolve_decision(
        _report_only_snapshot(),
        invocation_id=final["message_id"],
        persona="developer",
        direct_override=execution["direct_model_override"]["S"],
        direct_requested=execution["direct_model_requested"]["S"],
        now=_SNAPSHOT_NOW,
    )
    assert decision.resolution_source == "explicit-direct"
    assert decision.resolved_model_id == "global.anthropic.claude-sonnet-4-6"
    assert decision.requested_model_id == "sonnet46"
    assert decision.runtime_posture == "report_only"


def test_writer_omits_model_attributes_when_no_directive_was_given(store):
    """An absent directive must stay absent, not become an empty override.

    An empty-string attribute would read back as a falsy override but a
    present key, which is how a "no directive" run could be mistaken for a
    refused one.
    """
    final = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)
    execution = store._read("TENANT#tenant", f"EXEC#{final['message_id']}")

    assert "direct_model_requested" not in execution
    assert "direct_model_override" not in execution


def test_writer_keeps_an_unresolved_directive_visible_as_a_refusal(store):
    """A rejected alias must reach the resolver as a refusal, not a silent default.

    Edge validation sets ``model_resolved=None`` when it cannot resolve the
    alias. Dropping ``model_requested`` too would let the run fall through to
    the saved mapping -- the exact silent substitution the design forbids.
    """
    from src.agentauth.model_policy import ModelPolicyError, resolve_decision

    directive = {
        **envelope(),
        "model_requested": "not-a-real-model",
        "model_resolved": None,
        "model_canonical": None,
    }
    final = webhook.provision_human_dispatch(envelope=directive, event=event(), client=store.client)
    execution = store._read("TENANT#tenant", f"EXEC#{final['message_id']}")

    assert execution["direct_model_requested"] == {"S": "not-a-real-model"}
    assert "direct_model_override" not in execution

    with pytest.raises(ModelPolicyError, match="direct_override_unresolved"):
        resolve_decision(
            _report_only_snapshot(),
            invocation_id=final["message_id"],
            persona="developer",
            direct_override=None,
            direct_requested=execution["direct_model_requested"]["S"],
            now=_SNAPSHOT_NOW,
        )


def test_writer_proposes_only_the_published_value_while_legacy_still_executes(store):
    """PMM-07: the *proposed* override is the canonical one, never the legacy one.

    ``report_only`` runs two answers at once: the legacy assignment the worker
    actually executes, and the strict proposed resolution the gateway records.
    A regional ID the authority never published resolves on the legacy path (so
    the user's run is unchanged) but must NOT be written as a proposed override
    -- doing so would launder an unpublished model into a "proposed" decision
    and make the resolver claim ``explicit-direct`` for something it never
    selected. The correct proposed outcome is the refusal below.

    This is the writer half of the silent-substitution repair; the executed
    half is pinned in the worker's ``test_model_directive_end_to_end.py``.
    """
    from src.agentauth.model_policy import ModelPolicyError, resolve_decision

    requested = "us.anthropic.claude-opus-4-6-v1"
    directive = {
        **envelope(),
        "model_requested": requested,
        # Legacy execution keeps the requested value ...
        "model_resolved": requested,
        # ... while the strict published check refuses it.
        "model_canonical": None,
    }
    final = webhook.provision_human_dispatch(envelope=directive, event=event(), client=store.client)
    execution = store._read("TENANT#tenant", f"EXEC#{final['message_id']}")

    assert execution["direct_model_requested"] == {"S": requested}
    assert "direct_model_override" not in execution

    with pytest.raises(ModelPolicyError, match="direct_override_unresolved"):
        resolve_decision(
            _report_only_snapshot(),
            invocation_id=final["message_id"],
            persona="developer",
            direct_override=None,
            direct_requested=execution["direct_model_requested"]["S"],
            now=_SNAPSHOT_NOW,
        )


def test_writer_never_records_the_legacy_value_as_the_proposed_override(store):
    """Guards the specific line that reads ``model_canonical``, not ``model_resolved``.

    Constructed so the two disagree: if the writer ever went back to reading
    ``model_resolved``, ``direct_model_override`` would appear with the
    unpublished regional ID and this test would fail.
    """
    requested = "eu.anthropic.claude-sonnet-4-6"
    directive = {
        **envelope(),
        "model_requested": requested,
        "model_resolved": requested,
        "model_canonical": None,
    }
    final = webhook.provision_human_dispatch(envelope=directive, event=event(), client=store.client)
    execution = store._read("TENANT#tenant", f"EXEC#{final['message_id']}")

    assert "direct_model_override" not in execution
    assert requested not in str(execution.get("direct_model_override", ""))


def test_webhook_retry_preserves_identity_digest_and_authority_expiry(store):
    now = datetime.now(UTC)
    first = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now)
    second_input = {**envelope(), "message_id": "another-random-id", "arrived_at": "2026-09-14T00:00:00Z"}
    second = webhook.provision_human_dispatch(envelope=second_input, event=event(), client=store.client, now=now + timedelta(hours=2))
    assert first == second
    grant = store.authority.load_grant(principal=f"{first['message_id']}#1", tenant_id="tenant")
    assert grant.expires_at == (now + timedelta(days=7)).replace(microsecond=0)


def test_changed_dispatch_intent_cannot_reuse_human_event(store):
    webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)
    changed = envelope()
    changed["source_ref"] = {"repo": "org/repo", "issue": 999}
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=changed, event=event(), client=store.client)


def test_revoked_grant_does_not_publish_again_on_retry(store):
    final = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{final['message_id']}#1"}},
        UpdateExpression="SET revoked = :true",
        ExpressionAttributeValues={":true": {"BOOL": True}},
    )
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)


def test_expired_human_event_cannot_extend_itself_by_redelivery(store):
    now = datetime.now(UTC)
    webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now)
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now + timedelta(days=8))


@pytest.mark.parametrize("user_kind,sender_type", [("bot", "Bot"), ("service", "User"), ("human", "Bot")])
def test_worker_or_service_claim_cannot_create_human_event(user_kind, sender_type):
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.VerifiedHumanEvent.from_verified_webhook(
            body=b"body",
            event_type="issue_comment",
            resolved=resolved_human(user_kind=user_kind, user_id="claimed-human"),
            sender={"type": sender_type},
            tenant_id="tenant",
            repo="org/repo",
        )


@pytest.mark.parametrize("method", [None, "", "self_asserted", "channel_placement", "magic_link", "unknown_method"])
def test_unproven_human_resolution_still_cannot_authorize_dispatch(store, method):
    with pytest.raises(webhook.AuthorityProvisionError, match="proven identity link"):
        webhook.VerifiedHumanEvent.from_verified_webhook(
            body=b"body",
            event_type="issue_comment",
            resolved=resolved_human(verification_method=method),
            sender={"type": "User"},
            tenant_id="tenant",
            repo="org/repo",
        )
    assert store.client.scan(TableName=store.table)["Count"] == 0


def test_legacy_human_resolution_without_provenance_cannot_authorize_dispatch(store):
    with pytest.raises(webhook.AuthorityProvisionError, match="proven identity link"):
        webhook.VerifiedHumanEvent.from_verified_webhook(
            body=b"body",
            event_type="issue_comment",
            resolved=SimpleNamespace(user_kind="human", user_id="human"),
            sender={"type": "User"},
            tenant_id="tenant",
            repo="org/repo",
        )
    assert store.client.scan(TableName=store.table)["Count"] == 0


@pytest.mark.parametrize("scope", [{"tenant_id": "foreign"}, {"org_id": "foreign"}])
def test_proven_human_resolution_must_match_dispatch_tenant(store, scope):
    with pytest.raises(webhook.AuthorityProvisionError, match="tenant-consistent identity link"):
        webhook.VerifiedHumanEvent.from_verified_webhook(
            body=b"body",
            event_type="issue_comment",
            resolved=resolved_human(**scope),
            sender={"type": "User"},
            tenant_id="tenant",
            repo="org/repo",
        )
    assert store.client.scan(TableName=store.table)["Count"] == 0


@pytest.fixture
def child_dispatch(store):
    from src.agentauth.bootstrap import issue_bound_credential
    from src.agentauth.composition import build_authorization_service
    from src.agentauth.dispatch import DispatchRequest, DispatchService
    from src.agentauth.run_credential import CREDENTIAL_KEY_ENV

    env = {CREDENTIAL_KEY_ENV: "gateway-test-key-not-shared-with-workers"}
    original = envelope()
    original["persona"] = "operations"
    original["source_ref"]["installation_id"] = 123
    final = webhook.provision_human_dispatch(envelope=original, event=event(), client=store.client)
    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    record = store.bind(invocation_id=final["message_id"], digest=envelope_digest(final), pod=pod, now=datetime.now(UTC))
    credential = issue_bound_credential(record, now=datetime.now(UTC), env=env)["credential"]
    store.client.create_table(
        TableName="events",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="dispatch.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    policy = build_authorization_service(
        authority_table=store.table, events_table="events", dynamodb_client=store.client, dynamodb_resource=resource, env=env
    )
    service = DispatchService(store=store, policy=policy, queue_url=queue, events_table="events", sqs=sqs)
    body = DispatchRequest(
        persona="developer", target={"repo": "org/repo", "issue": 42}, request_id="same-request", reason="Implement the approved story"
    )
    return SimpleNamespace(service=service, body=body, credential=credential, invocation=final["message_id"], sqs=sqs, queue=queue)


def send_child(context, body=None):
    return context.service.dispatch(body=body or context.body, credential_token=context.credential, workload_binding="pod-a")


def test_authorized_dispatch_queues_one_bootstrappable_child_and_recovers_retry(store, child_dispatch):
    import json

    first = send_child(child_dispatch)
    assert send_child(child_dispatch) == first
    messages = child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]
    assert len(messages) == 1
    child_envelope = json.loads(messages[0]["Body"])
    record = store.bind(
        invocation_id=first["invocation_id"],
        digest=envelope_digest(child_envelope),
        pod=VerifiedPod("pod-b", "worker-b", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
        now=datetime.now(UTC),
    )
    assert record.parent_principal == f"{child_dispatch.invocation}#1"
    assert record.repo == "org/repo"
    assert child_envelope["actor"]["kind"] == "service"
    assert child_envelope["actor"]["user_id"] != "human"
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert {str(a) for a in grant.allowed_actions} == {"monitor", "dispatch"}
    assert {str(a) for a in grant.delegable_actions} == {"monitor"}
    parent = store.authority.load_grant(principal=f"{child_dispatch.invocation}#1", tenant_id="tenant")
    assert store.authority.active_dispatch_count(grant_id=parent.grant_id, tenant_id="tenant") == 1


def test_authenticated_parent_depth_ignores_a_deep_sibling(store, child_dispatch):
    """Issue #5365 CD-1: depth comes from the credential-bound caller only."""
    store.client.update_item(
        TableName=store.table,
        Key={
            "pk": {"S": "TENANT#tenant"},
            "sk": {"S": f"EXEC#{child_dispatch.invocation}"},
        },
        UpdateExpression="SET chain_depth = :depth",
        ExpressionAttributeValues={":depth": {"N": "1"}},
    )
    store.client.put_item(
        TableName="events",
        Item={
            "event_id": {"S": "unrelated-sibling"},
            "arrived_at": {"S": "2026-09-18T00:00:00Z"},
            "correlation_id": {"S": event().reference_id},
            "chain_depth": {"N": "7"},
        },
    )

    result = send_child(child_dispatch)

    child = store._read("TENANT#tenant", f"EXEC#{result['invocation_id']}")
    assert child["chain_depth"] == {"N": "2"}
    assert child["parent_principal"] == {"S": f"{child_dispatch.invocation}#1"}
    event_row = store.client.query(
        TableName="events",
        KeyConditionExpression="event_id = :id",
        ExpressionAttributeValues={":id": {"S": result["invocation_id"]}},
    )["Items"][0]
    assert event_row["chain_depth"] == {"N": "2"}
    assert event_row["credential_chain_depth"] == {"N": "2"}
    assert event_row["parent_invocation_id"] == {"S": child_dispatch.invocation}


def test_two_sequential_dispatches_do_not_compound_depth(store, child_dispatch):
    """Issue #5365 CD-2: a coordinator's authority does not decay as it fans out.

    The defect charged the newest row anywhere on the shared chain, so a
    coordinator's second dispatch inherited the depth of its own first child.
    Both children are siblings at the caller's depth + 1, forever.
    """
    depths = []
    for index in range(2):
        result = send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": f"story-{index}"}))
        row = store._read("TENANT#tenant", f"EXEC#{result['invocation_id']}")
        depths.append(row["chain_depth"]["N"])
        assert row["parent_principal"] == {"S": f"{child_dispatch.invocation}#1"}
        # Complete the first child before requesting the second, so this is a
        # sequential fan-out rather than two concurrent reservations.
        store.authority.release_dispatch(grant_id=row["parent_grant_id"]["S"], tenant_id="tenant", reservation_id=row["dispatch_reservation_id"]["S"])
    assert depths == ["1", "1"]
    # The caller's own depth is untouched by having dispatched.
    assert store._read("TENANT#tenant", f"EXEC#{child_dispatch.invocation}").get("chain_depth", {"N": "0"}) == {"N": "0"}


def test_dispatch_parent_is_not_the_newest_row_on_the_chain(store, child_dispatch):
    """Issue #5365 CD-3: a non-head parent keeps its own depth.

    The caller's record is read by primary key from its run credential, so a
    newer unrelated row on the same correlation chain cannot become the parent.
    """
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"EXEC#{child_dispatch.invocation}"}},
        UpdateExpression="SET chain_depth = :depth",
        ExpressionAttributeValues={":depth": {"N": "2"}},
    )
    store.client.put_item(
        TableName="events",
        Item={
            "event_id": {"S": "newer-unrelated-descendant"},
            "arrived_at": {"S": "2026-12-31T23:59:59Z"},
            "correlation_id": {"S": event().reference_id},
            "chain_depth": {"N": "7"},
        },
    )

    result = send_child(child_dispatch)

    child = store._read("TENANT#tenant", f"EXEC#{result['invocation_id']}")
    assert child["chain_depth"] == {"N": "3"}
    assert child["parent_principal"] == {"S": f"{child_dispatch.invocation}#1"}


def test_forged_or_replayed_ancestor_cannot_be_selected(store, child_dispatch, monkeypatch):
    """Issue #5365: the protected route has no caller-selectable ancestry.

    A forged parent pointer is refused by schema, and replaying a captured
    request cannot re-parent the child onto a shallower ancestor.
    """
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pydantic import ValidationError

    from src.agentauth.dispatch import DispatchRequest
    from src.agentauth.routes import AgentRuntime, get_agent_runtime, router

    # There is no field to forge: the request model refuses unknown ancestry.
    for forged in ("parent_invocation_id", "chain_depth", "parent_principal"):
        with pytest.raises(ValidationError):
            DispatchRequest(persona="developer", target={"repo": "org/repo", "issue": 42}, request_id="forge", **{forged: "1"})

    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    runtime = AgentRuntime(
        store=store, workloads=SimpleNamespace(verify=lambda token: pod), env=child_dispatch.service.policy._env, dispatcher=child_dispatch.service
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    headers = {
        "X-Caller-Identity": "shared-worker-role",
        "X-Adp-Workload-Token": "verified-by-tokenreview",
        "X-Adp-Run-Credential": child_dispatch.credential,
    }
    client = TestClient(app)
    body = child_dispatch.body.model_dump()
    assert client.post("/internal/v1/agent/dispatch", json={**body, "parent_invocation_id": "shallow-root"}, headers=headers).status_code == 422

    # Replaying the exact accepted request returns the same child, so a captured
    # request cannot be reused to mint a second, differently-parented run.
    first = client.post("/internal/v1/agent/dispatch", json=body, headers=headers)
    assert first.status_code == 202
    replay = client.post("/internal/v1/agent/dispatch", json=body, headers=headers)
    assert replay.status_code == 202
    assert replay.json() == first.json()
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_authenticated_ninth_generation_is_refused(store, child_dispatch):
    """Issue #5365 CD-4: the per-run route keeps the eight-generation cap."""
    from src.agentauth.bootstrap import BootstrapRefusedError

    store.client.update_item(
        TableName=store.table,
        Key={
            "pk": {"S": "TENANT#tenant"},
            "sk": {"S": f"EXEC#{child_dispatch.invocation}"},
        },
        UpdateExpression="SET chain_depth = :depth",
        ExpressionAttributeValues={":depth": {"N": "8"}},
    )

    with pytest.raises(BootstrapRefusedError, match="outside delegated work"):
        send_child(child_dispatch)
    assert not child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages")


def test_dispatch_exact_intent_conflict_and_scope_refused_before_queue(child_dispatch):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.dispatch import DispatchRequest
    from src.agentauth.policy import PolicyError

    send_child(child_dispatch)
    with pytest.raises(PolicyError, match="different content"):
        send_child(child_dispatch, child_dispatch.body.model_copy(update={"reason": "changed instructions"}))
    for target in ({"repo": "another/repo", "issue": 42}, {"repo": "org/repo", "issue": 99}):
        with pytest.raises(BootstrapRefusedError):
            send_child(child_dispatch, DispatchRequest(persona="developer", target=target, request_id="outside"))
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_dispatch_full_capacity_still_recovers_an_existing_outcome(child_dispatch):
    from src.agentauth.policy import PolicyError

    first = send_child(child_dispatch)
    send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": "second"}))
    assert send_child(child_dispatch) == first
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": "third"}))
    assert exc.value.status_code == 429


def test_dispatch_lost_send_response_reuses_same_sqs_dedup_id(child_dispatch, monkeypatch):
    from botocore.exceptions import BotoCoreError

    from src.agentauth.policy import PolicyError

    actual_send = child_dispatch.sqs.send_message

    def lose_response(**kwargs):
        actual_send(**kwargs)
        raise BotoCoreError()

    monkeypatch.setattr(child_dispatch.sqs, "send_message", lose_response)
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch)
    assert exc.value.status_code == 503
    monkeypatch.setattr(child_dispatch.sqs, "send_message", actual_send)
    send_child(child_dispatch)
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_ambiguous_send_is_not_reissued_after_dedup_window(child_dispatch, monkeypatch):
    from botocore.exceptions import BotoCoreError

    from src.agentauth.policy import PolicyError

    def fail(**kwargs):
        raise BotoCoreError()

    monkeypatch.setattr(child_dispatch.sqs, "send_message", fail)
    with pytest.raises(PolicyError):
        send_child(child_dispatch)
    child_dispatch.service.now = lambda: datetime.now(UTC) + timedelta(seconds=241)
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch)
    assert exc.value.status_code == 409


def test_revoked_parent_blocks_queued_child_bootstrap(store, child_dispatch):
    import json

    from src.agentauth.bootstrap import BootstrapRefusedError

    first = send_child(child_dispatch)
    child_envelope = json.loads(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue)["Messages"][0]["Body"])
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{child_dispatch.invocation}#1"}},
        UpdateExpression="SET revoked = :true",
        ExpressionAttributeValues={":true": {"BOOL": True}},
    )
    with pytest.raises(BootstrapRefusedError):
        store.bind(
            invocation_id=first["invocation_id"],
            digest=envelope_digest(child_envelope),
            pod=VerifiedPod("pod-b", "worker-b", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
            now=datetime.now(UTC),
        )
    assert store.authority.load_execution(invocation_id=first["invocation_id"], tenant_id="tenant").workload_binding is None


def test_real_dispatch_http_contract_creates_child(store, child_dispatch, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.agentauth.routes import AgentRuntime, get_agent_runtime, router

    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    runtime = AgentRuntime(
        store=store, workloads=SimpleNamespace(verify=lambda token: pod), env=child_dispatch.service.policy._env, dispatcher=child_dispatch.service
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    headers = {
        "X-Caller-Identity": "shared-worker-role",
        "X-Adp-Workload-Token": "verified-by-tokenreview-in-other-tests",
        "X-Adp-Run-Credential": child_dispatch.credential,
    }
    client = TestClient(app)
    result = client.post("/internal/v1/agent/dispatch", json=child_dispatch.body.model_dump(), headers=headers)
    assert result.status_code == 202
    assert result.json()["status"] == "accepted"
    assert result.headers["cache-control"] == "no-store"
    forged = {**child_dispatch.body.model_dump(), "parent_invocation_id": "forged-other-run"}
    assert client.post("/internal/v1/agent/dispatch", json=forged, headers=headers).status_code == 422
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_dispatch_reservation_rechecks_grant_atomically(store, child_dispatch, monkeypatch):
    from src.agentauth.policy import PolicyError

    actual = store.client.transact_write_items

    def revoke_before_commit(**kwargs):
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{child_dispatch.invocation}#1"}},
            UpdateExpression="SET revoked = :true",
            ExpressionAttributeValues={":true": {"BOOL": True}},
        )
        return actual(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", revoke_before_commit)
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch)
    assert exc.value.status_code == 409
    assert not child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages")
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 0


def test_committed_reservation_lost_response_is_recovered(store, child_dispatch, monkeypatch):
    from botocore.exceptions import BotoCoreError

    actual = store.client.transact_write_items

    def lose_response(**kwargs):
        actual(**kwargs)
        raise BotoCoreError()

    monkeypatch.setattr(store.client, "transact_write_items", lose_response)
    first = send_child(child_dispatch)
    assert send_child(child_dispatch) == first
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 1


def test_total_dispatch_budget_survives_reservation_release(store, child_dispatch):
    from src.agentauth.policy import PolicyError

    for index in range(4):
        result = send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": str(index)}))
        row = store._read("TENANT#tenant", f"EXEC#{result['invocation_id']}")
        store.authority.release_dispatch(grant_id=row["parent_grant_id"]["S"], tenant_id="tenant", reservation_id=row["dispatch_reservation_id"]["S"])
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": "over-total-budget"}))
    assert exc.value.status_code == 409


def test_codex_pm_grant_uses_existing_root_coordinator_dispatch(store):
    final = webhook.provision_human_dispatch(envelope={**envelope(), "persona": "agent-codex-pm"}, event=event(), client=store.client)
    grant = store._read("TENANT#tenant", f"GRANT#{final['message_id']}#1")
    assert grant["dispatch_personas"] == {"SS": ["agent-codex-developer"]}
    assert grant["dispatch_repository_scope"] == {"S": "org/repo"}
    assert "dispatch" in grant["allowed_actions"]["SS"]
    assert "dispatch" in grant["delegable_actions"]["SS"]
    assert grant["work_item_issue"] == {"N": "42"}
