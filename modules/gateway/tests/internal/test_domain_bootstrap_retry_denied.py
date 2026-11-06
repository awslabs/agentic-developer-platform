# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real shared operation and domain journals gate an original bootstrap retry."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from src.shared.models.audit import AuditLog
from tests.internal.test_domain_bootstrap_retry import bootstrap_interrupted as bootstrap_interrupted
from tests.internal.test_domain_bootstrap_retry import denied_bootstrap as denied_bootstrap
from tests.internal.test_domain_bootstrap_retry import unstarted_bootstrap as unstarted_bootstrap
from tests.internal.test_domain_dispatch_reauthorization import recovery as recovery
from tests.internal.test_paid_domain_operation_postgres import (
    ORG,
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


async def test_denied_first_grant_retry_retains_positive_revocation_and_attempts(denied_bootstrap):
    paid, body, evidence, journal = denied_bootstrap
    async with paid.connect() as c:
        before = dict(await c.fetchrow("SELECT * FROM harness_approval_consumption"))
    result = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert result.status_code == 200, result.text
    async with paid.connect() as c:
        proof = json.loads(await c.fetchval("SELECT proof FROM harness_provider_retries"))
        assert proof["bootstrap_mutations_started"] is True
        assert proof["bootstrap_grants_created"] is False
        assert proof["bootstrap_journal_revoked"] is True
        assert proof["denied_grant"] == evidence["denied_grant"]
        assert dict(await c.fetchrow("SELECT * FROM workspace_bootstrap_authority")) == journal
        assert dict(await c.fetchrow("SELECT * FROM harness_approval_consumption")) == before
        assert await c.fetchval("SELECT attempts FROM harness_operation_leases") == 1


@pytest.mark.parametrize(
    "fault",
    [
        "not-revoked",
        "not-complete",
        "identity",
        "second-grant",
        "extra-journal",
        "claim",
        "plan",
        "cluster",
        "reservation",
        "network",
        "membership",
        "artifact",
        "absent-label",
        "wrong-image",
        "denial-hash",
        "event",
        "absence",
    ],
)
async def test_denied_retry_refuses_ambiguous_journals_and_absence_claims(denied_bootstrap, fault):
    paid, body, evidence, journal = denied_bootstrap
    async with paid.connect() as c:
        if fault in {"not-revoked", "not-complete", "identity", "second-grant"}:
            progress = json.loads(journal["progress_json"])
            if fault == "not-revoked":
                await c.execute("UPDATE workspace_bootstrap_authority SET revoked=false")
            elif fault == "not-complete":
                del progress["complete"]
            elif fault == "identity":
                progress["registrar-entry"]["identity"] = {"arn": "observed"}
            else:
                progress["registrar-policy"] = {"phase": "revoked", "identity": None}
            await c.execute("UPDATE workspace_bootstrap_authority SET progress_json=$1", json.dumps(progress))
        elif fault == "extra-journal":
            await c.execute("INSERT INTO workspace_bootstrap_authority SELECT * FROM workspace_bootstrap_authority")
        elif fault == "claim":
            await c.execute("UPDATE workspace_bootstrap_authority SET claim=$1", "d" * 64)
        elif fault == "plan":
            plan = json.loads(journal["plan_json"])
            plan["grants"][1]["lifetime"] = "workspace"
            await c.execute("UPDATE workspace_bootstrap_authority SET plan_json=$1", json.dumps(plan))
        elif fault == "cluster":
            metadata = json.loads(await c.fetchval("SELECT artifact_metadata_json FROM workspace_lifecycle_artifacts"))
            metadata["outputs"]["cluster_arn"]["value"] += "-other"
            await c.execute("UPDATE workspace_lifecycle_artifacts SET artifact_metadata_json=$1", json.dumps(metadata))
        elif fault in {"reservation", "network", "membership"}:
            table = {
                "reservation": "workspace_bootstrap_reservations",
                "network": "workspace_lifecycle_effects",
                "membership": "cluster_memberships",
            }[fault]
            await c.execute("INSERT INTO " + table + " VALUES($1)", body["workspace_id"])
        elif fault == "artifact":
            await c.execute("INSERT INTO workspace_lifecycle_artifacts(source_operation_id) VALUES($1)", body["operation_id"])
        elif fault == "absent-label":
            evidence["evidence_kind"] = "bootstrap-journal-barrier-v1"
        elif fault == "wrong-image":
            from src.shared.domain_bootstrap_boundary import SOURCE_HASHES, SOURCE_IMAGE_DIGEST, SOURCE_REVISION

            evidence.update(source_image="qualified@" + SOURCE_IMAGE_DIGEST, source_revision=SOURCE_REVISION, source_hashes=SOURCE_HASHES)
        elif fault == "denial-hash":
            evidence["denial_source_hashes"] = {}
        elif fault == "event":
            evidence["denied_grant"]["event_ids"] = []
        else:
            evidence["denied_grant"]["absence_verified"] = False
    result = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert result.status_code == 409, result.text
    async with paid.connect() as c:
        assert await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert await c.fetchval("SELECT stage FROM harness_provider_call_intent") == "intended"


@pytest.mark.parametrize("fault", [None, "live-session", "changed-fence", "changed-holder", "completed-call", "cleanup-not-complete"])
async def test_owner_cleanup_requires_expired_current_call_and_protected_snapshot(denied_bootstrap, fault, monkeypatch):
    from src.internal import domain_bootstrap_recovery as bootstrap
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_runtime import bootstrap_store
    from src.internal.domain_operation_store import binding_for

    paid, body, evidence, journal = denied_bootstrap
    binding = binding_for("superplane", ORG)
    operation = await paid_operation(binding, body["operation_id"])
    async with paid.connect() as c:
        lease = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        call = dict(await c.fetchrow("SELECT * FROM harness_provider_call_intent"))
    # The fixture's SQLAlchemy audit session is available from the overridden
    # dependency, independent of the real shared/domain PostgreSQL store.
    from src.shared.database import get_db

    async for db in paid.client._transport.app.dependency_overrides[get_db]():
        if fault == "live-session":
            from sqlalchemy import select

            audit = (await db.execute(select(AuditLog))).scalars().one()
            audit.details = {**audit.details, "expires_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()}
            await db.commit()
        elif fault == "changed-fence":
            call["fence_token"] += 1
        elif fault == "changed-holder":
            lease["holder"] = "another#1"
        elif fault == "completed-call":
            call["stage"] = "reconciled"
        if fault and fault != "cleanup-not-complete":
            with pytest.raises(Exception):
                await bootstrap.cleanup_authority(db, bootstrap_store(), binding, operation, lease, call)
        else:
            result = await bootstrap.cleanup_authority(db, bootstrap_store(), binding, operation, lease, call)
            assert result["original_authority_expired"] and result["issued_sessions_expired"]
            assert result["source_fence"] == call["fence_token"]
            if fault == "cleanup-not-complete":
                with pytest.raises(Exception):
                    await bootstrap.cleanup_snapshot(binding, operation, complete=False)
            else:
                assert await bootstrap.cleanup_snapshot(binding, operation, complete=True) == journal


@pytest.mark.parametrize("fault", [None, "live-observer", "unbound", "wrong-source", "live-grant", "not-revoked"])
async def test_denied_retry_after_observer_keeps_original_execution_proof(denied_bootstrap, fault):
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import fence_expired_lease

    paid, body, evidence, journal = denied_bootstrap
    async with paid.connect() as c:
        original = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        observer = ResolvedPrincipal(body["org_id"], body["workspace_id"], "observer", frozenset({"workspace:recover"}))
        claim = await fence_expired_lease(c, operation_id=body["operation_id"], recovery_principal=None if fault == "unbound" else observer)
        assert claim is not None
        if fault != "live-observer":
            await c.execute(
                "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second',"
                "runtime_deadline=clock_timestamp()-interval '1 second'"
            )
        if fault == "not-revoked":
            await c.execute("UPDATE workspace_bootstrap_authority SET revoked=false")
        before = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
    if fault == "wrong-source":
        evidence["source_principal"] = "different#1"
    elif fault == "live-grant":
        # An observer claim is not proof that every original execution grant expired.
        for item in paid.store.client.scan(TableName=paid.store.table)["Items"]:
            if item["sk"]["S"].startswith("GRANT#"):
                item["expires_at"] = {"S": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
                paid.store.client.put_item(TableName=paid.store.table, Item=item)
    response = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert response.status_code == (409 if fault else 200), response.text
    async with paid.connect() as c:
        current = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        if fault:
            assert current == before
            assert await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        else:
            receipt = await c.fetchrow("SELECT * FROM harness_provider_retries")
            assert receipt["source_fence"] == original["fence_token"]
            assert receipt["source_attempt_id"] == original["attempt_id"]
            assert current["fence_token"] == claim.lease.fence_token + 1
            assert current["attempts"] == original["attempts"]
            assert current["max_attempts"] == original["max_attempts"]
            assert dict(await c.fetchrow("SELECT * FROM workspace_bootstrap_authority")) == journal
            assert json.loads(receipt["proof"])["owner_evidence"]["source_principal"] == original["holder"]
