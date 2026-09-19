"""PMM-06 snapshot determinism, trusted-root identity and adversarial cases."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from moto import mock_aws

from src.agentauth.envelope import (
    MODEL_POLICY_AUDIENCE,
    SIGNING_KEY_ENV,
    SIGNING_KEY_ID_ENV,
    verify_envelope,
)
from src.agentauth.grants import AuthorityReference, DelegatedGrant
from src.agentauth.model_policy import (
    SNAPSHOT_AUDIENCE,
    SNAPSHOT_SCHEMA_VERSION,
    ModelPolicyError,
    ModelPolicySnapshot,
    bootstrap_model_policy,
    build_root_snapshot,
    canonical_json,
    ensure_snapshot_for_admission,
    ensure_snapshot_report_only,
    parse_snapshot,
    policy_digest,
    resolve_decision,
)
from src.shared.models.organization import User
from src.shared.models.persona_models import (
    PersonaModelPolicySetting,
    PersonaModelPreference,
    ServicePrincipal,
    ServicePrincipalAlias,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
OPUS = "global.anthropic.claude-opus-5"
SONNET = "global.anthropic.claude-sonnet-4-6"
HAIKU = "global.anthropic.claude-haiku-4-5-20251001-v1:0"


def snapshot(**changes) -> ModelPolicySnapshot:
    contracts = {
        persona: {
            "compatibility_class": "claude-agent-sdk",
            "harness_contract_revision": "0.3.220",
        }
        for persona in ("architect", "developer", "reviewer")
    }
    values = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "tenant_id": "tenant-a",
        "principal_kind": "human",
        "principal_id": "user-a",
        "mappings": {"architect": OPUS, "developer": SONNET, "reviewer": HAIKU},
        "class_defaults": {
            "claude-agent-sdk": {
                "model_id": SONNET,
                "revision": 7,
                "posture": "report_only",
                "posture_revision": 2,
                "harness_contract_revision": "0.3.220",
            }
        },
        "persona_contracts": contracts,
        "policy_revision": "policy-7",
        "allowlist_policy_revision": "allowlist-4",
        "catalogue_revision": "catalogue-3",
        "correlation_id": "chain-a",
        "root_invocation_id": "root-a",
        "issued_at": NOW,
        "expires_at": NOW + timedelta(hours=2),
        "audience": SNAPSHOT_AUDIENCE,
        "source": "live",
    }
    values.update(changes)
    return ModelPolicySnapshot(**values)


def test_three_hops_select_distinct_models_from_one_root_snapshot():
    policy = snapshot()

    decisions = {
        persona: resolve_decision(policy, invocation_id=f"run-{persona}", persona=persona, now=NOW)
        for persona in ("architect", "developer", "reviewer")
    }

    assert {decision.snapshot_digest for decision in decisions.values()} == {policy_digest(policy.to_dict())}
    assert [decisions[key].resolved_model_id for key in ("architect", "developer", "reviewer")] == [OPUS, SONNET, HAIKU]
    assert all(decision.resolution_source == "principal-mapping" for decision in decisions.values())


def test_absent_mapping_uses_only_its_compatibility_class_default():
    policy = snapshot(mappings={"architect": OPUS})
    decision = resolve_decision(policy, invocation_id="run-review", persona="reviewer", now=NOW)

    assert decision.resolved_model_id == SONNET
    assert decision.requested_model_id is None
    assert decision.resolution_source == "system-default"
    assert (decision.runtime_posture, decision.posture_revision) == ("report_only", 2)


def test_broken_mapping_never_falls_through_to_default():
    policy = snapshot(mappings={"reviewer": "unknown.latest"})
    with pytest.raises(ModelPolicyError, match="model_unavailable"):
        resolve_decision(policy, invocation_id="run-review", persona="reviewer", now=NOW)


def test_direct_override_applies_only_when_supplied_for_this_hop():
    policy = snapshot(mappings={"developer": OPUS})

    direct = resolve_decision(
        policy,
        invocation_id="root-developer",
        persona="developer",
        direct_override=SONNET,
        direct_requested="sonnet46",
        now=NOW,
    )
    child = resolve_decision(policy, invocation_id="child-developer", persona="developer", now=NOW)

    assert (direct.resolved_model_id, direct.resolution_source) == (SONNET, "explicit-direct")
    assert direct.requested_model_id == "sonnet46"
    assert (child.resolved_model_id, child.resolution_source) == (OPUS, "principal-mapping")
    assert child.requested_model_id == OPUS


def test_unresolved_direct_override_is_reported_as_refusal_not_defaulted():
    with pytest.raises(ModelPolicyError, match="direct_override_unresolved"):
        resolve_decision(
            snapshot(mappings={}),
            invocation_id="run-developer",
            persona="developer",
            direct_requested="not-a-real-model",
            now=NOW,
        )


@pytest.mark.parametrize(
    "class_policy",
    [
        {"model_id": SONNET, "posture": "enforcing", "posture_revision": 3},
        {"model_id": SONNET, "posture": "report_only", "posture_revision": 0},
        {"model_id": SONNET, "posture": "unknown", "posture_revision": 3},
    ],
)
def test_partial_slice_never_issues_load_bearing_or_unknown_posture_decision(class_policy):
    with pytest.raises(ModelPolicyError, match="runtime_posture_unsupported"):
        resolve_decision(
            snapshot(class_defaults={"claude-agent-sdk": class_policy}),
            invocation_id="run-developer",
            persona="developer",
            now=NOW,
        )


def test_unknown_persona_and_missing_class_default_fail_distinctly():
    with pytest.raises(ModelPolicyError, match="persona_incompatible"):
        resolve_decision(snapshot(), invocation_id="run-x", persona="attacker-persona", now=NOW)
    with pytest.raises(ModelPolicyError, match="class_default_unavailable"):
        resolve_decision(snapshot(mappings={}, class_defaults={}), invocation_id="run-review", persona="reviewer", now=NOW)


def test_snapshot_tampering_and_cross_tenant_replay_are_distinct_refusals():
    value = snapshot().to_dict()
    raw = canonical_json(value).decode()
    digest = policy_digest(value)
    assert parse_snapshot(raw, digest, tenant_id="tenant-a").principal_id == "user-a"

    altered = json.loads(raw)
    altered["principal_id"] = "victim"
    with pytest.raises(ModelPolicyError, match="snapshot_altered"):
        parse_snapshot(canonical_json(altered).decode(), digest, tenant_id="tenant-a")
    with pytest.raises(ModelPolicyError, match="snapshot_cross_tenant"):
        parse_snapshot(raw, digest, tenant_id="tenant-b")


class _Store:
    def __init__(self, client):
        self.client = client
        self.table = "authority"

    def _read(self, pk, sk):
        return self.client.get_item(
            TableName=self.table,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")


@pytest.fixture
def policy_store():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        yield _Store(client)


def _put(store, item):
    store.client.put_item(TableName=store.table, Item=item)


@pytest.mark.asyncio
async def test_child_inherits_exact_parent_snapshot_without_rereading_preferences(db_session, policy_store):
    root = snapshot().to_dict()
    raw, digest = canonical_json(root).decode(), policy_digest(root)
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#root-a"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "active"},
            "model_policy_snapshot": {"S": raw},
            "model_policy_snapshot_digest": {"S": digest},
        },
    )
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#child-a"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "pending"},
            "parent_principal": {"S": "root-a#1"},
        },
    )
    _put(
        policy_store,
        {
            "pk": {"S": "INVOCATION#child-a"},
            "sk": {"S": "DISPATCH"},
            "tenant_id": {"S": "tenant-a"},
        },
    )

    result = await ensure_snapshot_for_admission(db_session, store=policy_store, invocation_id="child-a", now=NOW)
    child = policy_store._read("TENANT#tenant-a", "EXEC#child-a")

    assert result == {"status": "available", "snapshot_digest": digest, "root_invocation_id": "root-a"}
    assert child["model_policy_snapshot"] == {"S": raw}
    assert child["model_policy_snapshot_digest"] == {"S": digest}


@pytest.mark.asyncio
async def test_report_only_missing_parent_is_evidence_not_dispatch_failure(db_session, policy_store):
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#child-a"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "pending"},
            "parent_principal": {"S": "missing#1"},
        },
    )
    _put(
        policy_store,
        {
            "pk": {"S": "INVOCATION#child-a"},
            "sk": {"S": "DISPATCH"},
            "tenant_id": {"S": "tenant-a"},
        },
    )

    assert await ensure_snapshot_report_only(db_session, store=policy_store, invocation_id="child-a") == {
        "status": "unavailable",
        "reason": "parent_snapshot_missing",
    }


class _RootStore:
    def __init__(self, authority):
        self.authority = authority

    def _read(self, pk, sk):
        return self.authority if sk == "AUTHORITY#authority-a" else None


def _grant(kind: str, human_id: str = "human-sub") -> DelegatedGrant:
    return DelegatedGrant(
        grant_id="grant-root-a",
        tenant_id="tenant-a",
        principal="root-a#1",
        authority=AuthorityReference(kind, "authority-a", human_id, "tenant-a"),
        allowed_actions=frozenset(),
        expires_at=NOW + timedelta(hours=4),
    )


@pytest.mark.asyncio
async def test_human_root_snapshot_uses_canonical_user_and_frozen_db_rows(db_session):
    db_session.add(User(id="user-a", org_id="tenant-a", team_id="team-a", email="a@example.test", cognito_sub="human-sub"))
    db_session.add(
        PersonaModelPreference(
            id="pref-a",
            org_id="tenant-a",
            principal_kind="human",
            principal_source="self",
            principal_id="user-a",
            persona_key="developer",
            canonical_model_id=OPUS,
            requested_alias="opus",
            revision=3,
            updated_by="user-a",
            updated_by_source="self",
        )
    )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=4,
            posture_revision=2,
            enforcement_posture="report_only",
        )
    )
    await db_session.flush()

    built = await build_root_snapshot(
        db_session,
        store=_RootStore(
            {
                "authority_kind": {"S": "github_event"},
                "human_id": {"S": "human-sub"},
            }
        ),
        invocation_id="root-a",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-a"}},
        grant=_grant("github_event"),
        now=NOW,
    )

    assert (built.principal_kind, built.principal_id) == ("human", "user-a")
    assert built.mappings == {"developer": OPUS}
    assert built.class_defaults["claude-agent-sdk"]["model_id"] == SONNET


@pytest.mark.asyncio
async def test_report_only_mapping_snapshot_does_not_require_unproven_active_default(
    db_session,
):
    db_session.add(
        User(
            id="user-no-default",
            org_id="tenant-a",
            team_id="team-a",
            email="no-default@example.test",
            cognito_sub="human-no-default",
        )
    )
    db_session.add(
        PersonaModelPreference(
            id="pref-no-default",
            org_id="tenant-a",
            principal_kind="human",
            principal_source="self",
            principal_id="user-no-default",
            persona_key="developer",
            canonical_model_id=SONNET,
            requested_alias="sonnet46",
            revision=1,
            updated_by="user-no-default",
            updated_by_source="self",
        )
    )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=None,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
        )
    )
    await db_session.flush()

    built = await build_root_snapshot(
        db_session,
        store=_RootStore(
            {
                "authority_kind": {"S": "github_event"},
                "human_id": {"S": "human-no-default"},
            }
        ),
        invocation_id="root-no-default",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-no-default"}},
        grant=_grant("github_event", "human-no-default"),
        now=NOW,
    )

    decision = resolve_decision(
        built,
        invocation_id="run-developer",
        persona="developer",
        now=NOW,
    )
    assert decision.resolved_model_id == SONNET
    assert decision.resolution_source == "principal-mapping"
    assert built.class_defaults["claude-agent-sdk"]["model_id"] is None


@pytest.mark.asyncio
async def test_service_root_resolves_verified_alias_to_canonical_principal(db_session):
    db_session.add(
        ServicePrincipal(
            canonical_service_principal_id="service-canonical",
            org_id="tenant-a",
            display_name="nightly",
            approved_by="user-a",
        )
    )
    db_session.add(
        ServicePrincipalAlias(
            id="alias-a",
            org_id="tenant-a",
            canonical_service_principal_id="service-canonical",
            alias_source="eventbridge",
            alias_id="eventbridge:nightly",
            registered_by="user-a",
        )
    )
    await db_session.flush()

    built = await build_root_snapshot(
        db_session,
        store=_RootStore(
            {
                "authority_kind": {"S": "service_policy"},
                "human_id": {"S": "approver-user"},
                "service_identity": {"S": "eventbridge:nightly"},
            }
        ),
        invocation_id="root-a",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-a"}},
        grant=_grant("service_policy", "approver-user"),
        now=NOW,
    )

    assert (built.principal_kind, built.principal_id) == ("service_account", "service-canonical")


def test_bootstrap_decision_is_signed_for_model_audience_and_chain(policy_store):
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    value = snapshot().to_dict()
    raw, digest = canonical_json(value).decode(), policy_digest(value)
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#run-developer"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "active"},
            "persona": {"S": "developer"},
            "direct_model_requested": {"S": "sonnet46"},
            "direct_model_override": {"S": SONNET},
            "model_policy_snapshot": {"S": raw},
            "model_policy_snapshot_digest": {"S": digest},
        },
    )
    record = type(
        "Record",
        (),
        {
            "tenant_id": "tenant-a",
            "invocation_id": "run-developer",
            "principal": "run-developer#1",
            "current_attempt": 1,
        },
    )()
    env = {SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "policy-key"}
    result = bootstrap_model_policy(store=policy_store, record=record, grant=_grant("github_event"), env=env)
    decision = result["decision"]
    verified = verify_envelope(
        result["assertion"],
        public_keys={"policy-key": private.public_key()},
        expected_run_id="run-developer",
        expected_generation=1,
        expected_action="resolve_model",
        expected_command_id=digest,
        request_body=canonical_json(decision),
        expected_audience=MODEL_POLICY_AUDIENCE,
        expected_chain_id="chain-a",
        now=datetime.now(UTC),
    )

    assert result["posture"] == "report_only"
    assert result["status"] == "proposed"
    assert decision["resolved_model_id"] == SONNET
    assert decision["requested_model_id"] == "sonnet46"
    assert decision["resolution_source"] == "explicit-direct"
    assert (decision["runtime_posture"], decision["posture_revision"]) == ("report_only", 2)
    assert verified.chain_id == "chain-a"


def test_bootstrap_records_invalid_direct_override_as_report_only_unavailable(policy_store):
    value = snapshot().to_dict()
    raw, digest = canonical_json(value).decode(), policy_digest(value)
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#run-developer"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "active"},
            "persona": {"S": "developer"},
            "direct_model_requested": {"S": "not-a-real-model"},
            "model_policy_snapshot": {"S": raw},
            "model_policy_snapshot_digest": {"S": digest},
        },
    )
    record = type(
        "Record",
        (),
        {
            "tenant_id": "tenant-a",
            "invocation_id": "run-developer",
            "principal": "run-developer#1",
            "current_attempt": 1,
        },
    )()

    assert bootstrap_model_policy(
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
    ) == {
        "posture": "report_only",
        "status": "unavailable",
        "reason": "direct_override_unresolved",
    }
