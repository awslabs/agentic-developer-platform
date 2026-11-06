# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Real shared operation and domain journals gate an original bootstrap retry."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.internal.test_domain_bootstrap_retry import bootstrap_interrupted as bootstrap_interrupted
from tests.internal.test_domain_bootstrap_retry import denied_bootstrap as denied_bootstrap
from tests.internal.test_domain_bootstrap_retry import unstarted_bootstrap as unstarted_bootstrap
from tests.internal.test_domain_bootstrap_retry_registrar import acquisition_row, registrar_row
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


@pytest.mark.parametrize(
    "fault", [None, "receipt", "key", "fence", "version", "journal", "extra-journal", "reservation", "foreign", "proof", "absent-label"]
)
async def test_later_unstarted_attempt_requires_exact_consumed_revocation(denied_bootstrap, monkeypatch, fault):
    from src.internal import domain_bootstrap_recovery as bootstrap
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for, harness
    from src.shared.domain_bootstrap_recovery import record_id, validate
    from src.shared.domain_bootstrap_retained import KIND

    paid, body, evidence, journal = denied_bootstrap
    response = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert response.status_code == 200, response.text
    binding = binding_for("superplane", ORG)
    monkeypatch.setattr(bootstrap, "operation_connect", paid.connect)
    operation = await paid_operation(binding, body["operation_id"])
    async with paid.connect() as c:
        prior = dict(await c.fetchrow("SELECT * FROM harness_provider_retries"))
    key, fence = prior["replacement_key"], prior["source_fence"] + 2
    retained = await bootstrap.retained_revocation(binding, operation, key, fence, journal)
    current = {**evidence, "evidence_kind": KIND, "source_key": key, "source_fence": fence, "retained_revocation": retained}
    current.pop("denied_grant")
    current.pop("denial_source_hashes")
    current["record_id"] = record_id(current)
    request = harness("identity").decode_payload(operation["request_payload"])
    original_proof = prior["proof"]
    if fault == "receipt":
        retained["reauthorization_id"] = "different"
    elif fault == "key":
        current["source_key"] = "another-call"
    elif fault == "fence":
        current["source_fence"] = prior["source_fence"]
    elif fault == "version":
        operation["version"] += 1
    elif fault == "foreign":
        retained["journal"]["workspace_id"] = "other"
    elif fault == "proof":
        retained["proof_sha256"] = "0" * 64
    elif fault == "absent-label":
        current["evidence_kind"] = "bootstrap-journal-barrier-v1"
    async with paid.connect() as c:
        if fault == "journal":
            await c.execute("UPDATE workspace_bootstrap_authority SET revoked=false")
        elif fault == "extra-journal":
            await c.execute("INSERT INTO workspace_bootstrap_authority SELECT * FROM workspace_bootstrap_authority")
        elif fault == "reservation":
            await c.execute("INSERT INTO workspace_bootstrap_reservations VALUES($1)", body["workspace_id"])
    current["record_id"] = record_id(current)
    if fault:
        with pytest.raises(Exception):
            validate(current)
            await bootstrap.untouched(binding, operation, request, current)
    else:
        validate(current)
        assert await bootstrap.untouched(binding, operation, request, current)
    async with paid.connect() as c:
        assert await c.fetchval("SELECT proof FROM harness_provider_retries") == original_proof
        assert await c.fetchval("SELECT attempts FROM harness_operation_leases") == 1


@pytest.mark.parametrize("fault", [None, "actor", "count", "ceiling", "cap", "missing-authorization"])
@pytest.mark.parametrize("complete_acquisition,additional_attempts", [(False, 5), (True, 1), (True, 10)])
async def test_gateway_binds_extension_to_actual_exhausted_lease(
    denied_bootstrap, monkeypatch, db_session, fault, complete_acquisition, additional_attempts
):
    from harness_jobs.execution import OperationExecutor
    from harness_jobs.leases import acquire

    from src.internal import domain_bootstrap_recovery as bootstrap
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for
    from src.shared.domain_bootstrap_recovery import record_id
    from src.shared.domain_bootstrap_retained import KIND

    paid, body, evidence, journal = denied_bootstrap
    previous_limit = 10 if complete_acquisition else 5
    async with paid.connect() as c:
        original = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
    response = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert response.status_code == 200, response.text
    binding = binding_for("superplane", ORG)
    monkeypatch.setattr(bootstrap, "operation_connect", paid.connect)
    async with paid.connect() as c:
        prior = dict(await c.fetchrow("SELECT * FROM harness_provider_retries"))
        lease = await acquire(c, operation_id=body["operation_id"], holder=original["holder"], attempt_id=original["attempt_id"])
    await OperationExecutor(lease, connect=paid.connect).record_intent(
        idempotency_key=prior["replacement_key"], provider="superplane-lifecycle", operation_kind="bootstrap-workspace", target="f" * 64
    )
    async with paid.connect() as c:
        # Exhaustion is fixture state only. Production recovery reads these counts.
        await c.execute(
            "UPDATE harness_operation_leases SET attempts=$1,max_attempts=$1,expires_at=now()-interval '1 hour',"
            "runtime_deadline=now()-interval '1 hour'",
            previous_limit,
        )
        stopped = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
        call = dict(await c.fetchrow("SELECT * FROM harness_provider_call_intent WHERE idempotency_key=$1", prior["replacement_key"]))
    operation = await paid_operation(binding, body["operation_id"])
    retained = await bootstrap.retained_revocation(binding, operation, call["idempotency_key"], call["fence_token"], journal)
    evidence.pop("denied_grant")
    evidence.pop("denial_source_hashes")
    evidence.update(
        evidence_kind=KIND,
        source_key=call["idempotency_key"],
        source_fence=call["fence_token"],
        source_acquired_at=stopped["acquired_at"].isoformat(),
        retained_revocation=retained,
    )
    if complete_acquisition:
        from src.shared.domain_bootstrap_boundary import QUALIFIED_SOURCES
        from src.shared.domain_bootstrap_revoked import ACQUISITION_KIND

        row = registrar_row(journal)
        plan = json.loads(row["plan_json"])
        plan["grants"][2].update(actor="registrar", lifetime="resource")
        plan["grants"][2]["body"].update(apiVersion="v1", metadata={"name": "superplane-system"})
        row["plan_json"] = json.dumps(plan)
        row = acquisition_row(row, complete=True)
        async with paid.connect() as c:
            await c.execute(
                "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9)",
                *[
                    row[k]
                    for k in ("workspace_id", "operation_id", "org_id", "cluster_arn", "generation", "claim", "revoked", "plan_json", "progress_json")
                ],
            )
        snapshot = await bootstrap.registrar_snapshot(
            binding, operation, call["idempotency_key"], call["fence_token"], retained_namespace=True, acquisition=True
        )
        source_image = "sha256:77695ebc2e5d141181908fa44020b88b0bdb6e1ff44f64cc0b23f1ee00fe6890"
        qualified = QUALIFIED_SOURCES[source_image]
        evidence.pop("retained_revocation")
        evidence.update(
            evidence_kind=ACQUISITION_KIND,
            revoked_bootstrap=snapshot["revoked_bootstrap"],
            source_image="qualified@" + source_image,
            source_revision=qualified["revision"],
            source_hashes=qualified["hashes"],
        )
    evidence["record_id"] = record_id(evidence)
    extension = dict(
        authorized_by=operation["requester"],
        authorization="Authorize up to ten additional attempts after full cleanup" if complete_acquisition else "Authorize five additional attempts",
        previous_limit=previous_limit,
        recorded_attempts=previous_limit,
        additional_attempts=additional_attempts,
    )
    evidence["attempt_extension"] = extension
    if fault == "actor":
        extension["authorized_by"] = "other"
    if fault == "count":
        stopped["attempts"] = previous_limit - 1
    if fault == "ceiling":
        stopped["max_attempts"] = previous_limit + 1
    if fault == "cap":
        extension["additional_attempts"] = 11 if complete_acquisition else 6
    if fault == "missing-authorization":
        extension.pop("authorization")
    if fault:
        with pytest.raises(ValueError):
            await bootstrap.prove(db_session, paid.store, binding, operation, stopped, call)
    else:
        proof = await bootstrap.prove(db_session, paid.store, binding, operation, stopped, call)
        assert proof["attempt_extension"] == proof["owner_evidence"]["attempt_extension"] == extension
    async with paid.connect() as c:
        assert await c.fetchval("SELECT attempts FROM harness_operation_leases") == previous_limit
        assert await c.fetchval("SELECT max_attempts FROM harness_operation_leases") == previous_limit
        assert dict(await c.fetchrow("SELECT * FROM workspace_bootstrap_authority WHERE generation=$1", journal["generation"])) == journal

    if not fault:
        from harness_jobs.identity import ResolvedPrincipal
        from harness_jobs.provider_retry import recover_uncredentialed

        async def authorize(*args):
            return datetime.now(UTC) + timedelta(hours=1)

        async def prove(current, lease, call):
            # The Gateway composition joins requester from the paid admission.
            return await bootstrap.prove(db_session, paid.store, binding, {**operation, **current}, lease, call)

        async with paid.connect() as c:
            second = await recover_uncredentialed(
                c,
                principal=ResolvedPrincipal(ORG, operation["workspace_id"], operation["requester"], frozenset({"workspace:provision"})),
                operation_id=operation["operation_id"],
                expected_version=operation["version"],
                reauthorization_id="second-retained-decision",
                authorize=authorize,
                prove_not_started=prove,
                recovery_scope="unstarted-bootstrap",
            )
        operation = await paid_operation(binding, body["operation_id"])
        if complete_acquisition:
            async with paid.connect() as c:
                assert await c.fetchval("SELECT attempts FROM harness_operation_leases") == 10
                assert await c.fetchval("SELECT max_attempts FROM harness_operation_leases") == 10 + additional_attempts
                assert dict(await c.fetchrow("SELECT * FROM workspace_bootstrap_authority WHERE generation=$1", row["generation"])) == row
        else:
            next_reference = await bootstrap.retained_revocation(binding, operation, second["replacement_key"], call["fence_token"] + 2, journal)
            assert next_reference["reauthorization_id"] == second["reauthorization_id"]
            assert next_reference["journal"] == retained["journal"]
