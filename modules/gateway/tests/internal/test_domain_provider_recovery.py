# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real protected dispatch and SQL evidence before original planning recovery."""

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
async def interrupted(recovery, db_session, monkeypatch):
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
            "credential_id": "spda1:10000000-0000-0000-0000-000000000001",
            "credential_service": "aws",
            "credential_label": "provider",
            "provider": "aws",
            "provider_account_id": "123456789012",
            "lifecycle_request": json.dumps({"mode": "existing-account-managed", "target_account_id": "123456789012", "region": "us-east-1"}),
            "execution_steps": json.dumps(
                [
                    {
                        "step_id": "prepare-infrastructure",
                        "provider": "superplane-lifecycle",
                        "operation_kind": "prepare-infrastructure",
                        "target": "target",
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
        await connection.execute("CREATE TABLE workspace_lifecycle_artifacts(source_operation_id text)")
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


@pytest.fixture
async def unstarted(interrupted, monkeypatch):
    from src.internal import domain_planning_recovery as planning
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for
    from src.shared.domain_planning_recovery import record_id, validate

    paid, body, _, lease, db = interrupted
    async with paid.connect() as connection:
        await connection.execute("UPDATE operation_reauthorizations SET scope='unstarted-planning'")
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
        evidence_kind="planning-start-barrier-v1",
        planning_started=False,
        source_key=key,
        source_fence=lease.fence_token,
        source_principal=lease.holder,
        workload_binding=record.workload_binding,
        source_pod_name=raw["pod_name"]["S"],
        source_acquired_at=current_lease["acquired_at"].isoformat(),
        storage_uid="original-claim",
        volume_uid="original-volume",
        inspection_sha256="a" * 64,
        source_image="qualified@sha256:" + "b" * 64,
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


async def test_issued_session_can_only_recover_with_independent_start_barrier(unstarted):
    paid, body, evidence, audit, db = unstarted
    response = await paid.post("/recover-unstarted-planning", body, role="producer")
    assert response.status_code == 200, response.text
    async with paid.connect() as connection:
        row = await connection.fetchrow("SELECT * FROM harness_provider_retries")
        proof = json.loads(row["proof"])
        assert row["recovery_scope"] == "unstarted-planning"
        assert proof["issuance_audit_ids"] == [str(audit.id)]
        assert proof["owner_evidence_id"] == evidence["record_id"]
        assert "credentials_delivered" not in proof
    assert await db.get(AuditLog, audit.id) is not None


@pytest.mark.parametrize(
    "change", ["expired-evidence", "wrong-pod", "wrong-acquisition", "wrong-volume-proof", "session-live", "expiry-missing", "wrong-credential"]
)
async def test_issued_recovery_refuses_incomplete_or_inconsistent_proof(unstarted, change):
    paid, body, evidence, audit, db = unstarted
    if change == "expired-evidence":
        evidence["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "wrong-pod":
        evidence["workload_binding"] = "replaced-pod"
    elif change == "wrong-acquisition":
        evidence["source_acquired_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    elif change == "wrong-volume-proof":
        evidence["planning_started"] = True
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
    response = await paid.post("/recover-unstarted-planning", body, role="producer")
    assert response.status_code == 409, response.text
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert await connection.fetchval("SELECT stage FROM harness_provider_call_intent") == "intended"


@pytest.mark.parametrize("paid", [True, False], indirect=True)
async def test_recovery_retains_original_and_dispatches_fresh_bound_attempt(interrupted):
    paid, body, old_credential, lease, _ = interrupted
    response = await paid.post("/recover-uncredentialed", body, role="producer")
    assert response.status_code == 200, response.text
    assert (await paid.post("/recover-uncredentialed", body, role="producer")).json() == response.json()
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=old_credential)).status_code == 403
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 1
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_call_intent") == 1
        assert await connection.fetchval("SELECT fence_token FROM harness_operation_leases") == lease.fence_token + 1
        assert await connection.fetchval("SELECT expires_at<clock_timestamp() FROM operation_approvals")
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    assert dispatched.json()["principal"] != lease.holder
    paid.kubernetes[1].update(uid="pod-successor", token_uid="pod-successor")
    _, credential = await start(paid)
    fresh = await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["fence_token"] == lease.fence_token + 2


@pytest.mark.parametrize("change", ["issued", "artifact", "wrong-scope", "withdrawn", "live-grant", "worker"])
async def test_source_evidence_or_withdrawn_authority_prevents_retry(interrupted, change):
    paid, body, _, lease, db = interrupted
    role = "producer"
    if change == "issued":
        db.add(
            AuditLog(
                org_id="tenant",
                event_type="paid_domain_provider_session_issued",
                actor_id=lease.holder,
                details={"operation_id": "original-operation"},
            )
        )
        await db.commit()
    elif change == "live-grant":
        grant = paid.store._read("TENANT#tenant", "GRANT#" + lease.holder)
        grant["expires_at"] = {"S": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        paid.store.client.put_item(TableName=paid.store.table, Item=grant)
    elif change == "worker":
        role = "worker"
    else:
        async with paid.connect() as connection:
            await connection.execute(
                {
                    "artifact": "INSERT INTO workspace_lifecycle_artifacts VALUES('original-operation')",
                    "wrong-scope": "UPDATE operation_reauthorizations SET scope='undispatched'",
                    "withdrawn": "UPDATE operation_approvals SET revoked=true",
                }[change]
            )
    response = await paid.post("/recover-uncredentialed", body, role=role)
    assert response.status_code in (403, 409), response.text
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert await connection.fetchval("SELECT stage FROM harness_provider_call_intent") == "intended"


def original_envelope(paid):
    item = next(row for row in paid.store.client.scan(TableName=paid.store.table)["Items"] if row["sk"]["S"].startswith("DOMAIN_OPERATION#"))
    return json.loads(item["mapping"]["S"])["envelope"]


async def obsolete_recovery_delivery(paid, *, bound):
    response = await paid.post("/dispatch", {**paid.body, "mode": "recovery"}, role="producer")
    assert response.status_code == 200, response.text
    invocation = response.json()["invocation_id"]
    items = paid.store.client.scan(TableName=paid.store.table)["Items"]
    item = next(
        row
        for row in items
        if row["sk"]["S"].startswith("DOMAIN_OPERATION#") and json.loads(row["mapping"]["S"])["envelope"]["message_id"] == invocation
    )
    mapping = json.loads(item["mapping"]["S"])
    envelope = mapping["envelope"]
    if bound:
        from src.agentauth.bootstrap import envelope_digest

        paid.kubernetes[1].update(uid="pod-old-recovery", token_uid="pod-old-recovery")
        assert json.loads((await paid.post("/task/acquire")).json()["body"]) == envelope
        response = await paid.post("/bootstrap", {"invocation_id": invocation, "envelope_digest": envelope_digest(envelope)})
        assert response.status_code == 200, response.text
    else:
        # Isolated queue fixture: discard its first copy so the test below
        # controls the exact redelivery. Never mutate live queue state in tests.
        paid.sqs.purge_queue(QueueUrl=paid.queue)
    expired = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mapping["not_after"] = expired
    item["mapping"]["S"] = json.dumps(mapping)
    paid.store.client.put_item(TableName=paid.store.table, Item=item)
    grant = paid.store._read("TENANT#tenant", "GRANT#" + invocation + "#1")
    grant["expires_at"] = {"S": expired}
    paid.store.client.put_item(TableName=paid.store.table, Item=grant)
    return envelope, item


async def another_native_recovery(paid, body, evidence):
    """Advance the same paid admission through a second real expired execution."""
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.execution_plan import admitted_steps
    from harness_jobs.leases import _lease
    from harness_jobs.provider_retry import effective_step_key
    from harness_jobs.store import _record

    from src.shared.domain_planning_recovery import record_id

    paid.kubernetes[1].update(uid="pod-second-execution", token_uid="pod-second-execution")
    _, credential = await start(paid)
    assert (await paid.post("/lease", {"operation_id": body["operation_id"]}, credential=credential)).status_code == 200
    async with paid.connect() as connection:
        record = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease_row = await connection.fetchrow("SELECT * FROM harness_operation_leases")
        lease = _lease(lease_row)
        step = admitted_steps(record)[0]
        key = await effective_step_key(connection, record, step)
    await OperationExecutor(lease, connect=paid.connect).record_intent(
        idempotency_key=key, provider=step.provider, operation_kind=step.operation_kind, target=step.target
    )
    raw = paid.store._read("TENANT#tenant", "EXEC#" + lease.holder.rsplit("#", 1)[0])
    execution = paid.store.authority.load_execution(invocation_id=lease.holder.rsplit("#", 1)[0], tenant_id="tenant")
    evidence.update(
        source_key=key,
        source_fence=lease.fence_token,
        source_principal=lease.holder,
        workload_binding=execution.workload_binding,
        source_pod_name=raw["pod_name"]["S"],
        source_acquired_at=lease_row["acquired_at"].isoformat(),
    )
    evidence["record_id"] = record_id(evidence)
    # Passage of time in isolated Moto/SQL, preserving each prior receipt.
    expired = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for item in paid.store.client.scan(TableName=paid.store.table)["Items"]:
        if item["sk"]["S"].startswith("DOMAIN_OPERATION#"):
            mapping = json.loads(item["mapping"]["S"])
            mapping["not_after"] = expired
            item["mapping"]["S"] = json.dumps(mapping)
            paid.store.client.put_item(TableName=paid.store.table, Item=item)
        elif item["sk"]["S"].startswith(("GRANT#", "AUTHORITY#")):
            item["expires_at"] = {"S": expired}
            paid.store.client.put_item(TableName=paid.store.table, Item=item)
    async with paid.connect() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 hour',runtime_deadline=clock_timestamp()-interval '1 hour'"
        )
        await connection.execute(
            "INSERT INTO operation_reauthorizations SELECT 'second-credential-renewal',"
            "original_approval_id,operation_id,org_id,workspace_id,requester,approved_by,plan_digest,"
            "$1,'allowed-once',clock_timestamp()+interval '1 hour',clock_timestamp(),false,scope "
            "FROM operation_reauthorizations WHERE reauthorization_id=$2",
            record.version,
            body["reauthorization_id"],
        )
    response = await paid.post(
        "/recover-unstarted-planning",
        {**body, "reauthorization_id": "second-credential-renewal", "expected_version": record.version},
        role="producer",
    )
    assert response.status_code == 200, response.text
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 2


@pytest.mark.parametrize("fault", [None, "broken-chain", "live-grant", "successor-mapping"])
async def test_ancestor_recovery_delivery_requires_complete_native_receipt_chain(unstarted, fault):
    import hashlib

    from harness_jobs.identity import OperationRefused

    from src.internal.domain_operation_dispatch import canonical
    from src.internal.domain_operation_runtime import tasks
    from src.internal.domain_operation_store import binding_for

    paid, body, evidence, _, _ = unstarted
    envelope, item = await obsolete_recovery_delivery(paid, bound=False)
    assert (await paid.post("/recover-unstarted-planning", body, role="producer")).status_code == 200
    await another_native_recovery(paid, body, evidence)
    invocation = envelope["message_id"]
    if fault == "broken-chain":
        async with paid.connect() as connection:
            await connection.execute(
                "UPDATE harness_provider_call_intent SET provider_ref='changed-provider' "
                "WHERE idempotency_key=(SELECT original_key FROM harness_provider_retries ORDER BY source_fence DESC LIMIT 1)"
            )
    elif fault == "live-grant":
        grant = paid.store._read("TENANT#tenant", "GRANT#" + invocation + "#1")
        grant["expires_at"] = {"S": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        paid.store.client.put_item(TableName=paid.store.table, Item=grant)
    elif fault == "successor-mapping":
        paid.store.client.delete_item(TableName=paid.store.table, Key={key: item[key] for key in ("pk", "sk")})
        async with paid.connect() as connection:
            generation = await connection.fetchval("SELECT fence_token FROM harness_operation_leases")
        mapping = json.loads(item["mapping"]["S"])
        digest = hashlib.sha256(canonical({**mapping["identity"], "generation": generation, "bootstrap_retry": 0}).encode()).hexdigest()
        item["sk"] = {"S": "DOMAIN_OPERATION#" + digest}
        paid.store.client.put_item(TableName=paid.store.table, Item=item)
    binding = binding_for("superplane", ORG)
    if fault == "broken-chain":
        with pytest.raises(OperationRefused):
            await owner.recovered_delivery(binding, envelope, paid.store)
        return
    if fault:
        assert await owner.recovered_delivery(binding, envelope, paid.store) is False
        return
    before = {kind: paid.store._read("TENANT#tenant", kind + invocation + ("#1" if kind == "GRANT#" else "")) for kind in ("EXEC#", "GRANT#")}
    async with paid.connect() as connection:
        lease_before = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        receipts_before = [dict(r) for r in await connection.fetch("SELECT * FROM harness_provider_retries ORDER BY source_fence")]
    paid.sqs.send_message(QueueUrl=paid.queue, MessageBody=json.dumps(envelope))
    paid.kubernetes[1].update(uid="pod-ancestor-drain", token_uid="pod-ancestor-drain")
    response = await paid.post("/task/acquire")
    assert response.status_code == 200 and response.json() == {"body": None}, response.text
    journal = tasks(paid.runtime, binding).read("pod-ancestor-drain")
    assert len(journal["discarded_acknowledgements"]) == 1
    assert {kind: paid.store._read("TENANT#tenant", kind + invocation + ("#1" if kind == "GRANT#" else "")) for kind in before} == before
    async with paid.connect() as connection:
        assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == lease_before
        assert [dict(r) for r in await connection.fetch("SELECT * FROM harness_provider_retries ORDER BY source_fence")] == receipts_before


@pytest.mark.parametrize("bound", [True, False])
async def test_superseded_expired_recovery_delivery_drains_without_rewriting_history(interrupted, bound):
    from src.internal.domain_operation_runtime import tasks
    from src.internal.domain_operation_store import binding_for

    paid, body, _, _, _ = interrupted
    envelope, _ = await obsolete_recovery_delivery(paid, bound=bound)
    assert (await paid.post("/recover-uncredentialed", body, role="producer")).status_code == 200
    invocation = envelope["message_id"]
    before_record = paid.store._read("TENANT#tenant", "EXEC#" + invocation)
    before_grant = paid.store._read("TENANT#tenant", "GRANT#" + invocation + "#1")
    async with paid.connect() as connection:
        before_lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        before_intent = dict(await connection.fetchrow("SELECT * FROM harness_provider_call_intent"))
    paid.sqs.send_message(QueueUrl=paid.queue, MessageBody=json.dumps(envelope))
    paid.kubernetes[1].update(uid="pod-obsolete-drain", token_uid="pod-obsolete-drain")
    response = await paid.post("/task/acquire")
    assert response.status_code == 200, response.text
    assert response.json() == {"body": None}
    journal = tasks(paid.runtime, binding_for("superplane", ORG)).read("pod-obsolete-drain")
    assert journal["state"] == "empty" and len(journal["discarded_acknowledgements"]) == 1
    archived = paid.store._read("PODTASK#pod-obsolete-drain", "DISCARDED#" + journal["discarded_acknowledgements"][0])
    assert archived["invocation_id"] == {"S": invocation}
    assert archived["sqs_http_status"] == {"N": "200"} and archived["receipt_handle_sha256"]
    assert paid.store._read("TENANT#tenant", "EXEC#" + invocation) == before_record
    assert paid.store._read("TENANT#tenant", "GRANT#" + invocation + "#1") == before_grant
    async with paid.connect() as connection:
        assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == before_lease
        assert dict(await connection.fetchrow("SELECT * FROM harness_provider_call_intent")) == before_intent
    paid.kubernetes[1].update(uid="pod-after-obsolete-drain", token_uid="pod-after-obsolete-drain")
    dispatched, credential = await start(paid)
    assert dispatched["invocation_id"] != invocation
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)).status_code == 200


@pytest.mark.parametrize("mutation", ["missing-recovery", "live-grant", "live-mapping", "missing-mapping", "new-generation", "changed-envelope"])
async def test_recovery_redelivery_requires_exact_expired_superseded_mapping(interrupted, mutation):
    from src.internal.domain_operation_store import binding_for

    paid, body, _, lease, _ = interrupted
    envelope, item = await obsolete_recovery_delivery(paid, bound=True)
    if mutation != "missing-recovery":
        assert (await paid.post("/recover-uncredentialed", body, role="producer")).status_code == 200
    future = (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    if mutation == "live-grant":
        grant = paid.store._read("TENANT#tenant", "GRANT#" + envelope["message_id"] + "#1")
        grant["expires_at"] = {"S": future}
        paid.store.client.put_item(TableName=paid.store.table, Item=grant)
    elif mutation == "live-mapping":
        mapping = json.loads(item["mapping"]["S"])
        mapping["not_after"] = future
        item["mapping"]["S"] = json.dumps(mapping)
        paid.store.client.put_item(TableName=paid.store.table, Item=item)
    elif mutation in {"missing-mapping", "new-generation"}:
        paid.store.client.delete_item(TableName=paid.store.table, Key={key: item[key] for key in ("pk", "sk")})
        if mutation == "new-generation":
            import hashlib

            from src.internal.domain_operation_dispatch import canonical

            mapping = json.loads(item["mapping"]["S"])
            digest = hashlib.sha256(
                canonical({**mapping["identity"], "generation": lease.fence_token + 1, "bootstrap_retry": 0}).encode()
            ).hexdigest()
            item["sk"] = {"S": "DOMAIN_OPERATION#" + digest}
            paid.store.client.put_item(TableName=paid.store.table, Item=item)
    elif mutation == "changed-envelope":
        envelope["arrived_at"] = future
    assert await owner.recovered_delivery(binding_for("superplane", ORG), envelope, paid.store) is False


async def test_recovered_expired_redelivery_is_journalled_without_consuming_retry(interrupted):
    from src.internal.domain_operation_runtime import tasks
    from src.internal.domain_operation_store import binding_for

    paid, body, _, lease, _ = interrupted
    envelope = original_envelope(paid)
    assert (await paid.post("/recover-uncredentialed", body, role="producer")).status_code == 200
    before_record = paid.store._read("TENANT#tenant", "EXEC#" + envelope["message_id"])
    before_grant = paid.store._read("TENANT#tenant", "GRANT#" + lease.holder)
    async with paid.connect() as connection:
        before_lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        before_intent = dict(await connection.fetchrow("SELECT * FROM harness_provider_call_intent"))
    paid.sqs.send_message(QueueUrl=paid.queue, MessageBody=json.dumps(envelope))
    paid.kubernetes[1].update(uid="pod-drain", token_uid="pod-drain")
    response = await paid.post("/task/acquire")
    assert response.status_code == 200, response.text
    assert response.json() == {"body": None}
    binding = binding_for("superplane", ORG)
    journal = tasks(paid.runtime, binding).read("pod-drain")
    assert journal["state"] == "empty" and len(journal["discarded_acknowledgements"]) == 1
    archived = paid.store._read("PODTASK#pod-drain", "DISCARDED#" + journal["discarded_acknowledgements"][0])
    assert archived["invocation_id"] == {"S": envelope["message_id"]}
    assert archived["sqs_http_status"] == {"N": "200"} and archived["receipt_handle_sha256"]
    assert (await paid.post("/task/acquire")).json() == {"body": None}
    assert paid.store._read("TENANT#tenant", "EXEC#" + envelope["message_id"]) == before_record
    assert paid.store._read("TENANT#tenant", "GRANT#" + lease.holder) == before_grant
    async with paid.connect() as connection:
        assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == before_lease
        assert dict(await connection.fetchrow("SELECT * FROM harness_provider_call_intent")) == before_intent
    paid.kubernetes[1].update(uid="pod-fresh", token_uid="pod-fresh")
    dispatched, credential = await start(paid)
    assert dispatched["principal"] != lease.holder
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)).status_code == 200


@pytest.mark.parametrize("duplicates", [3, 11])
async def test_bounded_discard_preserves_history_and_reaches_fresh_paid_work(interrupted, duplicates, monkeypatch):
    from src.agentauth.bootstrap import envelope_digest
    from src.internal import domain_operation_routes as routes
    from src.internal.domain_operation_runtime import tasks
    from src.internal.domain_operation_store import binding_for

    paid, body, _, _, _ = interrupted
    monkeypatch.setattr(routes, "monotonic", lambda: 0)
    envelope, _ = await obsolete_recovery_delivery(paid, bound=False)
    assert (await paid.post("/recover-uncredentialed", body, role="producer")).status_code == 200
    old = paid.store._read("TENANT#tenant", "EXEC#" + envelope["message_id"])
    for _ in range(duplicates):
        paid.sqs.send_message(QueueUrl=paid.queue, MessageBody=json.dumps(envelope))
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    fresh = dispatched.json()["invocation_id"]
    assert fresh != envelope["message_id"]
    paid.kubernetes[1].update(uid="pod-bounded", token_uid="pod-bounded")
    response = await paid.post("/task/acquire")
    assert response.status_code == 200, response.text
    delivery = tasks(paid.runtime, binding_for("superplane", ORG))
    journal = delivery.read("pod-bounded")
    assert len(journal["discarded_acknowledgements"]) == min(duplicates, 9)
    for version in journal["discarded_acknowledgements"]:
        archived = paid.store._read("PODTASK#pod-bounded", "DISCARDED#" + version)
        assert archived["invocation_id"] == {"S": envelope["message_id"]}
        assert archived["sqs_http_status"] == {"N": "200"}
    assert paid.store._read("TENANT#tenant", "EXEC#" + envelope["message_id"]) == old
    if duplicates == 11:
        assert response.json() == {"body": None} and journal["state"] == "acknowledged"
        assert (await paid.post("/task/acquire")).status_code == 403
        paid.kubernetes[1].update(uid="pod-next-batch", token_uid="pod-next-batch")
        response = await paid.post("/task/acquire")
        assert response.status_code == 200, response.text
    selected = json.loads(response.json()["body"])
    assert selected["message_id"] == fresh
    assert json.loads((await paid.post("/task/acquire")).json()["body"]) == selected
    bootstrapped = await paid.post("/bootstrap", {"invocation_id": fresh, "envelope_digest": envelope_digest(selected)})
    assert bootstrapped.status_code == 200, bootstrapped.text
    assert (await paid.post("/lease", {"operation_id": "original-operation"}, credential=bootstrapped.json()["credential"])).status_code == 200


async def test_discard_time_budget_preserves_last_ack_without_receiving_again(interrupted, monkeypatch):
    from src.internal import domain_operation_routes as routes
    from src.internal.domain_operation_runtime import tasks
    from src.internal.domain_operation_store import binding_for

    paid, body, _, _, _ = interrupted
    envelope, _ = await obsolete_recovery_delivery(paid, bound=False)
    assert (await paid.post("/recover-uncredentialed", body, role="producer")).status_code == 200
    for _ in range(2):
        paid.sqs.send_message(QueueUrl=paid.queue, MessageBody=json.dumps(envelope))
    times = iter([0, 9])
    monkeypatch.setattr(routes, "monotonic", lambda: next(times))
    paid.kubernetes[1].update(uid="pod-time-bound", token_uid="pod-time-bound")
    response = await paid.post("/task/acquire")
    assert response.status_code == 200, response.text
    assert response.json() == {"body": None}
    journal = tasks(paid.runtime, binding_for("superplane", ORG)).read("pod-time-bound")
    assert journal["state"] == "acknowledged" and journal["invocation_id"] == envelope["message_id"]
    assert not journal.get("discarded_acknowledgements")
    remaining = paid.sqs.get_queue_attributes(QueueUrl=paid.queue, AttributeNames=["ApproximateNumberOfMessages"])
    assert remaining["Attributes"]["ApproximateNumberOfMessages"] == "1"


@pytest.mark.parametrize("mutation", ["missing-recovery", "foreign-tenant", "foreign-operation", "changed-envelope", "live-grant", "missing-mapping"])
async def test_only_exact_expired_delivery_in_retained_recovery_proof_can_drain(interrupted, mutation):
    from src.internal.domain_operation_store import binding_for

    paid, body, _, lease, _ = interrupted
    envelope = original_envelope(paid)
    if mutation != "missing-recovery":
        assert (await paid.post("/recover-uncredentialed", body, role="producer")).status_code == 200
    if mutation == "foreign-tenant":
        envelope["tenant_id"] = "other"
    elif mutation == "foreign-operation":
        envelope["domain_operation"]["operation_id"] = "other"
    elif mutation == "changed-envelope":
        envelope["persona"] = "other"
    elif mutation == "live-grant":
        grant = paid.store._read("TENANT#tenant", "GRANT#" + lease.holder)
        grant["expires_at"] = {"S": (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
        paid.store.client.put_item(TableName=paid.store.table, Item=grant)
    elif mutation == "missing-mapping":
        item = next(row for row in paid.store.client.scan(TableName=paid.store.table)["Items"] if row["sk"]["S"].startswith("DOMAIN_OPERATION#"))
        paid.store.client.delete_item(TableName=paid.store.table, Key={k: item[k] for k in ("pk", "sk")})
    assert await owner.recovered_delivery(binding_for("superplane", ORG), envelope, paid.store) is False


@pytest.fixture
async def saved_completion(unstarted):
    paid, body, evidence, audit, db = unstarted
    async with paid.connect() as connection:
        await connection.execute("UPDATE operation_reauthorizations SET scope='saved-plan-completion'")
    names = {
        "workspace.tfplan",
        "workspace-plan.json",
        "workspace-inventory.json",
        "workspace-estimate.json",
        "workspace-authorization.proposed.json",
    }
    files = {name: "c" * 64 for name in names}
    evidence.update(evidence_kind="saved-plan-finalization-v1", planning_started=True, apply_started=False)
    evidence["preparation_boundary"] = {
        "kind": "saved-plan-finalization-v1",
        "entries": {"review/" + name: {"kind": "file", "sha256": value} for name, value in files.items()},
        "observation": {
            "version": 1,
            "terraform_version": "1.9.8",
            "initial_create_only": True,
            "offline_guard_passed": True,
            "files": files,
            "target": {**{k: evidence[k] for k in ("account_id", "org_id", "workspace_id")}, "aws_region": evidence["region"]},
        },
    }
    return paid, body, evidence, audit, db


async def test_saved_plan_completion_has_separate_truthful_proof(saved_completion):
    paid, body, evidence, audit, db = saved_completion
    assert (await paid.post("/recover-unstarted-planning", body, role="producer")).status_code != 200
    response = await paid.post("/complete-saved-plan", body, role="producer")
    assert response.status_code == 200, response.text
    async with paid.connect() as connection:
        row = await connection.fetchrow("SELECT * FROM harness_provider_retries")
        proof = json.loads(row["proof"])
        assert row["recovery_scope"] == "saved-plan-completion"
        assert proof["planning_started"] is True and proof["apply_started"] is False
        assert proof["issuance_audit_ids"] == [str(audit.id)]
        assert proof["preparation_boundary"] == evidence["preparation_boundary"]
    assert await db.get(AuditLog, audit.id) is not None


@pytest.mark.parametrize(
    "change", ["plan-hash", "report-missing", "no-guard", "applied", "attempt-extension", "expired", "live-session", "wrong-scope"]
)
async def test_saved_plan_completion_refuses_uncertain_evidence(saved_completion, change):
    paid, body, evidence, audit, db = saved_completion
    if change == "plan-hash":
        evidence["preparation_boundary"]["entries"]["review/workspace.tfplan"]["sha256"] = "d" * 64
    elif change == "report-missing":
        evidence["preparation_boundary"]["observation"]["files"].pop("workspace-inventory.json")
    elif change == "no-guard":
        evidence["preparation_boundary"]["observation"]["offline_guard_passed"] = False
    elif change == "applied":
        evidence["apply_started"] = True
    elif change == "attempt-extension":
        evidence["attempt_extension"] = {"additional_attempts": 1}
    elif change == "expired":
        evidence["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "live-session":
        audit.details = {**audit.details, "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
        await db.commit()
    else:
        async with paid.connect() as connection:
            await connection.execute("UPDATE operation_reauthorizations SET scope='unstarted-planning'")
    response = await paid.post("/complete-saved-plan", body, role="producer")
    assert response.status_code in {403, 409}, response.text
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert await connection.fetchval("SELECT stage FROM harness_provider_call_intent") == "intended"


async def test_expired_execution_proof_retains_original_producer_after_observer_claim(interrupted):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import fence_expired_lease

    from src.internal.domain_operation_dispatch import paid_operation

    paid = interrupted[0]
    async with paid.connect() as connection:
        original = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        from src.internal.domain_operation_store import binding_for

        binding = binding_for("superplane", ORG)
        operation = await paid_operation(binding, original["operation_id"], connection=connection)
        principal = ResolvedPrincipal(original["org_id"], original["workspace_id"], "observer", frozenset({"workspace:recover"}))
        takeover = await fence_expired_lease(connection, operation_id=original["operation_id"], recovery_principal=principal)
        assert takeover is not None
        current = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
    proof = await owner.expired_execution_proof(paid.store, binding, operation, current, source_principal=original["holder"])
    assert original["holder"] in {item["principal"] for item in proof}
    with pytest.raises(owner.HTTPException):
        await owner.expired_execution_proof(paid.store, binding, operation, current)
