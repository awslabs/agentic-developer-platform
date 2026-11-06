# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Fresh review, native closure, acquisition proof and dispatch agree end to end."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from src.internal.domain_bootstrap_recovery import read_evidence as protected_read
from tests.internal.test_domain_bootstrap_retry import bootstrap_interrupted as bootstrap_interrupted
from tests.internal.test_domain_bootstrap_retry import denied_bootstrap as denied_bootstrap
from tests.internal.test_domain_bootstrap_retry import unstarted_bootstrap as unstarted_bootstrap
from tests.internal.test_domain_bootstrap_retry_registrar import acquisition_row, registrar_row
from tests.internal.test_domain_bootstrap_runtime import amendment, review
from tests.internal.test_domain_dispatch_reauthorization import recovery as recovery
from tests.internal.test_paid_domain_operation_postgres import kubernetes as kubernetes
from tests.internal.test_paid_domain_operation_postgres import paid as paid
from tests.internal.test_paid_domain_operation_postgres import pg_server as pg_server
from tests.internal.test_paid_domain_operation_postgres import pg_url as pg_url
from tests.internal.test_paid_domain_operation_postgres import store as store


def management_components(row):
    """A fully installed, never admitted original management bootstrap."""
    row = copy.deepcopy(row)
    progress = json.loads(row["progress_json"])
    progress["workspace-namespace"]["phase"] = "granted"
    progress["workspace-namespace"]["identity"]["generation"] = row["generation"]
    ns = "superplane-system"
    progress.update(component_inventory_complete=True, component_inventory_mode="management", components={})
    for kind, namespace, name in (
        ("ServiceAccount", ns, "superplane-controller"),
        ("Role", ns, "superplane-controller-workspace"),
        ("RoleBinding", ns, "superplane-controller-workspace"),
        ("ClusterRole", "", "superplane-controller-superplane-system-cluster"),
        ("ClusterRoleBinding", "", "superplane-controller-superplane-system-cluster"),
    ):
        key = json.dumps([kind, namespace, name], separators=(",", ":"))
        progress["components"][key] = {
            "desired": {
                "kind": kind,
                "apiVersion": "v1" if kind == "ServiceAccount" else "rbac.authorization.k8s.io/v1",
                "metadata": {"namespace": namespace, "name": name},
            },
            "phase": "owned",
            "creation": "a" * 32,
            "identity": {"uid": kind + "-uid", "digest": "e" * 64, "creation": "a" * 32},
        }
    progress["management_observation"] = {
        **{k: row[k] for k in ("operation_id", "org_id", "workspace_id", "cluster_arn")},
        "registration_claim": row["claim"],
        "namespace": ns,
        "endpoint": "https://original.eks.invalid",
    }
    progress["prerequisite_inventory"] = {
        "workspace_id": row["workspace_id"],
        "prerequisites": [
            {"kind": kind, "identifier": identifier, "workspace_id": row["workspace_id"], "ownership": ownership, "reason": "original bootstrap"}
            for kind, identifier, ownership in (
                ("EksAccessEntry", progress["supervisor-entry"]["identity"]["arn"], "adp-created"),
                ("EksPublicEndpoint", row["cluster_arn"], "adopted"),
                ("SecurityGroupRule/private-sts", "sgr-12345678", "adopted"),
            )
        ],
    }
    progress["system_workload_baseline"] = {
        "version": 1,
        **{k: row[k] for k in ("operation_id", "cluster_arn", "generation")},
        "original_allocation_id": json.loads(row["plan_json"])["grants"][17]["original_allocation_id"],
        "objects": [{"identity": ["Service", "default", "kubernetes", "original-service"], "owner_references": []}],
    }
    row["progress_json"] = json.dumps(progress)
    return row


@pytest.fixture
async def settled_bootstrap(denied_bootstrap, monkeypatch, request):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.recovery import sweep_scoped_expired_leases
    from harness_jobs.settled_bootstrap import digest, read, review_terms

    from src.internal import domain_bootstrap_recovery as bootstrap
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for
    from src.shared.domain_bootstrap_boundary import qualified_source

    paid, body, evidence, old = denied_bootstrap
    row = registrar_row(old)
    plan = json.loads(row["plan_json"])
    plan["grants"][2].update(actor="registrar", lifetime="resource")
    plan["grants"][2]["body"].update(apiVersion="v1", metadata={"name": "superplane-system"})
    row["plan_json"] = json.dumps(plan)
    row = acquisition_row(row, complete=True)
    same_cap = getattr(request, "param", None) == "components-same-cap"
    components = getattr(request, "param", None) in {"components", "components-same-cap"}
    if components:
        row = management_components(row)
    monkeypatch.setattr(bootstrap, "operation_connect", paid.connect)

    async def retained(connection, lease, result):
        operation = await connection.fetchrow("SELECT * FROM harness_operations")
        accounting = {
            "action": "unknown",
            "budget": "retain",
            "release_permitted": False,
            "partial_lifecycle": {
                "phase": "bootstrap-workspace",
                "workflow_complete": False,
                "source_artifact_id": "f" * 64,
                "authority_generations": [{"generation": row["generation"], "claim": row["claim"], "plan_sha256": digest(row["plan_json"])}],
            },
        }
        await connection.execute(
            "INSERT INTO harness_recovery_settlements(receipt_id,operation_id,org_id,workspace_id,job_id,attempt_id,"
            "claim_holder,claim_attempt_id,claim_fence_token,payload_digest,accounting) "
            "VALUES('closed-fixture',$1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)",
            operation["operation_id"],
            operation["org_id"],
            operation["workspace_id"],
            operation["job_id"],
            operation["attempt_id"],
            lease.holder,
            lease.attempt_id,
            lease.fence_token,
            "a" * 64,
            json.dumps(accounting),
        )

    async with paid.connect() as connection:
        if same_cap:
            for table in ("harness_approval_consumption", "operation_approvals", "operation_budget_reservations"):
                await connection.execute("UPDATE " + table + " SET max_runtime_seconds=10800")
        await connection.execute("DELETE FROM workspace_bootstrap_authority")
        await connection.execute(
            "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
            *[
                row[k]
                for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "revoked", "plan_json", "progress_json")
            ],
        )
        result = await sweep_scoped_expired_leases(
            connection,
            principal=ResolvedPrincipal(body["org_id"], body["workspace_id"], "observer", frozenset({"workspace:recover"})),
            max_reconcile_attempts=1,
            on_settled=retained,
        )
        assert result.results[0].action == "unknown"
        await connection.execute("UPDATE harness_recovery_settlements SET delivered_at=clock_timestamp()")
        operation = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        call = dict(await connection.fetchrow("SELECT * FROM harness_provider_call_intent"))
        closure = await read(connection, operation, lease, call)
        body["expected_version"] = operation["version"]
        await connection.execute("UPDATE operation_reauthorizations SET expected_version=$1", operation["version"])
    binding = binding_for("superplane", body["org_id"])
    operation = await paid_operation(binding, body["operation_id"])
    observed = await bootstrap.registrar_snapshot(
        binding, operation, call["idempotency_key"], call["fence_token"], acquisition=not components, components=components
    )
    image = "qualified@sha256:" + (
        "845ce1dc5c4d3a1379c548e48d8fd269b1977118793738fc02a6883413fd6117"
        if components
        else "9fd2a13be7c5e147fe00cf5433fcd792ab751d27cd5ce86206c1d9bc326212bb"
    )
    qualified = qualified_source(image)
    evidence.pop("denied_grant")
    evidence.pop("denial_source_hashes")
    evidence.update(
        evidence_kind="bootstrap-management-components-revoked-v1" if components else "bootstrap-acquisition-revoked-v1",
        settled_bootstrap=closure,
        source_acquired_at=closure["source_audit"]["recorded_at"],
        source_image=image,
        source_revision=qualified["revision"],
        source_hashes=qualified["hashes"],
        revoked_bootstrap=observed["revoked_bootstrap"],
    )
    terms = amendment()
    if same_cap:
        terms.update(kind="bootstrap-runtime-reauthorization-v1", original_max_runtime_seconds=10800)
    await review(paid, review_terms(closure, terms))
    return paid, body, evidence


@pytest.mark.parametrize("settled_bootstrap", ["components"], indirect=True)
@pytest.mark.parametrize("fault", [None, "changed-snapshot", "changed-expiry", "live-session", "cleanup-present"])
async def test_component_owner_collection_rechecks_closure_revocation_and_expiry(settled_bootstrap, monkeypatch, fault):
    from pathlib import Path
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[4] / "modules/domain-apps/superplane"))
    from installation import bootstrap_components as owner

    paid, _, evidence = settled_bootstrap
    async with paid.connect() as connection:
        row = dict(await connection.fetchrow("SELECT * FROM workspace_bootstrap_authority"))
    descriptor = {k: evidence[k] for k in ("operation_id", "source_principal", "source_key", "source_fence", "settled_bootstrap")}
    expiry = {
        **descriptor,
        "original_authority_expired": True,
        "issued_sessions_expired": fault != "live-session",
        "earliest_owner_evidence_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    }
    snapshot = {"journals": [row], "revoked_bootstrap": evidence["revoked_bootstrap"]}
    reads = {"snapshot": 0, "expiry": 0}

    def read(which, original):
        reads[which] += 1
        value = copy.deepcopy(original)
        if reads[which] == 2 and fault == "changed-" + which:
            value["changed"] = True
        return value

    monkeypatch.setattr(owner, "collect", lambda *args, **kwargs: copy.deepcopy(evidence))
    observed = []
    monkeypatch.setattr(owner, "verify_absence", lambda *args, **kwargs: observed.append("actors"))

    def cleanup(*args):
        observed.append("cleanup")
        if fault == "cleanup-present":
            raise ValueError("cleanup principal remains")

    monkeypatch.setattr(owner, "verify_cleanup_absence", cleanup)

    def collect():
        return owner.collect_components(
            {"authority": {"region": "us-east-1"}},
            {},
            descriptor,
            SimpleNamespace(client=lambda *args, **kwargs: object()),
            read_snapshot=lambda: read("snapshot", snapshot),
            read_expiry=lambda: read("expiry", expiry),
        )

    if fault:
        with pytest.raises(ValueError):
            collect()
    else:
        assert collect() == evidence
        assert observed == ["actors", "cleanup"]
        assert reads == {"snapshot": 2, "expiry": 2}


@pytest.mark.parametrize("settled_bootstrap", ["components"], indirect=True)
@pytest.mark.parametrize(
    "fault", ["adopted", "intended", "missing", "extra", "creation", "uid", "namespace", "inventory", "baseline", "retain-false", "extra-marker"]
)
async def test_component_boundary_requires_exact_complete_original_ownership(settled_bootstrap, fault):
    from src.shared.domain_bootstrap_components import journal_identity

    paid, _, _ = settled_bootstrap
    async with paid.connect() as connection:
        row = dict(await connection.fetchrow("SELECT * FROM workspace_bootstrap_authority"))
    progress = json.loads(row["progress_json"])
    key = next(iter(progress["components"]))
    component = progress["components"][key]
    if fault in {"adopted", "intended"}:
        component["phase"] = fault
    elif fault == "missing":
        progress["components"].pop(key)
    elif fault == "extra":
        progress["components"]["foreign-component"] = copy.deepcopy(component)
    elif fault == "creation":
        component["identity"]["creation"] = "f" * 32
    elif fault == "uid":
        component["identity"]["uid"] = ""
    elif fault == "namespace":
        progress["management_observation"]["namespace"] = "foreign"
    elif fault == "inventory":
        progress["component_inventory_complete"] = False
    elif fault == "baseline":
        progress["system_workload_baseline"]["original_allocation_id"] = "foreign"
    elif fault == "retain-false":
        progress["retain_workspace"] = False
    elif fault == "extra-marker":
        progress["unrecognized_progress"] = True
    row["progress_json"] = json.dumps(progress)
    with pytest.raises(ValueError):
        journal_identity(row)


@pytest.mark.parametrize("settled_bootstrap", ["components", "components-same-cap"], indirect=True)
@pytest.mark.parametrize("fault", [None, "legacy-route", "changed-uid", "grant-survives", "admission-started", "unqualified-source"])
async def test_retained_management_components_resume_only_from_the_same_settled_source(settled_bootstrap, fault):
    from harness_jobs.provider_retry import bootstrap_no_effects, bootstrap_recoverable

    from src.shared.domain_bootstrap_acquisition import journal_identity as acquisition_identity

    paid, body, evidence = settled_bootstrap
    async with paid.connect() as connection:
        original = dict(await connection.fetchrow("SELECT * FROM workspace_bootstrap_authority"))
        closed = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        with pytest.raises(ValueError):
            acquisition_identity(original, complete=True)
        changed = json.loads(original["progress_json"])
        if fault == "changed-uid":
            next(iter(changed["components"].values()))["identity"]["uid"] = "replaced"
        elif fault == "grant-survives":
            changed["supervisor-entry"]["phase"] = "granted"
        elif fault == "admission-started":
            changed["retain_workspace"] = True
        elif fault == "unqualified-source":
            evidence["source_image"] = "unknown@sha256:" + "a" * 64
        if changed != json.loads(original["progress_json"]):
            await connection.execute("UPDATE workspace_bootstrap_authority SET progress_json=$1", json.dumps(changed))
    result = await paid.post("/recover-unstarted-bootstrap" if fault == "legacy-route" else "/recover-settled-bootstrap", body, role="producer")
    if fault:
        assert result.status_code in {403, 409}, result.text
        async with paid.connect() as connection:
            assert not await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == closed
        return
    assert result.status_code == 200, result.text
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    async with paid.connect() as connection:
        proof = json.loads(await connection.fetchval("SELECT proof FROM harness_provider_retries"))
        assert bootstrap_recoverable(proof) and not bootstrap_no_effects(proof)
        assert proof["bootstrap_components_retained"] is True
        assert proof["bootstrap_admission_started"] is False
        assert proof["settled_bootstrap"] == evidence["settled_bootstrap"]
        assert dict(await connection.fetchrow("SELECT * FROM workspace_bootstrap_authority")) == original
        assert await connection.fetchval("SELECT max_attempts FROM harness_operation_leases") == closed["max_attempts"]


@pytest.mark.parametrize("settled_bootstrap", ["components-same-cap"], indirect=True)
@pytest.mark.parametrize("fault", ["unwrapped", "extension-kind", "changed-cap", "expired", "foreign-closure", "undecided"])
async def test_same_cap_requires_exact_fresh_settled_review(settled_bootstrap, fault):
    paid, body, _ = settled_bootstrap
    async with paid.connect() as connection:
        before = {
            table: dict(await connection.fetchrow("SELECT * FROM " + table))
            for table in (
                "harness_operations",
                "harness_operation_leases",
                "harness_approval_consumption",
                "operation_approvals",
            )
        }
        terms = json.loads(await connection.fetchval("SELECT amendment_json FROM operation_reauthorizations"))
        if fault == "unwrapped":
            terms = terms["runtime_amendment"]
        elif fault == "extension-kind":
            terms["runtime_amendment"]["kind"] = "bootstrap-runtime-extension-v1"
        elif fault == "changed-cap":
            terms["runtime_amendment"]["max_runtime_seconds"] = 10801
        elif fault == "foreign-closure":
            terms["closure_sha256"] = "0" * 64
        elif fault == "expired":
            await connection.execute("UPDATE operation_reauthorizations SET expires_at=clock_timestamp()-interval '1 second'")
        elif fault == "undecided":
            await connection.execute("UPDATE operation_reauthorizations SET result=NULL,decided_at=NULL")
        await connection.execute("UPDATE operation_reauthorizations SET amendment_json=$1", json.dumps(terms))
    result = await paid.post("/recover-settled-bootstrap", body, role="producer")
    assert result.status_code == (409 if fault == "foreign-closure" else 403), result.text
    async with paid.connect() as connection:
        assert not await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
        assert {table: dict(await connection.fetchrow("SELECT * FROM " + table)) for table in before} == before


@pytest.mark.parametrize("fault", [None, "legacy-route", "foreign-closure", "changed-journal", "different-review", "original-phase"])
async def test_settled_boundary_is_approved_and_dispatched_for_three_hours(settled_bootstrap, fault):
    paid, body, evidence = settled_bootstrap
    async with paid.connect() as connection:
        original = dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption"))
        closed = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
        if fault == "foreign-closure":
            evidence["settled_bootstrap"]["operation"]["workspace_id"] = "foreign"
        elif fault == "changed-journal":
            await connection.execute("UPDATE workspace_bootstrap_authority SET claim=$1", "f" * 64)
        elif fault == "different-review":
            terms = json.loads(await connection.fetchval("SELECT amendment_json FROM operation_reauthorizations"))
            terms["closure_sha256"] = "0" * 64
            await connection.execute("UPDATE operation_reauthorizations SET amendment_json=$1", json.dumps(terms))
        elif fault == "original-phase":
            await connection.execute("UPDATE harness_operations SET state='failed'")
    result = await paid.post("/recover-unstarted-bootstrap" if fault == "legacy-route" else "/recover-settled-bootstrap", body, role="producer")
    if fault:
        assert result.status_code in {403, 409}, result.text
        async with paid.connect() as connection:
            assert not await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == closed
        return
    assert result.status_code == 200, result.text
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    remaining = (datetime.fromisoformat(dispatched.json()["not_after"].replace("Z", "+00:00")) - datetime.now(UTC)).total_seconds()
    assert 10000 < remaining <= 10800
    async with paid.connect() as connection:
        proof = json.loads(await connection.fetchval("SELECT proof FROM harness_provider_retries"))
        assert proof["settled_bootstrap"] == evidence["settled_bootstrap"]
        assert proof["runtime_amendment"] == amendment()
        assert dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption")) == original


@pytest.mark.parametrize("fault", [None, "live-grant", "successor-fence"])
async def test_settled_observer_delivery_uses_only_recorded_closed_claims(settled_bootstrap, fault):
    from src.internal import domain_provider_recovery as owner
    from src.internal.domain_operation_dispatch import paid_operation, provision
    from src.internal.domain_operation_runtime import tasks
    from src.internal.domain_operation_store import binding_for

    paid, body, evidence = settled_bootstrap
    binding = binding_for("superplane", body["org_id"])
    operation = await paid_operation(binding, body["operation_id"])
    identity = {key: operation[key] for key in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id")}
    identity.update(domain=binding.domain, domain_org_id=binding.org_id, adp_org_id=binding.adp_org_id, mode="recovery")
    closure = evidence["settled_bootstrap"]
    generation = closure["claims"][0]["fence_token"]
    if fault == "successor-fence":
        generation = closure["lease"]["fence_token"] + 1
    # Isolated historical task fixture using the real protected map/grant writer.
    pending = provision(paid.store, binding, identity, operation, generation)
    envelope = pending["envelope"]
    invocation = envelope["message_id"]
    expired = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for item in paid.store.client.scan(TableName=paid.store.table)["Items"]:
        if item["sk"]["S"].startswith("DOMAIN_OPERATION#"):
            mapping = json.loads(item["mapping"]["S"])
            if mapping["envelope"]["message_id"] == invocation:
                mapping["not_after"] = expired
                item["mapping"]["S"] = json.dumps(mapping)
                paid.store.client.put_item(TableName=paid.store.table, Item=item)
    grant = paid.store._read("TENANT#tenant", "GRANT#" + invocation + "#1")
    if fault != "live-grant":
        grant["expires_at"] = {"S": expired}
        paid.store.client.put_item(TableName=paid.store.table, Item=grant)
    assert await owner.recovered_delivery(binding, envelope, paid.store) is False
    response = await paid.post("/recover-settled-bootstrap", body, role="producer")
    assert response.status_code == 200, response.text
    if fault:
        assert await owner.recovered_delivery(binding, envelope, paid.store) is False
        return
    before = paid.store._read("TENANT#tenant", "EXEC#" + invocation)
    async with paid.connect() as connection:
        lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
    paid.sqs.send_message(QueueUrl=paid.queue, MessageBody=json.dumps(envelope))
    paid.kubernetes[1].update(uid="pod-closed-observer-drain", token_uid="pod-closed-observer-drain")
    response = await paid.post("/task/acquire")
    assert response.status_code == 200 and response.json() == {"body": None}, response.text
    journal = tasks(paid.runtime, binding).read("pod-closed-observer-drain")
    assert len(journal["discarded_acknowledgements"]) == 1
    assert paid.store._read("TENANT#tenant", "EXEC#" + invocation) == before
    assert paid.store._read("TENANT#tenant", "GRANT#" + invocation + "#1") == grant
    async with paid.connect() as connection:
        assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == lease


@pytest.mark.parametrize("race", [None, "changed-child", "appended-child"])
async def test_settled_recovery_consumes_successor_once_and_rereads_full_chain(settled_bootstrap, monkeypatch, race):
    from src.internal import domain_bootstrap_recovery as bootstrap
    from src.shared import domain_bootstrap_evidence as contract

    paid, body, fresh = settled_bootstrap
    fresh["source_audit"]["log_group"] = "original-logs"
    base = copy.deepcopy(fresh)
    now = datetime.now(UTC)
    base.update(checked_at=(now - timedelta(hours=2)).isoformat(), expires_at=(now - timedelta(hours=1)).isoformat())
    successor = contract.renew(fresh, base)

    def item(value):
        return {"record_id": {"S": value["record_id"]}, "document": {"S": contract.canonical(value)}, "revision": {"S": contract.digest(value)}}

    originals = [item(base), item(successor)]
    items = {value["record_id"]["S"]: copy.deepcopy(value) for value in originals}
    roots = 0

    class Client:
        def get_item(self, **kwargs):
            nonlocal roots
            assert kwargs["ConsistentRead"] is True
            key = kwargs["Key"]["record_id"]["S"]
            if key == base["record_id"]:
                roots += 1
                if roots == 2 and race == "changed-child":
                    changed = copy.deepcopy(successor)
                    changed["source_audit"]["historical_environment"] = {"changed": True}
                    items[successor["record_id"]] = item(changed)
                elif roots == 2 and race == "appended-child":
                    items[contract.successor(successor)[0]] = {}
            value = items.get(key)
            return {} if value is None else {"Item": copy.deepcopy(value)}

    monkeypatch.setattr(bootstrap, "read_evidence", protected_read)
    monkeypatch.setattr(bootstrap, "aws_client", lambda _: Client())
    monkeypatch.setattr(bootstrap, "tables", lambda: ("authority", "unused"))
    async with paid.connect() as connection:
        original_approval = dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption"))
        original_lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
    result = await paid.post("/recover-settled-bootstrap", body, role="producer")
    assert roots == 2
    if race:
        assert result.status_code in {403, 409}, result.text
        async with paid.connect() as connection:
            assert not await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == original_lease
        return
    assert result.status_code == 200, result.text
    async with paid.connect() as connection:
        proof = json.loads(await connection.fetchval("SELECT proof FROM harness_provider_retries"))
        assert proof["owner_evidence_id"] == successor["record_id"]
        assert proof["owner_evidence"]["renewal"]["generation"] == 1
        assert dict(await connection.fetchrow("SELECT * FROM harness_approval_consumption")) == original_approval
        resumed_lease = dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
    replay = await paid.post("/recover-settled-bootstrap", body, role="producer")
    assert replay.status_code == 200 and replay.json() == result.json(), replay.text
    async with paid.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_provider_retries") == 1
        assert dict(await connection.fetchrow("SELECT * FROM harness_operation_leases")) == resumed_lease
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    assert list(items.values()) == originals
