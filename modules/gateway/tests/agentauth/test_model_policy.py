"""PMM-06 snapshot determinism, trusted-root identity and adversarial cases."""

from __future__ import annotations

import json
import math
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import boto3
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from moto import mock_aws
from sqlalchemy import update
from sqlalchemy.exc import OperationalError

from src.admin.persona_models.catalogue import HARNESS_CONTRACT_REVISION
from src.admin.persona_models.catalogue_service import compute_request_shape_sha256
from src.agentauth import envelope as envelope_module
from src.agentauth import model_policy as model_policy_module
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
    ModelPolicyDecision,
    ModelPolicyError,
    ModelPolicySnapshot,
    apply_live_posture,
    bootstrap_model_policy,
    bootstrap_model_policy_live,
    build_root_snapshot,
    canonical_json,
    ensure_snapshot_for_admission,
    ensure_snapshot_report_only,
    parse_snapshot,
    policy_digest,
    resolve_decision,
)
from src.agentauth.runtime_posture import measured_cache_ttl_seconds
from src.proxy.bedrock_routing import BedrockTarget, bedrock_routing_resolver
from src.shared.models.organization import User
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence
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


@pytest.fixture
def snapshot_clock(monkeypatch):
    """Resolve and sign fixed-date snapshots against that same fixed clock."""

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr(model_policy_module, "datetime", Clock)
    monkeypatch.setattr(envelope_module, "datetime", Clock)
    return NOW


def test_snapshot_is_refused_at_its_exact_expiry():
    with pytest.raises(ModelPolicyError, match="snapshot_expired"):
        resolve_decision(snapshot(), invocation_id="run-review", persona="reviewer", now=NOW + timedelta(hours=2))


def active_allowlist_revision(
    *,
    tenant_patterns=None,
    tenant_policy_source="platform-baseline",
    service_restriction_pattern_sets=None,
    service_policy_unavailable_reason=None,
    service_principal_status=None,
):
    return model_policy_module._active_allowlist_policy_revision(
        tenant_patterns=tenant_patterns,
        tenant_policy_source=tenant_policy_source,
        service_restriction_pattern_sets=service_restriction_pattern_sets or [],
        service_policy_unavailable_reason=service_policy_unavailable_reason,
        service_principal_status=service_principal_status,
    )


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


def live_snapshot(**changes) -> ModelPolicySnapshot:
    """A `snapshot()` whose validity window is relative to the real clock.

    `NOW` is a fixed instant, which is exactly right for the resolution tests: they
    pass `now=NOW` explicitly, so issuance and resolution share one clock and the
    result is deterministic forever.

    It is wrong for tests that go through `bootstrap_model_policy`, because that
    seam does not accept a `now` -- `_resolve_execution_decision` calls
    `resolve_decision` without one, so it reads the real wall clock by design. A
    snapshot issued at a fixed 12:00 with a two-hour window therefore expires
    against real time, and those tests began returning `snapshot_expired` once the
    day's clock passed 14:00 UTC. The expiry check was right; the fixture was
    incoherent.

    Anchoring issuance to `datetime.now(UTC)` makes both ends of the comparison use
    the same clock again, which is the same convention the live-bootstrap tests in
    this file already follow. Issuance is backdated a minute so the window is
    unambiguously open rather than opening exactly at the current instant. Not a
    larger hard-coded expiry: pushing the constant into the future would only move
    the failure, and the next reader would have no way to tell it was load-bearing.
    """
    current = datetime.now(UTC)
    values = {"issued_at": current - timedelta(minutes=1), "expires_at": current + timedelta(hours=2)}
    values.update(changes)
    return snapshot(**values)


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
    ("class_policy", "reason"),
    [
        ({"model_id": SONNET, "posture": "unknown", "posture_revision": 3}, "runtime_posture_unsupported"),
        ({"model_id": SONNET, "posture": "", "posture_revision": 3}, "runtime_posture_unsupported"),
        ({"model_id": SONNET, "posture": None, "posture_revision": 3}, "runtime_posture_unsupported"),
        ({"model_id": SONNET, "posture": "report_only", "posture_revision": 0}, "posture_revision_unsupported"),
        ({"model_id": SONNET, "posture": "report_only", "posture_revision": True}, "posture_revision_unsupported"),
        ({"model_id": SONNET, "posture": "report_only", "posture_revision": "3"}, "posture_revision_unsupported"),
        ({"model_id": SONNET, "posture": "report_only"}, "posture_revision_unsupported"),
    ],
)
def test_a_malformed_snapshot_posture_is_refused_not_guessed(class_policy, reason):
    """An unreadable posture is a malformed snapshot, never a substituted default."""
    with pytest.raises(ModelPolicyError, match=reason):
        resolve_decision(
            snapshot(class_defaults={"claude-agent-sdk": class_policy}),
            invocation_id="run-developer",
            persona="developer",
            now=NOW,
        )


@pytest.mark.parametrize("posture", ["disabled", "report_only", "enforcing"])
def test_all_three_postures_resolve_and_are_marked_unverified(posture):
    """Source supports the full vocabulary; the snapshot's value is not trusted.

    Rejecting the string ``enforcing`` in this helper would not be an enforcement
    implementation — it would only hide the refusal somewhere less visible. What
    makes a decision safe is that ``posture_source`` is ``None`` until the live
    per-hop read replaces the snapshot's value, so an unverified decision is
    distinguishable by inspection rather than by trust.
    """
    decision = resolve_decision(
        snapshot(class_defaults={"claude-agent-sdk": {"model_id": SONNET, "posture": posture, "posture_revision": 3}}),
        invocation_id="run-developer",
        persona="developer",
        now=NOW,
    )
    assert decision.runtime_posture == posture
    assert decision.posture_revision == 3
    assert decision.snapshot_runtime_posture == posture
    assert decision.snapshot_posture_revision == 3
    assert decision.posture_source is None, "a decision that skipped the live read must not look verified"
    assert decision.posture_observed_at is None


@pytest.mark.asyncio
async def test_one_unchanged_snapshot_follows_posture_rollover_within_the_measured_bound(db_session):
    """``report_only -> enforcing -> report_only`` on a single frozen snapshot.

    The dispatch requirement this pins: the root snapshot is immutable — same
    object, same digest, same frozen ``snapshot_runtime_posture`` at every hop —
    yet it must not pin the chain into ``enforcing`` after an audited operational
    rollback.  So the posture each hop actually runs under comes from a live read,
    and the only staleness allowed is the measured cache window.

    A fake clock rather than ``sleep``: the guarantee is stated in elapsed
    seconds, and a test that waited for real time would be both slow and, near
    the boundary, flaky.  Advancing an injected ``now`` tests the same comparison
    the deployed code performs.
    """
    ttl = measured_cache_ttl_seconds()
    assert ttl > 0, "the rollover window under test only exists when caching is on"
    policy = snapshot()
    frozen_digest = policy_digest(policy.to_dict())
    await _add_live_posture_setting(db_session, posture="report_only", posture_revision=5)
    await db_session.commit()

    async def hop(label: str, *, at: datetime) -> ModelPolicyDecision:
        decision = resolve_decision(policy, invocation_id=f"run-{label}", persona="developer", now=NOW)
        # Every hop re-reads: the frozen snapshot is never the authority.
        return await apply_live_posture(db_session, decision=decision, now=at)

    async def operator_sets(posture: str, revision: int) -> None:
        await db_session.execute(update(PersonaModelPolicySetting).values(enforcement_posture=posture, posture_revision=revision))
        await db_session.commit()

    first = await hop("one", at=NOW)
    assert (first.runtime_posture, first.posture_revision, first.posture_source) == ("report_only", 5, "live")

    # Roll forward to enforcing.  Within the bound the old observation may still
    # be served — that is the measured window, and it must be visible as such.
    await operator_sets("enforcing", 6)
    still_cached = await hop("two", at=NOW + timedelta(seconds=ttl - 1))
    assert (still_cached.runtime_posture, still_cached.posture_revision) == ("report_only", 5)
    assert still_cached.posture_source == "cache", "a stale observation must not be reported as a fresh read"

    enforcing = await hop("three", at=NOW + timedelta(seconds=ttl + 1))
    assert (enforcing.runtime_posture, enforcing.posture_revision, enforcing.posture_source) == ("enforcing", 6, "live")

    # The audited rollback.  This is the case a snapshot-pinned posture would
    # get wrong: the chain's root still says enforcing forever.
    await operator_sets("report_only", 7)
    rolled_back = await hop("four", at=NOW + timedelta(seconds=(2 * ttl) + 2))
    assert (rolled_back.runtime_posture, rolled_back.posture_revision) == ("report_only", 7)
    assert rolled_back.posture_source == "live"

    for hop_decision in (first, still_cached, enforcing, rolled_back):
        assert hop_decision.snapshot_digest == frozen_digest, "the snapshot must not be rewritten to follow the posture"
        assert hop_decision.snapshot_runtime_posture == "report_only"
        assert hop_decision.snapshot_posture_revision == 2
        assert hop_decision.posture_observed_at is not None
        assert hop_decision.resolved_model_id == SONNET, "posture changes must not change the selected model"


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

    with pytest.raises(ModelPolicyError, match="snapshot_missing"):
        parse_snapshot(None, None, tenant_id="tenant-a")
    with pytest.raises(ModelPolicyError, match="snapshot_revision_mismatch"):
        parse_snapshot(
            raw,
            digest,
            tenant_id="tenant-a",
            policy_revision="another-policy-revision",
        )
    with pytest.raises(ModelPolicyError, match="snapshot_chain_mismatch"):
        parse_snapshot(raw, digest, tenant_id="tenant-a", correlation_id="chain-b")
    wrong_audience = {**value, "audience": "another-consumer"}
    with pytest.raises(ModelPolicyError, match="snapshot_audience_mismatch"):
        parse_snapshot(
            canonical_json(wrong_audience).decode(),
            policy_digest(wrong_audience),
            tenant_id="tenant-a",
        )


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
    raw = item.get("model_policy_snapshot", {}).get("S")
    if raw:
        value = json.loads(raw)
        item.setdefault("model_policy_revision", {"S": value["policy_revision"]})
        item.setdefault("model_policy_correlation_id", {"S": value["correlation_id"]})
        item.setdefault("model_policy_root_invocation_id", {"S": value["root_invocation_id"]})
    store.client.put_item(TableName=store.table, Item=item)


@pytest.mark.asyncio
async def test_child_inherits_exact_parent_snapshot_without_rereading_preferences(db_session, policy_store, snapshot_clock):
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
            "persona": {"S": "developer"},
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

    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    child_decision = bootstrap_model_policy(
        store=policy_store,
        record=type(
            "Record",
            (),
            {
                "tenant_id": "tenant-a",
                "invocation_id": "child-a",
                "principal": "child-a#1",
                "current_attempt": 1,
            },
        )(),
        grant=_grant("github_event"),
        env={SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "policy-key"},
    )
    assert child_decision["decision"]["snapshot_digest"] == digest
    assert child_decision["decision"]["correlation_id"] == "chain-a"
    assert child_decision["decision"]["resolved_model_id"] == SONNET


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


@pytest.mark.asyncio
async def test_real_root_admission_creates_and_binds_snapshot_before_publication(
    db_session,
    policy_store,
):
    db_session.add(
        User(
            id="admission-user",
            org_id="tenant-a",
            team_id="team-a",
            email="admission@example.test",
            cognito_sub="admission-sub",
        )
    )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
        )
    )
    await db_session.flush()
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": "github_event"},
            "human_id": {"S": "admission-sub"},
        },
    )
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#root-admission"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "pending"},
            "persona": {"S": "developer"},
            "flow_id": {"S": "chain-admission"},
        },
    )
    _put(
        policy_store,
        {
            "pk": {"S": "INVOCATION#root-admission"},
            "sk": {"S": "DISPATCH"},
            "tenant_id": {"S": "tenant-a"},
        },
    )
    grant = _grant("github_event", "admission-sub")
    policy_store.live_grant = lambda **_kwargs: grant

    receipt = await ensure_snapshot_for_admission(
        db_session,
        store=policy_store,
        invocation_id="root-admission",
        now=NOW,
    )
    execution = policy_store._read("TENANT#tenant-a", "EXEC#root-admission")
    stored = parse_snapshot(
        execution["model_policy_snapshot"]["S"],
        execution["model_policy_snapshot_digest"]["S"],
        tenant_id="tenant-a",
        policy_revision=execution["model_policy_revision"]["S"],
        correlation_id="chain-admission",
        root_invocation_id="root-admission",
    )

    assert receipt == {
        "status": "available",
        "snapshot_digest": policy_digest(stored.to_dict()),
        "root_invocation_id": "root-admission",
    }
    assert stored.principal_id == "admission-user"
    assert stored.source == "live"


@pytest.mark.asyncio
async def test_snapshot_admission_latency_stays_well_inside_webhook_budget(
    db_session,
    policy_store,
):
    db_session.add(
        User(
            id="latency-user",
            org_id="tenant-a",
            team_id="team-a",
            email="latency@example.test",
            cognito_sub="latency-sub",
        )
    )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
        )
    )
    await db_session.flush()
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": "github_event"},
            "human_id": {"S": "latency-sub"},
        },
    )
    policy_store.live_grant = lambda **_kwargs: _grant("github_event", "latency-sub")
    latencies_ms = []
    for index in range(25):
        invocation_id = f"latency-root-{index}"
        _put(
            policy_store,
            {
                "pk": {"S": "TENANT#tenant-a"},
                "sk": {"S": f"EXEC#{invocation_id}"},
                "tenant_id": {"S": "tenant-a"},
                "status": {"S": "pending"},
                "persona": {"S": "developer"},
                "flow_id": {"S": invocation_id},
            },
        )
        _put(
            policy_store,
            {
                "pk": {"S": f"INVOCATION#{invocation_id}"},
                "sk": {"S": "DISPATCH"},
                "tenant_id": {"S": "tenant-a"},
            },
        )
        started = time.perf_counter()
        await ensure_snapshot_for_admission(
            db_session,
            store=policy_store,
            invocation_id=invocation_id,
            now=NOW + timedelta(seconds=index),
        )
        latencies_ms.append((time.perf_counter() - started) * 1000)

    ordered = sorted(latencies_ms)
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[math.ceil(len(ordered) * 0.99) - 1]
    print(f"PMM06_AC10 snapshot admission delta p50={p50:.2f}ms p99={p99:.2f}ms")
    assert p99 < 2000, "snapshot admission consumed too much of the 10-second budget"


class _RootStore:
    def __init__(self, authority):
        self.authority = authority

    def _read(self, pk, sk):
        return self.authority if sk == "AUTHORITY#authority-a" else None


class _UnavailablePreferenceSession:
    """A session whose preference read fails, as a database outage would.

    ``begin_nested`` stands in for the SAVEPOINT that the real reads run inside.
    It only has to be a working async context manager here; this stub cannot
    model an aborted PostgreSQL transaction at all, which is exactly why the
    claim-preservation guarantee is proved in
    ``tests/migrations/test_model_policy_postgres.py`` against a real server.
    """

    @asynccontextmanager
    async def begin_nested(self):
        yield self

    async def scalar(self, query):
        rendered = str(query)
        if "FROM users" in rendered:
            return SimpleNamespace(id="cache-user", team_id="team-a")
        if "FROM service_principals" in rendered:
            return SimpleNamespace(status="active")
        if "FROM teams" in rendered:
            return SimpleNamespace(id="team-a", department_id="department-a")
        raise AssertionError(f"unexpected scalar query: {rendered}")

    async def execute(self, query):
        if "FROM users" in str(query):
            return SimpleNamespace(scalar_one_or_none=lambda: SimpleNamespace(id="cache-user", team_id="team-a"))
        raise AssertionError(f"unexpected execute query: {query}")

    async def scalars(self, query):
        if "FROM service_principal_aliases" in str(query):
            return []
        if "FROM team_memberships" in str(query):
            return SimpleNamespace(all=lambda: [])
        raise OperationalError("preferences unavailable", {}, RuntimeError("offline"))


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
@pytest.mark.parametrize("authority_kind", ["github_event", "gate_decision", "replan_request"])
async def test_human_root_snapshot_uses_canonical_user_and_frozen_db_rows(db_session, authority_kind):
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
                "authority_kind": {"S": authority_kind},
                "human_id": {"S": "human-sub"},
            }
        ),
        invocation_id="root-a",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-a"}},
        grant=_grant(authority_kind),
        now=NOW,
    )

    assert (built.principal_kind, built.principal_id) == ("human", "user-a")
    assert built.mappings == {"developer": OPUS}
    assert built.class_defaults["claude-agent-sdk"]["model_id"] == SONNET
    assert built.allowlist_policy_revision == active_allowlist_revision()
    assert set(built.to_dict()) == {
        "schema_version",
        "tenant_id",
        "principal_kind",
        "principal_id",
        "mappings",
        "class_defaults",
        "persona_contracts",
        "policy_revision",
        "allowlist_policy_revision",
        "catalogue_revision",
        "correlation_id",
        "root_invocation_id",
        "issued_at",
        "expires_at",
        "audience",
        "source",
    }
    assert built.persona_contracts["developer"] == {
        "compatibility_class": "claude-agent-sdk",
        "harness_contract_revision": "0.3.220",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_kind", ["github_event", "gate_decision", "replan_request"])
async def test_forged_root_identity_is_distinct_unverified_provenance(db_session, authority_kind):
    with pytest.raises(ModelPolicyError, match="unverified_provenance"):
        await build_root_snapshot(
            db_session,
            store=_RootStore(
                {
                    "authority_kind": {"S": authority_kind},
                    "human_id": {"S": "forged-human"},
                }
            ),
            invocation_id="root-a",
            tenant_id="tenant-a",
            execution={"flow_id": {"S": "chain-a"}},
            grant=_grant(authority_kind, "trusted-human"),
            now=NOW,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_kind", ["github_event", "gate_decision", "replan_request"])
async def test_human_root_cannot_resolve_another_tenants_user(db_session, authority_kind):
    from src.shared.identity.resolver import UnresolvableUserEntityError

    db_session.add(User(id="other-user", org_id="tenant-b", team_id="team-b", email="b@example.test", cognito_sub="human-sub"))
    await db_session.flush()
    with pytest.raises(UnresolvableUserEntityError):
        await build_root_snapshot(
            db_session,
            store=_RootStore({"authority_kind": {"S": authority_kind}, "human_id": {"S": "human-sub"}}),
            invocation_id="root-a",
            tenant_id="tenant-a",
            execution={"flow_id": {"S": "chain-a"}},
            grant=_grant(authority_kind),
            now=NOW,
        )


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
    for index, (persona, model) in enumerate((("architect", OPUS), ("developer", SONNET), ("reviewer", HAIKU))):
        db_session.add(
            PersonaModelPreference(
                id=f"service-pref-{index}",
                org_id="tenant-a",
                principal_kind="service_account",
                principal_source="sa_registration",
                principal_id="service-canonical",
                persona_key=persona,
                canonical_model_id=model,
                requested_alias=model,
                revision=index + 1,
                updated_by="user-a",
                updated_by_source="sa_registration",
            )
        )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
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
    service_decisions = [
        resolve_decision(
            built,
            invocation_id=f"service-{persona}",
            persona=persona,
            now=NOW,
        )
        for persona in ("architect", "developer", "reviewer")
    ]
    assert [row.resolved_model_id for row in service_decisions] == [
        OPUS,
        SONNET,
        HAIKU,
    ]
    assert {row.snapshot_digest for row in service_decisions} == {policy_digest(built.to_dict())}


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_kind", ["github_event", "replan_request"])
async def test_root_uses_only_fresh_tenant_and_principal_bound_lkg_on_database_outage(
    db_session,
    policy_store,
    monkeypatch,
    authority_kind,
):
    db_session.add(
        User(
            id="cache-user",
            org_id="tenant-a",
            team_id="team-a",
            email="cache@example.test",
            cognito_sub="cache-sub",
        )
    )
    db_session.add(
        PersonaModelPreference(
            id="cache-pref",
            org_id="tenant-a",
            principal_kind="human",
            principal_source="self",
            principal_id="cache-user",
            persona_key="developer",
            canonical_model_id=OPUS,
            requested_alias="opus",
            revision=9,
            updated_by="cache-user",
            updated_by_source="self",
        )
    )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=8,
            posture_revision=3,
            enforcement_posture="report_only",
        )
    )
    await db_session.flush()
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": authority_kind},
            "human_id": {"S": "cache-sub"},
        },
    )

    live = await build_root_snapshot(
        db_session,
        store=policy_store,
        invocation_id="root-cache-a",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-cache-a"}},
        grant=_grant(authority_kind, "cache-sub"),
        now=NOW,
    )
    monkeypatch.setattr(
        model_policy_module,
        "_resolve_principal",
        AsyncMock(return_value=("human", "cache-user")),
    )
    cached = await build_root_snapshot(
        _UnavailablePreferenceSession(),
        store=policy_store,
        invocation_id="root-cache-b",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-cache-b"}},
        grant=_grant(authority_kind, "cache-sub"),
        now=NOW + timedelta(seconds=60),
    )

    assert cached.source == "last_known_good_cache"
    assert cached.mappings == live.mappings
    assert cached.policy_revision == live.policy_revision
    assert (cached.root_invocation_id, cached.correlation_id) == (
        "root-cache-b",
        "chain-cache-b",
    )
    assert cached.expires_at == NOW + timedelta(seconds=300)

    tenant_b_pk, tenant_b_sk = model_policy_module._lkg_cache_key("tenant-b", "human:cache-user")
    assert policy_store._read(tenant_b_pk, tenant_b_sk) is None
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-b"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": authority_kind},
            "human_id": {"S": "cache-sub"},
        },
    )
    with pytest.raises(ModelPolicyError, match="snapshot_cache_missing"):
        await build_root_snapshot(
            _UnavailablePreferenceSession(),
            store=policy_store,
            invocation_id="root-cache-tenant-b",
            tenant_id="tenant-b",
            execution={"flow_id": {"S": "chain-cache-tenant-b"}},
            grant=_grant(authority_kind, "cache-sub"),
            now=NOW + timedelta(seconds=60),
        )
    assert policy_store._read(tenant_b_pk, tenant_b_sk) is None


@pytest.mark.asyncio
async def test_lkg_refuses_when_active_allowlist_revision_changed(
    db_session,
    policy_store,
    monkeypatch,
):
    db_session.add(
        User(
            id="cache-user",
            org_id="tenant-a",
            team_id="team-a",
            email="cache-revision@example.test",
            cognito_sub="cache-revision-sub",
        )
    )
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=1,
            posture_revision=1,
            enforcement_posture="report_only",
        )
    )
    await db_session.flush()
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": "github_event"},
            "human_id": {"S": "cache-revision-sub"},
        },
    )
    await build_root_snapshot(
        db_session,
        store=policy_store,
        invocation_id="root-cache-revision-a",
        tenant_id="tenant-a",
        execution={"flow_id": {"S": "chain-cache-revision-a"}},
        grant=_grant("github_event", "cache-revision-sub"),
        now=NOW,
    )

    monkeypatch.setenv(
        "BG_MODEL_ALLOWED_MODELS_CONFIG",
        json.dumps({"tenant-a": ["global.anthropic.claude-*"]}),
    )
    monkeypatch.setattr(
        model_policy_module,
        "_resolve_principal",
        AsyncMock(return_value=("human", "cache-user")),
    )
    with pytest.raises(ModelPolicyError, match="snapshot_revision_mismatch"):
        await build_root_snapshot(
            _UnavailablePreferenceSession(),
            store=policy_store,
            invocation_id="root-cache-revision-b",
            tenant_id="tenant-a",
            execution={"flow_id": {"S": "chain-cache-revision-b"}},
            grant=_grant("github_event", "cache-revision-sub"),
            now=NOW + timedelta(seconds=60),
        )


@pytest.mark.asyncio
async def test_lkg_refuses_identity_outage_before_reading_alias_addressed_cache(
    policy_store,
    monkeypatch,
):
    # A legacy/raw-subject-addressed row is deliberately present.  If current
    # identity resolution is unavailable, its canonical owner cannot be
    # proven and the row must not be considered.
    legacy_pk, legacy_sk = model_policy_module._lkg_cache_key("tenant-a", "github_event:human-sub")
    _put(
        policy_store,
        {
            "pk": {"S": legacy_pk},
            "sk": {"S": legacy_sk},
            "tenant_id": {"S": "tenant-a"},
            "owner_locator": {"S": "human:old-owner"},
            "principal_kind": {"S": "human"},
            "principal_id": {"S": "old-owner"},
        },
    )
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": "github_event"},
            "human_id": {"S": "human-sub"},
        },
    )
    monkeypatch.setattr(
        model_policy_module,
        "_resolve_principal",
        AsyncMock(side_effect=OperationalError("identity unavailable", {}, RuntimeError("offline"))),
    )

    with pytest.raises(ModelPolicyError, match="snapshot_cache_owner_unproven"):
        await build_root_snapshot(
            _UnavailablePreferenceSession(),
            store=policy_store,
            invocation_id="root-cache-unproven",
            tenant_id="tenant-a",
            execution={"flow_id": {"S": "chain-cache-unproven"}},
            grant=_grant("github_event"),
            now=NOW + timedelta(seconds=60),
        )

    assert policy_store._read(legacy_pk, legacy_sk) is not None


@pytest.mark.asyncio
async def test_lkg_alias_recycle_cannot_cross_canonical_service_owners(
    policy_store,
    monkeypatch,
):
    original = snapshot(
        principal_kind="service_account",
        principal_id="service-owner-a",
        allowlist_policy_revision=active_allowlist_revision(service_principal_status="active"),
    )
    owner_a_locator = "service_account:service-owner-a"
    owner_b_locator = "service_account:service-owner-b"
    await model_policy_module._store_lkg_snapshot(
        store=policy_store,
        snapshot=original,
        owner_locator=owner_a_locator,
        now=NOW,
    )
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": "service_policy"},
            # Same alias as before, now authoritatively resolved to owner B.
            "service_identity": {"S": "eventbridge:nightly"},
        },
    )
    monkeypatch.setattr(
        model_policy_module,
        "_resolve_principal",
        AsyncMock(return_value=("service_account", "service-owner-b")),
    )
    kwargs = {
        "session": _UnavailablePreferenceSession(),
        "store": policy_store,
        "invocation_id": "root-recycled-alias",
        "tenant_id": "tenant-a",
        "execution": {"flow_id": {"S": "chain-recycled-alias"}},
        "grant": _grant("service_policy"),
        "now": NOW + timedelta(seconds=60),
    }

    with pytest.raises(ModelPolicyError, match="snapshot_cache_missing"):
        await build_root_snapshot(**kwargs)

    # Even copying owner A's row under owner B's digest cannot change the
    # independently stored canonical-owner binding.
    owner_a_pk, owner_a_sk = model_policy_module._lkg_cache_key("tenant-a", owner_a_locator)
    owner_b_pk, owner_b_sk = model_policy_module._lkg_cache_key("tenant-a", owner_b_locator)
    copied = policy_store._read(owner_a_pk, owner_a_sk)
    copied["pk"] = {"S": owner_b_pk}
    copied["sk"] = {"S": owner_b_sk}
    _put(policy_store, copied)

    with pytest.raises(ModelPolicyError, match="snapshot_cache_owner_mismatch"):
        await build_root_snapshot(**kwargs)


@pytest.mark.asyncio
async def test_lkg_rejects_stale_unsigned_and_revision_mismatched_rows(
    policy_store,
    monkeypatch,
):
    policy = snapshot(
        principal_id="cache-user",
        allowlist_policy_revision=active_allowlist_revision(),
    )
    await model_policy_module._store_lkg_snapshot(
        store=policy_store,
        snapshot=policy,
        owner_locator="human:cache-user",
        now=NOW,
    )
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "AUTHORITY#authority-a"},
            "authority_kind": {"S": "github_event"},
            "human_id": {"S": "human-sub"},
        },
    )
    monkeypatch.setattr(
        model_policy_module,
        "_resolve_principal",
        AsyncMock(return_value=("human", "cache-user")),
    )
    kwargs = {
        "session": _UnavailablePreferenceSession(),
        "store": policy_store,
        "invocation_id": "root-cache-b",
        "tenant_id": "tenant-a",
        "execution": {"flow_id": {"S": "chain-cache-b"}},
        "grant": _grant("github_event"),
    }
    with pytest.raises(ModelPolicyError, match="snapshot_cache_stale"):
        await build_root_snapshot(**kwargs, now=NOW + timedelta(seconds=301))

    pk, sk = model_policy_module._lkg_cache_key("tenant-a", "human:cache-user")
    item = policy_store._read(pk, sk)
    item["snapshot"]["S"] = item["snapshot"]["S"].replace("cache-user", "cache-user-forged")
    _put(policy_store, item)
    with pytest.raises(ModelPolicyError, match="snapshot_altered"):
        await build_root_snapshot(**kwargs, now=NOW + timedelta(seconds=60))

    await model_policy_module._store_lkg_snapshot(
        store=policy_store,
        snapshot=policy,
        owner_locator="human:cache-user",
        now=NOW,
    )
    item = policy_store._read(pk, sk)
    item["policy_revision"] = {"S": "forged-revision"}
    _put(policy_store, item)
    with pytest.raises(ModelPolicyError, match="snapshot_revision_mismatch"):
        await build_root_snapshot(**kwargs, now=NOW + timedelta(seconds=60))


def test_bootstrap_decision_is_signed_for_model_audience_and_chain(policy_store, snapshot_clock):
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    # `live_snapshot`: `bootstrap_model_policy` resolves against the real clock.
    value = live_snapshot().to_dict()
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
        now=snapshot_clock,
    )

    assert result["posture"] == "report_only"
    assert result["status"] == "proposed"
    assert decision["resolved_model_id"] == SONNET
    assert decision["requested_model_id"] == "sonnet46"
    assert decision["resolution_source"] == "explicit-direct"
    assert (decision["runtime_posture"], decision["posture_revision"]) == ("report_only", 2)
    assert verified.chain_id == "chain-a"


def test_bootstrap_records_invalid_direct_override_as_report_only_unavailable(policy_store, snapshot_clock):
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

    # ``posture: None`` rather than ``"report_only"``: a failure that never
    # established a posture must not claim one. The old shape hard-coded the
    # permissive value into every handler, so an enforcing-class failure reported
    # itself as benign — an enforcing failure becoming report_only by exception
    # handling, which is exactly what this story has to prevent.
    assert bootstrap_model_policy(
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
    ) == {
        "posture": None,
        "posture_verified": False,
        "status": "unavailable",
        "reason": "direct_override_unresolved",
    }


def test_bootstrap_refuses_an_expired_snapshot_rather_than_signing_a_stale_proposal(policy_store):
    """Expiry is load-bearing, so it gets its own test rather than only a fixture.

    Nothing asserted `snapshot_expired` anywhere before this. That mattered because
    the three tests above used to fail against it by accident -- a fixed-clock
    fixture aging past its own window -- and the cheapest way to make those failures
    stop would have been to widen or drop the expiry check. Nothing would have
    caught that.

    So the window is closed deliberately here: issued in the past, expired before
    now, with everything else about the snapshot valid. A signed proposal describing
    policy that is no longer current is worse than no proposal, because the
    signature makes it look authoritative. The refusal must be the reason, not a
    generic unavailability, and no assertion may be signed.
    """
    current = datetime.now(UTC)
    value = snapshot(
        issued_at=current - timedelta(hours=3),
        expires_at=current - timedelta(minutes=1),
    ).to_dict()
    raw, digest = canonical_json(value).decode(), policy_digest(value)
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#run-developer"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "active"},
            "persona": {"S": "developer"},
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

    result = bootstrap_model_policy(store=policy_store, record=record, grant=_grant("github_event"))

    assert result == {
        "posture": None,
        "posture_verified": False,
        "status": "unavailable",
        "reason": "snapshot_expired",
    }
    # No assertion, so there is nothing a downstream verifier could mistake for a
    # current decision.
    assert "assertion" not in result


def test_resolution_accepts_a_snapshot_up_to_but_not_including_its_expiry(policy_store):
    """The boundary itself, since `>=` versus `>` is the likely way this breaks.

    Paired with the test above so the expiry rule is pinned from both sides: a
    snapshot one second inside its window resolves, and one exactly at its expiry
    does not. Without the second half, widening the comparison to `>` would pass
    every other test in this file.
    """
    policy = snapshot()

    inside = resolve_decision(
        policy,
        invocation_id="run-developer",
        persona="developer",
        now=policy.expires_at - timedelta(seconds=1),
    )
    assert inside.resolved_model_id == SONNET

    with pytest.raises(ModelPolicyError) as exact:
        resolve_decision(policy, invocation_id="run-developer", persona="developer", now=policy.expires_at)
    assert exact.value.reason == "snapshot_expired"


def _live_policy_record(policy_store, policy: ModelPolicySnapshot):
    raw = canonical_json(policy.to_dict()).decode()
    digest = policy_digest(policy.to_dict())
    _put(
        policy_store,
        {
            "pk": {"S": "TENANT#tenant-a"},
            "sk": {"S": "EXEC#run-live-developer"},
            "tenant_id": {"S": "tenant-a"},
            "status": {"S": "active"},
            "persona": {"S": "developer"},
            "model_policy_snapshot": {"S": raw},
            "model_policy_snapshot_digest": {"S": digest},
        },
    )
    return type(
        "Record",
        (),
        {
            "tenant_id": "tenant-a",
            "invocation_id": "run-live-developer",
            "principal": "run-live-developer#1",
            "current_attempt": 1,
        },
    )()


async def _add_live_posture_setting(db_session, *, posture: str = "report_only", posture_revision: int = 1) -> None:
    """Commit the settings row the per-hop live posture read requires.

    Live bootstrap no longer takes the posture from the frozen snapshot — a root
    snapshot must not pin a chain into enforcing after an audited rollback — so a
    missing settings row is now a genuine readiness failure
    (``runtime_posture_unavailable``) rather than something the snapshot can
    paper over. Tests that exercise live bootstrap therefore have to provision it,
    exactly as a deployed environment does by migration.

    It **commits** rather than flushing, on purpose. The reader requires committed
    platform authority: a flushed-only row is the test transaction's own pending
    write, and returning that as live policy was the defect the operator
    reproduced. Loosening the reader so a flush would satisfy it would be
    preserving a permissive production fallback to accommodate the fixture.
    """
    db_session.add(
        PersonaModelPolicySetting(
            compatibility_class="claude-agent-sdk",
            harness_contract_revision="0.3.220",
            active_default_model_id=SONNET,
            revision=1,
            posture_revision=posture_revision,
            enforcement_posture=posture,
        )
    )
    await db_session.commit()


def _add_invocability_evidence(
    db_session,
    *,
    account_id: str,
    outcome: str,
    expires_at: datetime,
) -> None:
    current = datetime.now(UTC)
    db_session.add(
        ModelInvocabilityEvidence(
            account_id=account_id,
            region="us-east-1",
            canonical_model_id=SONNET,
            compatibility_class="claude-agent-sdk",
            harness_contract_revision=HARNESS_CONTRACT_REVISION,
            request_shape_sha256=compute_request_shape_sha256(SONNET),
            outcome=outcome,
            error_code=("AccessDeniedException" if outcome != "proven" else None),
            provider_request_id=("provider-request-a" if outcome == "proven" else None),
            verified_at=current - timedelta(minutes=1),
            expires_at=expires_at,
            updated_at=current,
        )
    )


def _assert_established_posture(result: dict, *, posture: str = "report_only") -> None:
    """A failed *proposal* must still report the posture it failed under.

    The posture is established from trusted snapshot facts before model selection
    is attempted, so a selection or admission failure does not discard it.  This
    is the distinction a consumer's failure behaviour depends on: a verified
    ``report_only`` with an unavailable proposal means legacy behaviour is exactly
    correct and the evidence says so, whereas an unverified posture means the
    platform does not know what it is enforcing and nothing may be assumed.
    Making the worker infer the difference from its own editable configuration
    would be handing it the decision the gateway is supposed to own.
    """
    assert result["posture"] == posture, "an established posture must survive a failed proposal"
    assert result["posture_verified"] is True
    assert result["posture_evidence"]["runtime_posture"] == posture
    assert result["posture_evidence"]["posture_source"] in {"live", "cache"}
    assert result["posture_revision"] >= 1


def _assert_unverified_posture(result: dict) -> None:
    """A failure that happened *before* any posture was established."""
    assert result["posture"] is None, "a refusal must not claim a posture it never established"
    assert result["posture_verified"] is False
    assert "posture_evidence" not in result


def _assert_bare_refusal(result: dict, reason: str, *, posture: str = "report_only") -> None:
    """A proposal refusal raised before any allowlist revision could be computed.

    It has no revision evidence to carry, so the shape is asserted exactly — but
    it does carry the established posture, because the posture read succeeded.
    """
    assert result == {
        "posture": posture,
        "posture_verified": True,
        "posture_revision": result.get("posture_revision"),
        "posture_evidence": result.get("posture_evidence"),
        "status": "unavailable",
        "reason": reason,
    }
    _assert_established_posture(result, posture=posture)


def _assert_revisioned_refusal(result: dict, reason: str, *, posture: str = "report_only") -> None:
    _assert_established_posture(result, posture=posture)
    assert result["status"] == "unavailable"
    assert result["reason"] == reason
    assert result["evidence"]["snapshot_allowlist_policy_revision"]
    assert result["evidence"]["live_allowlist_policy_revision"]
    assert type(result["evidence"]["allowlist_policy_drift"]) is bool


@pytest.mark.asyncio
async def test_live_bootstrap_signs_only_exact_fresh_destination_evidence(
    db_session,
    policy_store,
    monkeypatch,
):
    current = datetime.now(UTC)
    policy = snapshot(
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
        allowlist_policy_revision=active_allowlist_revision(),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session)
    db_session.add(
        User(
            id="user-a",
            org_id="tenant-a",
            team_id="team-a",
            email="live@example.test",
            cognito_sub="live-sub",
        )
    )
    _add_invocability_evidence(
        db_session,
        account_id="111111111111",
        outcome="proven",
        expires_at=current + timedelta(hours=1),
    )
    await db_session.flush()
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(
            return_value=BedrockTarget(
                account_id="111111111111",
                region="us-east-1",
                rung="user",
            )
        ),
    )
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
        env={SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "policy-key"},
    )

    assert result["status"] == "proposed"
    assert result["decision"]["destination_account_id"] == "111111111111"
    assert result["decision"]["destination_region"] == "us-east-1"
    assert result["decision"]["evidence_verified_at"] is not None
    assert result["decision"]["snapshot_allowlist_policy_revision"] == active_allowlist_revision()
    assert result["decision"]["live_allowlist_policy_revision"] == active_allowlist_revision()
    assert result["decision"]["allowlist_policy_drift"] is False


@pytest.mark.asyncio
async def test_live_bootstrap_signs_permitted_mid_chain_allowlist_drift(
    db_session,
    policy_store,
    monkeypatch,
):
    current = datetime.now(UTC)
    policy = snapshot(
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
        allowlist_policy_revision=active_allowlist_revision(),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session)
    db_session.add(
        User(
            id="user-a",
            org_id="tenant-a",
            team_id="team-a",
            email="live-drift@example.test",
            cognito_sub="live-drift-sub",
        )
    )
    _add_invocability_evidence(
        db_session,
        account_id="111111111111",
        outcome="proven",
        expires_at=current + timedelta(hours=1),
    )
    await db_session.flush()
    monkeypatch.setenv(
        "BG_MODEL_ALLOWED_MODELS_CONFIG",
        json.dumps({"tenant-a": [SONNET]}),
    )
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(
            return_value=BedrockTarget(
                account_id="111111111111",
                region="us-east-1",
                rung="user",
            )
        ),
    )
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
        env={SIGNING_KEY_ENV: pem, SIGNING_KEY_ID_ENV: "policy-key"},
    )

    assert result["status"] == "proposed"
    assert result["decision"]["snapshot_allowlist_policy_revision"] == active_allowlist_revision()
    assert result["decision"]["live_allowlist_policy_revision"] == active_allowlist_revision(
        tenant_patterns=[SONNET],
        tenant_policy_source="org:tenant-a",
    )
    assert result["decision"]["allowlist_policy_drift"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evidence_account", "outcome", "expiry_delta", "expected_reason"),
    [
        ("111111111111", "proven", timedelta(seconds=-1), "evidence_stale"),
        ("111111111111", "refused", timedelta(hours=1), "not_invocable"),
        ("222222222222", "proven", timedelta(hours=1), "probing_disabled"),
    ],
)
async def test_live_bootstrap_refuses_stale_refused_and_wrong_destination_evidence(
    db_session,
    policy_store,
    monkeypatch,
    evidence_account,
    outcome,
    expiry_delta,
    expected_reason,
):
    current = datetime.now(UTC)
    policy = snapshot(
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session)
    db_session.add(
        User(
            id="user-a",
            org_id="tenant-a",
            team_id="team-a",
            email="live-negative@example.test",
            cognito_sub="live-negative-sub",
        )
    )
    _add_invocability_evidence(
        db_session,
        account_id=evidence_account,
        outcome=outcome,
        expires_at=current + expiry_delta,
    )
    await db_session.flush()
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(
            return_value=BedrockTarget(
                account_id="111111111111",
                region="us-east-1",
                rung="user",
            )
        ),
    )

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
    )

    _assert_revisioned_refusal(result, expected_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("principal_status", "expected_reason"),
    [
        (None, "principal_unavailable"),
        ("suspended", "not_permitted"),
    ],
)
async def test_live_bootstrap_refuses_missing_or_suspended_service_principal(
    db_session,
    policy_store,
    monkeypatch,
    principal_status,
    expected_reason,
):
    current = datetime.now(UTC)
    policy = snapshot(
        principal_kind="service_account",
        principal_id="service-a",
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session)
    if principal_status is not None:
        db_session.add(
            ServicePrincipal(
                canonical_service_principal_id="service-a",
                org_id="tenant-a",
                display_name="Service A",
                status=principal_status,
                approved_by="admin-a",
            )
        )
        await db_session.flush()
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(
            return_value=BedrockTarget(
                account_id="111111111111",
                region="us-east-1",
                rung="org",
            )
        ),
    )

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
    )

    if principal_status is None:
        _assert_bare_refusal(result, expected_reason)
    else:
        _assert_revisioned_refusal(result, expected_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy_config",
    [
        json.dumps({"tenant-a": [HAIKU]}),
        json.dumps({"tenant-a": []}),
        "not-json",
    ],
)
async def test_live_bootstrap_refuses_model_outside_or_unavailable_tenant_policy(
    db_session,
    policy_store,
    monkeypatch,
    policy_config,
):
    current = datetime.now(UTC)
    policy = snapshot(
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session)
    db_session.add(
        User(
            id="user-a",
            org_id="tenant-a",
            team_id="team-a",
            email="tenant-policy@example.test",
            cognito_sub="tenant-policy-sub",
        )
    )
    _add_invocability_evidence(
        db_session,
        account_id="111111111111",
        outcome="proven",
        expires_at=current + timedelta(hours=1),
    )
    await db_session.flush()
    # Exercise the same BG_ setting rendered from SSM in production; no
    # route-global test resolver is injected.
    monkeypatch.setenv("BG_MODEL_ALLOWED_MODELS_CONFIG", policy_config)
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(return_value=BedrockTarget(account_id="111111111111", region="us-east-1", rung="user")),
    )

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
    )

    if policy_config == "not-json":
        _assert_bare_refusal(result, "not_permitted")
    else:
        _assert_revisioned_refusal(result, "not_permitted")
        assert result["evidence"]["allowlist_policy_drift"] is True
        assert result["evidence"]["snapshot_allowlist_policy_revision"] != result["evidence"]["live_allowlist_policy_revision"]


@pytest.mark.asyncio
@pytest.mark.parametrize("posture", ["disabled", "report_only", "enforcing"])
async def test_failed_direct_proposal_retains_the_established_live_posture(db_session, policy_store, posture):
    """A refused proposal reports the posture it was refused under.

    The operator's case: a committed posture plus an unresolvable direct override.
    Before this, the posture was established only while resolving the decision, so
    the selection failure discarded it and every posture produced
    ``posture: None``.  That left the worker unable to tell "report-only, no
    proposal — carry on exactly as before, and here is truthful evidence" from
    "we cannot establish what is being enforced", and its only remaining way to
    guess was its own editable configuration.

    Holds for all three postures, including ``enforcing``: the refusal reason is
    identical, and what differs is only the posture the consumer must now act
    under.  No model is called.
    """
    await _add_live_posture_setting(db_session, posture=posture, posture_revision=11)
    policy = live_snapshot()
    record = _live_policy_record(policy_store, policy)
    policy_store.client.update_item(
        TableName=policy_store.table,
        Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "EXEC#run-live-developer"}},
        UpdateExpression="SET direct_model_requested = :requested",
        ExpressionAttributeValues={":requested": {"S": "not-a-canonical-model"}},
    )

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
        env={},
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "direct_override_unresolved"
    _assert_established_posture(result, posture=posture)
    assert result["posture_revision"] == 11


@pytest.mark.asyncio
async def test_a_posture_read_failure_reports_no_posture_at_all(db_session, policy_store):
    """The other half of the distinction: nothing established, nothing claimed.

    With no committed settings row the posture is genuinely unknown, so the
    response must not name one — not even the snapshot's frozen value, which is
    exactly what an audited rollback may already have superseded.
    """
    policy = live_snapshot()
    record = _live_policy_record(policy_store, policy)

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("github_event"),
        env={},
    )

    assert result["status"] == "unavailable"
    assert result["reason"] == "runtime_posture_unavailable"
    _assert_unverified_posture(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("restriction_patterns", "policy_unavailable_reason"),
    [
        ([[HAIKU]], None),
        ([], "not_permitted"),
    ],
)
async def test_live_bootstrap_refuses_registry_allowed_models_or_disabled_row(
    db_session,
    policy_store,
    monkeypatch,
    restriction_patterns,
    policy_unavailable_reason,
):
    from src.admin.persona_models import registry_policy

    current = datetime.now(UTC)
    policy = snapshot(
        principal_kind="service_account",
        principal_id="service-a",
        issued_at=current - timedelta(minutes=1),
        expires_at=current + timedelta(hours=2),
    )
    record = _live_policy_record(policy_store, policy)
    await _add_live_posture_setting(db_session)
    db_session.add(
        ServicePrincipal(
            canonical_service_principal_id="service-a",
            org_id="tenant-a",
            display_name="Service A",
            status="active",
            approved_by="admin-a",
        )
    )
    _add_invocability_evidence(
        db_session,
        account_id="111111111111",
        outcome="proven",
        expires_at=current + timedelta(hours=1),
    )
    await db_session.flush()
    restriction_resolver = AsyncMock(
        return_value=(restriction_patterns, policy_unavailable_reason),
    )
    monkeypatch.setattr(
        registry_policy,
        "resolve_managed_service_restriction_policy",
        restriction_resolver,
    )
    monkeypatch.setattr(
        bedrock_routing_resolver,
        "resolve",
        AsyncMock(return_value=BedrockTarget(account_id="111111111111", region="us-east-1", rung="org")),
    )

    result = await bootstrap_model_policy_live(
        db_session,
        store=policy_store,
        record=record,
        grant=_grant("service_policy"),
    )

    _assert_revisioned_refusal(result, "not_permitted")
    restriction_resolver.assert_awaited_once_with(
        db_session,
        org_id="tenant-a",
        canonical_service_principal_id="service-a",
    )


@pytest.mark.parametrize("owner_kind,owner_id", [("human", "canonical-human"), ("service_account", "canonical-service")])
def test_issued_decision_retains_canonical_owner_for_shadow_evidence(owner_kind, owner_id):
    frozen = snapshot(principal_kind=owner_kind, principal_id=owner_id)
    decision = resolve_decision(frozen, invocation_id="run-review", persona="reviewer", now=NOW).to_dict()
    assert decision["principal_kind"] == owner_kind
    assert decision["principal_id"] == owner_id
