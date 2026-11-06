# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Protected producer + real SQL/identity checks + durable transport recovery."""

import hashlib
import json
import os
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from src.internal import domain_current_identity, domain_dispatch_recovery
from src.internal.domain_operation_dispatch import canonical
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.internal.test_paid_domain_operation_postgres import (
    ORG,
    start,
    store,  # noqa: F401
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


@pytest.fixture
async def recovery(paid, db_session, monkeypatch):  # noqa: F811
    db_session.add(Organization(id="tenant", name="Recovery test"))
    await db_session.flush()
    for subject in ("human", "approver"):
        user = User(id=f"adp-{subject}", org_id="tenant", team_id="", email=f"{subject}@example.test", cognito_sub=subject)
        db_session.add(user)
        await db_session.flush()
        db_session.add(TenantMembership(user_id=user.id, tenant_id="tenant", is_active=True))
    await db_session.commit()

    @asynccontextmanager
    async def membership_session():
        yield db_session

    monkeypatch.setattr(domain_current_identity, "get_session_factory", lambda: membership_session)
    monkeypatch.setattr(domain_current_identity, "cognito_user_pool_id", lambda: "fixture-pool")
    cognito = MagicMock()
    cognito.list_users.side_effect = lambda **kw: {"Users": [{"Username": kw["Filter"].split('"')[1]}]}
    cognito.admin_get_user.side_effect = lambda **kw: {
        "Enabled": True,
        "UserAttributes": [
            {"Name": "sub", "Value": kw["Username"]},
            {"Name": "custom:org_id", "Value": "tenant"},
        ],
    }
    monkeypatch.setattr(domain_current_identity, "aws_client", lambda service: cognito)
    bindings = json.loads(os.environ["ADP_DOMAIN_OPERATION_BINDINGS"])
    bindings[0]["current_identity_enforced"] = True
    monkeypatch.setenv("ADP_DOMAIN_OPERATION_BINDINGS", json.dumps(bindings))
    monkeypatch.setattr(domain_dispatch_recovery, "domain_connect", paid.connect)
    monkeypatch.setattr(domain_dispatch_recovery, "operation_connect", paid.connect)
    async with paid.connect() as db:
        await db.execute("""INSERT INTO harness_dispatch_outbox
            (operation_id,job_id,attempt_id,org_id,workspace_id,action,request_payload,attempts,claim_generation,abandoned_at)
            SELECT operation_id,job_id,attempt_id,org_id,workspace_id,action,request_payload,10,10,now()
            FROM harness_operations""")
        await db.execute("UPDATE harness_operations SET state='unknown',version=2")
        await db.execute("UPDATE operation_approvals SET expires_at=now()-interval '1 hour'")
        await db.execute("""CREATE TABLE operation_reauthorizations (
            reauthorization_id text PRIMARY KEY, original_approval_id text, operation_id text,
            org_id text,workspace_id text,requester text,approved_by text,plan_digest text,
            expected_version bigint,result text,expires_at timestamptz,decided_at timestamptz,revoked boolean)""")
        await db.execute("""INSERT INTO operation_reauthorizations
            SELECT 'renewal-1',approval_id,operation_id,org_id,workspace_id,requester,approved_by,plan_digest,
            2,'allowed-once',now()+interval '1 hour',now(),false FROM harness_approval_consumption""")
    body = {k: paid.body[k] for k in ("domain", "org_id", "workspace_id", "operation_id")}
    body.update(requester="human", reauthorization_id="renewal-1", expected_version=2)
    return paid, body, cognito


@pytest.mark.parametrize("paid", [True, False], indirect=True)
async def test_reauthorization_resumes_original_dispatch_and_worker_lease(recovery):
    paid, body, cognito = recovery
    result = await paid.post("/recover-undispatched", body, role="producer")
    assert result.status_code == 200, result.text
    assert result.json()["restored_version"] == 3
    repeated = await paid.post("/recover-undispatched", body, role="producer")
    assert repeated.json() == result.json()
    dispatched, credential = await start(paid)
    lease = await paid.post("/lease", {"operation_id": body["operation_id"]}, credential=credential)
    assert lease.status_code == 200, lease.text
    assert lease.json()["operation_id"] == body["operation_id"]
    assert dispatched["attempt_id"] == "original-admission-attempt"
    assert cognito.admin_get_user.call_count >= 4
    async with paid.connect() as db:
        assert await db.fetchval("SELECT count(*) FROM harness_dispatch_reauthorizations") == 1
        assert await db.fetchval("SELECT count(*) FROM harness_approval_consumption") == 1
        assert await db.fetchval("SELECT expires_at<now() FROM operation_approvals") is True
        assert await db.fetchval("SELECT attempts FROM harness_dispatch_outbox") == 0
        await db.execute("UPDATE operation_reauthorizations SET revoked=true")
    denied = await paid.post("/authority", {"operation_id": body["operation_id"]}, credential=credential)
    assert denied.status_code == 403


@pytest.mark.parametrize(
    "failure",
    [
        "expired",
        "undecided",
        "revoked",
        "original-revoked",
        "wrong-requester",
        "wrong-workspace",
        "wrong-version",
        "wrong-producer",
        "membership",
        "mapped",
    ],
)
async def test_recovery_refuses_missing_authority_and_possible_dispatch(recovery, failure):
    paid, body, cognito = recovery
    role = "producer"
    sql = {
        "expired": "UPDATE operation_reauthorizations SET expires_at=now()-interval '1 second'",
        "undecided": "UPDATE operation_reauthorizations SET result=NULL,decided_at=NULL",
        "revoked": "UPDATE operation_reauthorizations SET revoked=true",
        "original-revoked": "UPDATE operation_approvals SET revoked=true",
    }
    if failure in sql:
        async with paid.connect() as db:
            await db.execute(sql[failure])
    elif failure == "wrong-requester":
        body["requester"] = "approver"
    elif failure == "wrong-workspace":
        body["workspace_id"] = "foreign"
    elif failure == "wrong-version":
        body["expected_version"] = 3
    elif failure == "wrong-producer":
        role = "worker"
    elif failure == "membership":
        cognito.admin_get_user.return_value = {"Enabled": False}
        cognito.admin_get_user.side_effect = None
    else:
        identity = {k: paid.body[k] for k in ("operation_id", "job_id", "attempt_id", "org_id", "workspace_id", "domain", "mode")}
        identity.update(domain_org_id=ORG, adp_org_id="tenant")
        digest = hashlib.sha256(canonical({**identity, "generation": 0, "bootstrap_retry": 4}).encode()).hexdigest()
        paid.store.client.put_item(
            TableName=paid.store.table,
            Item={
                "pk": {"S": "TENANT#tenant"},
                "sk": {"S": "DOMAIN_OPERATION#" + digest},
                "mapping": {"S": "{}"},
            },
        )
    response = await paid.post("/recover-undispatched", body, role=role)
    assert response.status_code in {403, 409}, (failure, response.text)
    async with paid.connect() as db:
        assert await db.fetchval("SELECT state FROM harness_operations") == "unknown"
        assert await db.fetchval("SELECT count(*) FROM harness_dispatch_reauthorizations") == 0
    assert "Messages" not in paid.sqs.receive_message(QueueUrl=paid.queue)
