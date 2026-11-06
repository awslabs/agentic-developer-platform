"""Offline broker and durable-work fixtures; no native provider effects."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from l5_services.adapters.transform.authority import TransformWorkAuthorizer
from l5_services.workers.execution import AuthorityRevoked


NOW = datetime(2026, 10, 29, tzinfo=UTC)
SCOPE = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
         "relationship_revision": "revision-a"}
CONNECTION = {"tenant_id": "tenant-a", "project_id": "project-a",
              "source_connection_id": "credential-a", "record_type": "connection",
              "record_id": "credential-a", "record_revision": "revision-a"}
IDENTITY = {"region": "us-east-1", "account_id": "123456789012",
            "role_arn": "arn:aws:iam::123456789012:role/example"}


def fixtures():
    admission = {"scope": deepcopy(SCOPE), "connection_ref": deepcopy(CONNECTION),
                 "transform_settings": {"connection_ref": deepcopy(CONNECTION), "region": "us-east-1"},
                 "delegation_id": "delegation-a", "delegation_version": 2, "binding_generation": 3,
                 "monitoring_policy": {"poll_grace_seconds": 30},
                 "limits": {"deadline_seconds": 900},
                 "provider_identity": deepcopy(IDENTITY)}
    run = SimpleNamespace(tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                          run_id="run-a", service_id="transform", state="queued", revision=4,
                          configuration_id="config-a", configuration_revision=1,
                          binding_id="binding-a", binding_generation=3, input={"capability": "refactor"},
                          admission=admission, deadline_at=NOW + timedelta(minutes=15),
                          capture_limits={"max_files": 10, "max_bytes": 1024})
    claim = SimpleNamespace(tenant_id=run.tenant_id, run_id=run.run_id, work_id="work-a",
                            owner="worker-a", fence=2, generation=1, kind="submit",
                            admission=deepcopy(admission), run_input=deepcopy(run.input),
                            run_revision=run.revision)
    return run, claim


def broker_receipt(run, claim):
    return {"operation_id": run.run_id, "attempt_id": claim.work_id,
            "delegation_revision": run.admission["delegation_version"],
            "provider_identity": deepcopy(IDENTITY),
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "access_key_id": "fixture-access", "secret_access_key": "fixture-secret",
            "session_token": "fixture-token"}


class Broker:
    def __init__(self, receipt, *, status=200):
        self.receipt = receipt
        self.status = status
        self.requests = []

    async def post(self, path, *, json):
        self.requests.append((path, json))
        return httpx.Response(self.status, json=self.receipt,
                              request=httpx.Request("POST", "https://example.invalid" + path))


@pytest.mark.asyncio
async def test_scoped_work_gets_only_pinned_session_and_source_request():
    run, claim = fixtures()
    broker = Broker(broker_receipt(run, claim))
    authorize = TransformWorkAuthorizer(broker, clock=lambda: NOW)
    authority = await authorize(run, claim, "submit")
    assert authority.scope == SCOPE and authority.capture_limits == run.capture_limits
    assert authority.provider_deadline_seconds == 900
    assert authority.source_request == {
        "delegation_id": "delegation-a", "delegation_revision": 2,
        "operation_id": "run-a", "attempt_id": "work-a",
        "lease_owner": "worker-a", "lease_fence": 2}
    assert broker.requests[0][1]["connection_ref"] == {**CONNECTION, "record_type": "credential"}
    assert authority.provider_credentials()["session_token"] == "fixture-token"
    authority.provider_credentials()["session_token"] = "changed"
    assert authority.provider_credentials()["session_token"] == "fixture-token"


@pytest.mark.asyncio
async def test_baseline_has_no_provider_credentials_or_broker_call():
    run, claim = fixtures()
    broker = Broker(broker_receipt(run, claim))
    authority = await TransformWorkAuthorizer(broker, clock=lambda: NOW)(run, claim, "verify_baseline")
    assert broker.requests == []
    with pytest.raises(AuthorityRevoked):
        authority.provider_credentials()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "foreign_product", "foreign_tenant", "foreign_connection", "revoked_scope",
    "stale_revision", "stale_admission", "stale_kind", "duplicate_fence", "expired_work",
    "stale_binding", "completed_run",
])
async def test_foreign_stale_or_expired_work_never_calls_broker(change):
    run, claim = fixtures()
    if change == "foreign_product":
        run.product_id = "product-b"
    elif change == "foreign_tenant":
        claim.tenant_id = "tenant-b"
    elif change == "foreign_connection":
        run.admission["transform_settings"]["connection_ref"]["record_id"] = "credential-b"
    elif change == "revoked_scope":
        run.admission["scope"]["project_id"] = "project-b"
    elif change == "stale_revision":
        claim.run_revision = 3
    elif change == "stale_admission":
        claim.admission["delegation_version"] = 1
    elif change == "stale_kind":
        claim.kind = "collect"
    elif change == "duplicate_fence":
        claim.fence = 0
    elif change == "expired_work":
        run.deadline_at = NOW
    elif change == "stale_binding":
        run.binding_generation = 4
    else:
        run.state = "succeeded"
    broker = Broker(broker_receipt(run, claim))
    with pytest.raises(AuthorityRevoked):
        await TransformWorkAuthorizer(broker, clock=lambda: NOW)(run, claim, "submit")
    assert broker.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    "missing_identity", "wrong_operation", "wrong_revision", "other_attempt",
    "foreign_identity", "foreign_role", "foreign_region", "expired_session",
    "unbounded_session", "missing_credentials",
])
async def test_unverified_broker_response_cannot_issue_provider_credentials(change):
    run, claim = fixtures()
    receipt = broker_receipt(run, claim)
    if change == "missing_identity":
        receipt.pop("provider_identity")
    elif change == "wrong_operation":
        receipt["operation_id"] = "run-b"
    elif change == "wrong_revision":
        receipt["delegation_revision"] = 1
    elif change == "other_attempt":
        receipt["attempt_id"] = "work-b"
    elif change == "foreign_identity":
        receipt["provider_identity"]["account_id"] = "000000000000"
    elif change == "foreign_role":
        receipt["provider_identity"]["role_arn"] = "arn:aws:iam::123456789012:role/other"
    elif change == "foreign_region":
        receipt["provider_identity"]["region"] = "us-west-2"
    elif change == "expired_session":
        receipt["expires_at"] = NOW.isoformat()
    elif change == "unbounded_session":
        receipt["expires_at"] = (NOW + timedelta(hours=1)).isoformat()
    else:
        receipt.pop("session_token")
