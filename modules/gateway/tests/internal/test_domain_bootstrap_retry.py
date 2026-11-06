# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real shared operation and domain journals gate an original bootstrap retry."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.internal import domain_provider_recovery as owner
from src.shared.models.audit import AuditLog
from tests.internal.test_domain_dispatch_reauthorization import recovery as recovery
from tests.internal.test_paid_domain_operation_postgres import (
    ORG,
    lifecycle_session_window,
    start,
)
from tests.internal.test_paid_domain_operation_postgres import (
    kubernetes as kubernetes,
)
from tests.internal.test_paid_domain_operation_postgres import (
    paid as paid,
)
from tests.internal.test_paid_domain_operation_postgres import (
    pg_server as pg_server,
)
from tests.internal.test_paid_domain_operation_postgres import (
    pg_url as pg_url,
)
from tests.internal.test_paid_domain_operation_postgres import (
    store as store,
)


@pytest.fixture
async def bootstrap_interrupted(recovery, db_session, monkeypatch):
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.execution_plan import admitted_steps, step_key
    from harness_jobs.identity import OperationRequest, encode_payload, payload_digest
    from harness_jobs.leases import _lease
    from harness_jobs.store import _record

    paid, body, cognito = recovery
    monkeypatch.setattr(owner, "domain_connect", paid.connect)
    monkeypatch.setattr(owner, "operation_connect", paid.connect)
    await lifecycle_session_window(paid)
    request = OperationRequest(
        action="provision",
        idempotency_key="approved-key",
        parameters={
            "runtime_config_sha256": "a" * 64,
            "lifecycle_artifact_id": "f" * 64,
            "lifecycle_policy_sha256": "e" * 64,
            "lifecycle_phase": "bootstrap-workspace",
            "credential_id": "spda1:10000000-0000-0000-0000-000000000001",
            "credential_service": "aws",
            "credential_label": "provider",
            "provider": "aws",
            "provider_account_id": "123456789012",
            "lifecycle_request": json.dumps({"mode": "existing-account-managed", "target_account_id": "123456789012", "region": "us-east-1"}),
            "execution_steps": json.dumps(
                [
                    {
                        "step_id": "bootstrap-workspace",
                        "provider": "superplane-lifecycle",
                        "operation_kind": "bootstrap-workspace",
                        "target": "f" * 64,
                    }
                ]
            ),
        },
    )
    async with paid.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET state='pending',request_payload=$1,plan_digest=$2", encode_payload(request), payload_digest(request)
        )
        await connection.execute("UPDATE harness_approval_consumption SET plan_digest=$1", payload_digest(request))
        await connection.execute("UPDATE operation_approvals SET request_payload=$1,plan_digest=$2", encode_payload(request), payload_digest(request))
        await connection.execute(
            "ALTER TABLE workspaces ADD COLUMN org_id uuid, ADD COLUMN provisioning_operation_id text,"
            "ADD COLUMN status text, ADD COLUMN cluster_id uuid"
        )
        await connection.execute(
            "UPDATE workspaces SET org_id=$1::text::uuid,provisioning_operation_id='original-operation',status='Provisioning'", ORG
        )
        await connection.execute(
            "CREATE TABLE workspace_lifecycle_artifacts(artifact_id text,org_id text,workspace_id text,"
            "source_operation_id text,source_payload_digest text,artifact_metadata_json text)"
        )
        await connection.execute(
            "INSERT INTO workspace_lifecycle_artifacts VALUES($1,$2,$3,'original-apply',$4,$5)",
            "f" * 64,
            ORG,
            body["workspace_id"],
            "d" * 64,
            json.dumps(
                {
                    "next_phase": "bootstrap-workspace",
                    "outputs": {"account_id": {"value": "123456789012"}, "cluster_endpoint_public_access": {"value": True}},
                }
            ),
        )
        for table in ("workspace_bootstrap_authority", "workspace_bootstrap_reservations", "workspace_lifecycle_effects", "cluster_memberships"):
            await connection.execute("CREATE TABLE " + table + "(workspace_id text)")
        await connection.execute("ALTER TABLE operation_reauthorizations ADD COLUMN scope text NOT NULL DEFAULT 'undispatched'")
    dispatched, credential = await start(paid)
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)).status_code == 200
    async with paid.connect() as connection:
        record = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease = _lease(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
    step = admitted_steps(record)[0]
    await OperationExecutor(lease, connect=paid.connect).record_intent(
        idempotency_key=step_key(record, step), provider=step.provider, operation_kind=step.operation_kind, target=step.target
    )
    expired = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Simulate passage of the original grant's lifetime in isolated Moto/SQL.
    items = paid.store.client.scan(TableName=paid.store.table)["Items"]
    for item in items:
        if item["sk"]["S"].startswith("DOMAIN_OPERATION#"):
            value = json.loads(item["mapping"]["S"])
            value["not_after"] = expired
            item["mapping"]["S"] = json.dumps(value)
            paid.store.client.put_item(TableName=paid.store.table, Item=item)
        elif item["sk"]["S"].startswith(("GRANT#", "AUTHORITY#")):
            item["expires_at"] = {"S": expired}
            paid.store.client.put_item(TableName=paid.store.table, Item=item)
    async with paid.connect() as connection:
        await connection.execute("UPDATE harness_operations SET state='running'")
        await connection.execute("UPDATE operation_approvals SET expires_at=clock_timestamp()-interval '1 hour'")
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 hour',runtime_deadline=clock_timestamp()-interval '1 hour'"
        )
        await connection.execute(
            "UPDATE operation_reauthorizations SET reauthorization_id='credential-renewal',scope='uncredentialed-planning',plan_digest=$1",
            record.plan_digest,
        )
    body.update(reauthorization_id="credential-renewal", expected_version=record.version)

    async def audit_session():
        yield db_session

    # get_db is the audit owner, independent of the real operation PostgreSQL pool.
    from src.shared.database import get_db as dependency

    paid.client._transport.app.dependency_overrides[dependency] = audit_session
    return paid, body, credential, lease, db_session


@pytest.fixture(params=[0, 1])
async def unstarted_bootstrap(bootstrap_interrupted, monkeypatch, request):
    from src.internal import domain_bootstrap_recovery as planning
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for
    from src.shared.domain_bootstrap_boundary import QUALIFIED_SOURCES
    from src.shared.domain_bootstrap_recovery import record_id, validate

    image, source = list(QUALIFIED_SOURCES.items())[request.param]
    paid, body, _, lease, db = bootstrap_interrupted
    async with paid.connect() as connection:
        await connection.execute("UPDATE operation_reauthorizations SET scope='unstarted-bootstrap'")
        current_lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
    operation = await paid_operation(binding_for("superplane", ORG), body["operation_id"])
    raw = paid.store._read("TENANT#tenant", "EXEC#" + lease.holder.rsplit("#", 1)[0])
    record = paid.store.authority.load_execution(invocation_id=lease.holder.rsplit("#", 1)[0], tenant_id="tenant")
    async with paid.connect() as connection:
        key = await connection.fetchval("SELECT idempotency_key FROM harness_provider_call_intent")
    now = datetime.now(UTC)
    evidence = {k: operation[k] for k in ("org_id", "workspace_id", "operation_id", "plan_digest")}
    evidence.update(
        version=1,
        evidence_kind="bootstrap-journal-barrier-v1",
        source_revision=source["revision"],
        source_hashes=dict(source["hashes"]),
        source_pod_uid=record.workload_binding,
        source_audit={"creation_event_id": "create", "terminal_event_ids": ["done"]},
        policy_sha256="e" * 64,
        source_key=key,
        source_fence=lease.fence_token,
        source_principal=lease.holder,
        workload_binding=record.workload_binding,
        source_pod_name=raw["pod_name"]["S"],
        source_acquired_at=current_lease["acquired_at"].isoformat(),
        storage_uid="original-claim",
        volume_uid="original-volume",
        inspection_sha256="a" * 64,
        source_image="qualified@" + image,
        checked_at=now.isoformat(),
        expires_at=(now + timedelta(minutes=30)).isoformat(),
        credential_id="spda1:10000000-0000-0000-0000-000000000001",
        installation_id="installation",
        adp_org_id="tenant",
        account_id="123456789012",
        region="us-east-1",
    )
    evidence["record_id"] = record_id(evidence)
    authority = {k: evidence[k] for k in ("installation_id", "account_id", "region", "org_id", "workspace_id")}
    authority["subject"] = operation["requester"]
    monkeypatch.setattr(planning, "domain_connect", paid.connect)
    monkeypatch.setattr(planning, "read_evidence", lambda _, **options: validate(evidence, **options))
    monkeypatch.setattr(planning, "read_authority", lambda _: authority)
    audit = AuditLog(
        org_id="tenant",
        event_type="paid_domain_provider_session_issued",
        actor_id=lease.holder,
        details={
            "operation_id": body["operation_id"],
            "credential_id": evidence["credential_id"],
            "expires_at": (now - timedelta(minutes=30)).isoformat(),
        },
    )
    db.add(audit)
    await db.commit()
    return paid, body, evidence, audit, db


async def test_original_bootstrap_can_resume_only_after_independent_journal_barrier(unstarted_bootstrap):
    paid, body, evidence, audit, db = unstarted_bootstrap
    async with paid.connect() as connection:
        original = dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption"))
    response = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert response.status_code == 200, response.text
    async with paid.connect() as connection:
        row = await connection.fetchrow("SELECT * FROM harness_provider_retries")
        proof = json.loads(row["proof"])
        assert row["recovery_scope"] == "unstarted-bootstrap"
        assert proof["issuance_audit_ids"] == [str(audit.id)]
        assert proof["owner_evidence_id"] == evidence["record_id"]
        assert proof["bootstrap_mutations_started"] is False
        assert dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption")) == original
        assert await connection.fetchval("SELECT attempts FROM harness_operation_leases") == 1
    assert await db.get(AuditLog, audit.id) is not None


@pytest.mark.parametrize(
    "change",
    [
        "authority",
        "reservation",
        "network-intent",
        "membership",
        "artifact",
        "source-image",
        "source-code",
        "source-policy",
        "expired-evidence",
        "wrong-pod",
        "session-live",
        "expiry-missing",
        "wrong-credential",
        "fresh-grant",
    ],
)
async def test_bootstrap_retry_refuses_any_mutation_or_unproven_source(unstarted_bootstrap, change):
    paid, body, evidence, audit, db = unstarted_bootstrap
    tables = {
        "authority": "workspace_bootstrap_authority",
        "reservation": "workspace_bootstrap_reservations",
        "network-intent": "workspace_lifecycle_effects",
        "membership": "cluster_memberships",
    }
    if change in tables:
        async with paid.connect() as connection:
            await connection.execute("INSERT INTO " + tables[change] + " VALUES($1)", body["workspace_id"])
    elif change == "artifact":
        async with paid.connect() as connection:
            await connection.execute("INSERT INTO workspace_lifecycle_artifacts(source_operation_id) VALUES($1)", body["operation_id"])
    elif change == "source-image":
        evidence["source_image"] = "different@sha256:" + "0" * 64
    elif change == "source-code":
        evidence["source_hashes"]["workspace_provisioning/runtime.py"] = "0" * 64
    elif change == "source-policy":
        evidence["policy_sha256"] = "0" * 64
    elif change == "expired-evidence":
        evidence["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "wrong-pod":
        evidence["workload_binding"] = "another-pod"
    elif change == "fresh-grant":
        async with paid.connect() as connection:
            await connection.execute("UPDATE harness_operation_leases SET runtime_deadline=clock_timestamp()+interval '1 minute'")
    else:
        details = dict(audit.details)
        if change == "session-live":
            details["expires_at"] = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        elif change == "expiry-missing":
            del details["expires_at"]
        else:
            details["credential_id"] = "another-provider"
        audit.details = details
        await db.commit()
    response = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert response.status_code == 409, response.text
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert await connection.fetchval("SELECT stage FROM harness_provider_call_intent") == "intended"


@pytest.fixture
async def denied_bootstrap(unstarted_bootstrap):
    import hashlib

    from src.shared.domain_bootstrap_boundary import QUALIFIED_SOURCES
    from src.shared.domain_bootstrap_denial import KIND, SOURCE_HASHES, SOURCE_IMAGE, journal_identity

    paid, body, evidence, audit, db = unstarted_bootstrap
    source = QUALIFIED_SOURCES[SOURCE_IMAGE]
    evidence.update(
        source_image="qualified@" + SOURCE_IMAGE,
        source_revision=source["revision"],
        source_hashes=dict(source["hashes"]),
        evidence_kind=KIND,
        denial_source_hashes=SOURCE_HASHES,
    )
    claim = "c" * 64
    generation = hashlib.sha256((body["operation_id"] + ":" + claim).encode()).hexdigest()
    cluster = "arn:aws:eks:us-east-1:123456789012:cluster/fixture"
    stem = "sp-bootstrap-" + generation[:24]
    first = {
        "key": "registrar-entry",
        "kind": "eks-entry",
        "actor": "registrar",
        "cluster_arn": cluster,
        "generation": generation,
        "principal_arn": "arn:aws:iam::123456789012:role/registrar",
        "groups": [stem + ":registrar"],
        "username": stem + ":registrar:{{SessionName}}",
        "client_token": hashlib.sha256((generation + ":registrar").encode()).hexdigest(),
        "lifetime": "temporary",
    }
    row = {
        **{k: body[k] for k in ("operation_id", "org_id", "workspace_id")},
        "cluster_arn": cluster,
        "generation": generation,
        "claim": claim,
        "revoked": True,
        "plan_json": json.dumps({"version": 1, "grants": [first, {**first, "key": "registrar-policy", "kind": "eks-policy"}]}),
        "progress_json": json.dumps({"phase": "revoked", "complete": True, "registrar-entry": {"phase": "revoked", "identity": None}}),
    }
    evidence["denied_grant"] = {
        **journal_identity(row, complete=True),
        "denied_request_verified": True,
        "absence_verified": True,
        "event_ids": ["aws-denial"],
        "cloudtrail_sha256": "a" * 64,
        "inventory_sha256": "b" * 64,
    }
    async with paid.connect() as c:
        await c.execute("DROP TABLE workspace_bootstrap_authority")
        await c.execute(
            "CREATE TABLE workspace_bootstrap_authority (workspace_id text, operation_id text, org_id text, cluster_arn text, "
            "generation text, claim text, revoked boolean, plan_json text, progress_json text)"
        )
        await c.execute(
            "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            *[
                row[k]
                for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "revoked", "plan_json", "progress_json")
            ],
        )
        metadata = json.loads(await c.fetchval("SELECT artifact_metadata_json FROM workspace_lifecycle_artifacts"))
        metadata["outputs"]["cluster_arn"] = {"value": cluster}
        await c.execute("UPDATE workspace_lifecycle_artifacts SET artifact_metadata_json=$1", json.dumps(metadata))
    return paid, body, evidence, row
