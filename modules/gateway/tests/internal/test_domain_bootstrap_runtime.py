# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Fresh native runtime review remains bound through recovery and dispatch."""

import json
from datetime import UTC, datetime

import pytest

from tests.internal.test_domain_bootstrap_retry import bootstrap_interrupted as bootstrap_interrupted
from tests.internal.test_domain_bootstrap_retry import unstarted_bootstrap as unstarted_bootstrap
from tests.internal.test_domain_dispatch_reauthorization import recovery as recovery
from tests.internal.test_paid_domain_operation_postgres import kubernetes as kubernetes
from tests.internal.test_paid_domain_operation_postgres import paid as paid
from tests.internal.test_paid_domain_operation_postgres import pg_server as pg_server
from tests.internal.test_paid_domain_operation_postgres import pg_url as pg_url
from tests.internal.test_paid_domain_operation_postgres import store as store


def amendment():
    return {
        "version": 1,
        "kind": "bootstrap-runtime-extension-v1",
        "original_max_runtime_seconds": 3600,
        "max_runtime_seconds": 10800,
    }


async def review(paid, terms):
    async with paid.connect() as db:
        await db.execute("ALTER TABLE operation_reauthorizations ADD COLUMN amendment_json text")
        await db.execute(
            "UPDATE operation_reauthorizations SET amendment_json=$1,expires_at=clock_timestamp()+interval '3 hours'",
            json.dumps(terms) if terms is not None else None,
        )


@pytest.mark.parametrize("extended", [False, True])
async def test_native_bootstrap_runtime_review_controls_new_dispatch_only(unstarted_bootstrap, extended):
    paid, body, evidence, audit, db = unstarted_bootstrap
    await review(paid, amendment() if extended else None)
    async with paid.connect() as c:
        tables = ("harness_approval_consumption", "operation_approvals", "operation_budget_reservations")
        original = {t: dict(await c.fetchrow("SELECT * FROM " + t)) for t in tables}
        request_payload = await c.fetchval("SELECT request_payload FROM harness_operations")
        deadline = await c.fetchval("SELECT expires_at FROM operation_reauthorizations")
    result = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert result.status_code == 200, result.text
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 200, dispatched.text
    expiry = datetime.fromisoformat(dispatched.json()["not_after"].replace("Z", "+00:00"))
    remaining = (expiry - datetime.now(UTC)).total_seconds()
    assert (10000 < remaining <= 10800) if extended else (3000 < remaining <= 3600)
    assert expiry <= deadline
    async with paid.connect() as c:
        proof = json.loads(await c.fetchval("SELECT proof FROM harness_provider_retries"))
        if extended:
            assert proof["runtime_amendment"] == amendment()
            assert datetime.fromisoformat(proof["runtime_authority_expires_at"]) == deadline
        else:
            assert "runtime_amendment" not in proof
        assert {t: dict(await c.fetchrow("SELECT * FROM " + t)) for t in tables} == original
        assert await c.fetchval("SELECT request_payload FROM harness_operations") == request_payload


@pytest.mark.parametrize("fault", ["undecided", "revoked", "original", "excess", "extra-field", "boolean"])
async def test_runtime_recovery_refuses_missing_or_changed_human_authority(unstarted_bootstrap, fault):
    paid, body, evidence, audit, db = unstarted_bootstrap
    terms = amendment()
    if fault == "original":
        terms["original_max_runtime_seconds"] = 900
    elif fault == "excess":
        terms["max_runtime_seconds"] = 10801
    elif fault == "extra-field":
        terms["unreviewed"] = True
    elif fault == "boolean":
        terms["max_runtime_seconds"] = True
    await review(paid, terms)
    async with paid.connect() as c:
        if fault == "undecided":
            await c.execute("UPDATE operation_reauthorizations SET result=NULL,decided_at=NULL")
        elif fault == "revoked":
            await c.execute("UPDATE operation_reauthorizations SET revoked=true")
        lease = dict(await c.fetchrow("SELECT * FROM harness_operation_leases"))
    result = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert result.status_code == 403, result.text
    async with paid.connect() as c:
        assert await c.fetchval("SELECT count(*) FROM harness_provider_retries") == 0
        assert dict(await c.fetchrow("SELECT * FROM harness_operation_leases")) == lease


@pytest.mark.parametrize("fault", ["terms", "expiry", "removed", "scope"])
async def test_dispatch_refuses_runtime_receipt_that_differs_from_current_review(unstarted_bootstrap, fault):
    paid, body, evidence, audit, db = unstarted_bootstrap
    await review(paid, amendment())
    result = await paid.post("/recover-unstarted-bootstrap", body, role="producer")
    assert result.status_code == 200, result.text
    async with paid.connect() as c:
        retained = await c.fetchval("SELECT proof FROM harness_provider_retries")
        if fault == "terms":
            await c.execute("UPDATE operation_reauthorizations SET amendment_json=$1", json.dumps({**amendment(), "max_runtime_seconds": 7200}))
        elif fault == "expiry":
            await c.execute("UPDATE operation_reauthorizations SET expires_at=expires_at-interval '1 second'")
        elif fault == "removed":
            await c.execute("UPDATE operation_reauthorizations SET amendment_json=NULL")
        else:
            await c.execute("UPDATE operation_reauthorizations SET scope='unstarted-planning'")
    dispatched = await paid.post("/dispatch", paid.body, role="producer")
    assert dispatched.status_code == 403, dispatched.text
    async with paid.connect() as c:
        assert await c.fetchval("SELECT proof FROM harness_provider_retries") == retained


async def test_fresh_recovery_does_not_inherit_expired_runtime_receipt(unstarted_bootstrap):
    from fastapi import HTTPException

    from src.internal.domain_operation_approval import current_approval
    from src.internal.domain_operation_dispatch import paid_operation
    from src.internal.domain_operation_store import binding_for, harness

    paid, body, evidence, audit, db = unstarted_bootstrap
    await review(paid, None)
    operation = await paid_operation(binding_for("superplane", body["org_id"]), body["operation_id"])
    operation.update(
        reauthorization_id=body["reauthorization_id"],
        recovery_expected_version=body["expected_version"],
        recovery_scope="unstarted-bootstrap",
        provider_recovery_proof=json.dumps({"runtime_amendment": amendment(), "runtime_authority_expires_at": "2020-01-01T00:00:00+00:00"}),
    )
    request = harness("identity").decode_payload(operation["request_payload"])
    async with paid.connect() as c:
        deadline = await current_approval(c, operation, request, pending_recovery=True)
        assert deadline > datetime.now(UTC)
        assert operation["bootstrap_runtime_amendment"] is None
        with pytest.raises(HTTPException) as refusal:
            await current_approval(c, operation, request)
        assert refusal.value.status_code == 403
