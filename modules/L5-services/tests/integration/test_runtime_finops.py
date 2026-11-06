"""FinOps worker mounts private resources and rejects unqualified native callbacks."""

import io
import json
import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from l5_services import runtime, runtime_finops
from l5_services.adapters.finops.inquiry import FinOpsInquiryAdapter
from l5_services.adapters.finops.billing import CostExplorerAdapter, normalize_pages
from l5_services.billing_collection import query_identity
from l5_services.workers.execution import AuthorityRevoked, load_service


def _mount(monkeypatch, response=None):
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    gateway = SimpleNamespace(post=AsyncMock(return_value=response))
    sessions = object()
    archive = object()
    publish = AsyncMock()

    def required(name):
        value = os.environ.get(name)
        if not value:
            raise ValueError(f"{name} is required")
        return value

    monkeypatch.setattr(runtime_finops, "_resources", lambda: SimpleNamespace(
        gateway_transport=lambda: gateway, sessions=lambda: sessions,
        archive_store=lambda service: archive if service == "finops" else None,
        queue_publisher=lambda service: publish if service == "finops" else None,
        ScopedSources=runtime._ScopedSources, required=required))
    return gateway, sessions, archive, publish


def _callbacks(names):
    return SimpleNamespace(**{name: AsyncMock() for name in names})


def _executor():
    return _callbacks(("submit", "read_submit", "observe", "read_answer", "read_native_cost", "stop"))


def _billing():
    return _callbacks(("collect_billing_page", "read_billing_page"))


def test_finops_factory_rejects_missing_executor_and_incomplete_callbacks(monkeypatch):
    _mount(monkeypatch)
    monkeypatch.delenv("L5_FINOPS_EXECUTOR_ARN", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with pytest.raises(ValueError, match="L5_FINOPS_EXECUTOR_ARN"):
        runtime_finops.factory()
    monkeypatch.setattr(runtime_finops, "_private_executor", lambda: _callbacks(("submit",)))
    monkeypatch.setattr(runtime_finops, "_independent_billing", _billing)
    with pytest.raises(ValueError, match="callbacks unavailable"):
        runtime_finops.factory()
    monkeypatch.setenv("L5_FINOPS_ALLOW_UNQUALIFIED", "true")
    with pytest.raises(ValueError, match="authority overrides"):
        runtime_finops.factory()
    monkeypatch.delenv("L5_FINOPS_ALLOW_UNQUALIFIED")
    monkeypatch.setenv("L5_SERVICE_ID", "security")
    with pytest.raises(ValueError, match="service identity"):
        runtime_finops.factory()


def test_finops_factory_uses_service_archive_and_installed_entry_point(monkeypatch):
    gateway, sessions, archive, publish = _mount(monkeypatch)
    executor, billing = _executor(), _billing()
    monkeypatch.setattr(runtime_finops, "_private_executor", lambda: executor)
    monkeypatch.setattr(runtime_finops, "_independent_billing", lambda: billing)
    monkeypatch.setenv("L5_WORKER_FACTORY", "l5_services.runtime_finops:factory")
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    service = load_service()
    assert service.service_id == "finops"
    assert service.sessions is sessions
    assert service.archive_store is archive
    assert service.publish is publish
    assert isinstance(service.finops_adapter, FinOpsInquiryAdapter)
    assert service.finops_adapter.executor is executor
    assert service.finops_adapter.billing is billing
    assert isinstance(service.source_client, runtime._ScopedSources)
    assert service.source_client.transport is gateway
    monkeypatch.setenv("L5_FINOPS_ALLOW_FOREIGN_SCOPE", "true")
    with pytest.raises(ValueError, match="authority overrides"):
        runtime_finops.factory()


def test_finops_factory_mounts_real_shared_resources_when_available(monkeypatch):
    resources = pytest.importorskip("l5_services.runtime_support")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "example-l5-finops-worker")
    monkeypatch.setenv("L5_SERVICE_ID", "finops")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("L5_GATEWAY_URL", "https://exampleapi.execute-api.us-east-1.amazonaws.com/dev")
    monkeypatch.setenv("L5_RDS_HOST", "example.invalid")
    monkeypatch.setenv("L5_RDS_DATABASE", "example")
    monkeypatch.setenv("L5_RDS_USER", "example")
    monkeypatch.setenv("L5_RDS_PORT", "5432")
    monkeypatch.setenv("L5_WORK_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/000000000000/example")
    monkeypatch.setenv("L5_ARCHIVE_BUCKET", "example-private-bucket")
    monkeypatch.setenv("L5_FINOPS_EXECUTOR_ARN", "arn:aws:lambda:us-east-1:000000000000:function:example-l5-finops-executor")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "example-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "example-secret")
    monkeypatch.setattr(runtime_finops, "_private_executor", lambda: _executor())
    monkeypatch.setattr(runtime_finops, "_independent_billing", lambda: _billing())
    service = runtime_finops.factory()
    assert service.service_id == "finops"
    assert isinstance(service.source_client, resources.ScopedSources)
    assert isinstance(service.finops_adapter, FinOpsInquiryAdapter)
    assert service.archive_store.service_id == "finops"
    assert service.archive_store.bucket == "example-private-bucket"
    assert callable(service.publish) and callable(service.sessions)


async def test_finops_broker_checks_pinned_identity_and_current_work(monkeypatch):
    identity = {"account_id": "000000000000", "role_arn": "arn:aws:iam::000000000000:role/Example",
                "region": "us-east-1"}
    claim = SimpleNamespace(work_id="work-example", owner="worker-example", fence=2, generation=3, kind="submit")
    run = SimpleNamespace(service_id="finops", input={"capability": "cost_inquiry"},
                          tenant_id="tenant-example", product_id="product-example", project_id="project-example",
                          binding_generation=3, revision=4,
                          run_id="run-example", admission={
                              "scope": {"tenant_id": "tenant-example", "product_id": "product-example",
                                        "project_id": "project-example"},
                              "delegation_id": "delegation-example", "delegation_version": 1,
                              "connection_ref": {"source_connection_id": "connection-example"},
                              "native_agent_ref": {"tenant_id": "tenant-example", "project_id": "project-example",
                                                   "source_connection_id": "connection-example",
                                                   "record_type": "finops_agent", "record_id": "agent-example"},
                              "billing_scope": {"scope_digest": "digest-example", "scope_generation": 3},
                              "provider_identity": identity})
    result = {"operation_id": run.run_id, "attempt_id": claim.work_id, "delegation_revision": 1,
              "provider_identity": identity, "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
    response = SimpleNamespace(status_code=200, json=lambda: result, raise_for_status=lambda: None)
    gateway, _, _, _ = _mount(monkeypatch, response)
    monkeypatch.setattr(runtime_finops, "_private_executor", _executor)
    monkeypatch.setattr(runtime_finops, "_independent_billing", _billing)
    service = runtime_finops.factory()
    assert await service.authorize(run, claim, "submit") == {
        **result, "lease_fence": claim.fence, "lease_owner": claim.owner,
        "tenant_id": run.tenant_id, "product_id": run.product_id, "project_id": run.project_id,
        "run_revision": run.revision, "work_generation": claim.generation, "work_kind": claim.kind,
        "delegation_id": run.admission["delegation_id"],
        "connection_ref": {**run.admission["connection_ref"], "record_type": "credential"}}
    assert gateway.post.await_args.kwargs["json"]["lease_fence"] == 2
    run.product_id = "foreign-product"
    with pytest.raises(AuthorityRevoked, match="scope"):
        await service.authorize(run, claim, "submit")
    run.product_id = "product-example"
    run.input["allow_foreign_scope"] = True
    with pytest.raises(AuthorityRevoked, match="scope"):
        await service.authorize(run, claim, "submit")
    run.input.pop("allow_foreign_scope")
    run.admission["billing_scope"]["scope_generation"] = 2
    with pytest.raises(AuthorityRevoked, match="scope"):
        await service.authorize(run, claim, "submit")
    run.admission["billing_scope"]["scope_generation"] = 3
    result["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with pytest.raises(AuthorityRevoked, match="expired"):
        await service.authorize(run, claim, "submit")
    result["expires_at"] = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    result["provider_identity"] = {**identity, "role_arn": "arn:aws:iam::000000000000:role/Foreign"}
    with pytest.raises(AuthorityRevoked, match="another work"):
        await service.authorize(run, claim, "submit")
    gateway.post.return_value = SimpleNamespace(status_code=409)
    with pytest.raises(AuthorityRevoked, match="current FinOps work"):
        await service.authorize(run, claim, "submit")


@pytest.mark.parametrize("identity", (None, {}, {"account_id": "000000000000", "region": "us-east-1",
    "role_arn": "arn:aws:iam::000000000001:role/Foreign"}, {"account_id": "000000000000", "region": "us-east-1",
    "role_arn": "arn:aws:iam::000000000000:role/Example", "access_key_id": "must-not-be-persisted"}))
async def test_admission_rejects_missing_mismatched_or_secret_provider_identity(identity):
    from fastapi import HTTPException
    from l5_services.admission import admit_run

    request = {"service_id": "finops", "capability": "cost_inquiry", "configuration_revision": 1,
               "binding_id": "binding-example", "question": "What was spent?",
               "query": {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost",
                         "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}}
    with pytest.raises(HTTPException) as failure:
        await admit_run(None, None, request, {"provider_identity": identity}, "example-receipt-0001")
    assert failure.value.status_code == 409 and failure.value.detail == {"code": "native_unqualified"}


async def test_inquiry_adapter_uses_native_readback_and_separate_billing_boundary():
    executor, billing = _executor(), _billing()
    adapter = FinOpsInquiryAdapter(executor, billing)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    agent = {"tenant_id": scope["tenant_id"], "project_id": scope["project_id"],
             "record_type": "finops_agent", "record_id": "agent-example"}
    question = "What was the cost?"
    question_hash = sha256(question.encode()).hexdigest()
    query = {"metric": "UnblendedCost"}
    manifest = {"scope": scope, "native_agent_ref": agent,
                "native_agent_display_name": "Example Agent", "question_hash": question_hash,
                "query": query, "run_id": "run-example", "binding_generation": 3}
    run = SimpleNamespace(service_id="finops", tenant_id=scope["tenant_id"],
                          product_id=scope["product_id"], project_id=scope["project_id"],
                          run_id="run-example", binding_generation=3, manifest=manifest,
                          input={"capability": "cost_inquiry", "question": question, "query": query},
                          admission={"scope": scope, "native_agent_ref": agent,
                                     "native_agent_display_name": "Example Agent",
                                     "billing_scope": {"scope_generation": 3}})
    input_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    token = sha256(f"{run.tenant_id}:{run.run_id}:{input_hash}".encode()).hexdigest()
    attempt = SimpleNamespace(run_id=run.run_id, question_hash=question_hash,
                              input_hash=input_hash, idempotency_token=token)
    native = {"tenant_id": run.tenant_id, "product_id": run.product_id,
              "project_id": run.project_id,
              "agent_id": agent["record_id"], "conversation_id": "conversation-example",
              "turn_id": "turn-example", "question_hash": question_hash}
    receipt = {"attempt_token": token, "native_job_ref": native}
    executor.submit.return_value = receipt
    executor.read_submit.return_value = receipt
    authority = object()
    assert (await adapter.submit(run, manifest, attempt, authority)).native_job_ref == native
    assert executor.submit.await_args.kwargs["authority"] is authority
    assert (await adapter.reconcile_submit(run, attempt, authority)).native_job_ref == native
    assert executor.submit.await_count == 1
    executor.observe.return_value = {"native_job_ref": native, "outcome": "completed",
                                     "observed_at": datetime.now(UTC).isoformat()}
    assert (await adapter.observe(run, native, attempt, authority)).classification == "completed"
    answer = {"kind": "finops_native_answer", "native_job_ref": native,
              "status": "answered", "observed_at": datetime.now(UTC).isoformat(),
              "answer_artifact": {"artifact_id": "answer-example", "version": "1", "sha256": "a" * 64}}
    executor.read_answer.return_value = answer
    assert (await adapter.collect(run, native, attempt, authority)).result == answer
    run.native_job_ref = native
    executor.read_native_cost.return_value = {"status": "answered"}
    assert await adapter.read_native_cost(run, attempt, authority) == {"status": "answered"}
    billing.collect_billing_page.return_value = {"response": {}}
    assert await adapter.collect_billing_page(run, attempt, authority, {}, None, "collection-example") == {
        "response": {}}
    billing.collect_billing_page.assert_awaited_once()

    executor.read_submit.return_value = {"attempt_token": token, "native_job_ref": {
        **native, "product_id": "foreign-product"}}
    assert await adapter.reconcile_submit(run, attempt, authority) is None
    for operation in (adapter.observe, adapter.collect, adapter.cancel):
        with pytest.raises(ValueError, match="pinned inquiry"):
            await operation(run, {**native, "product_id": "foreign-product"}, attempt, authority)
    run.native_job_ref = {**native, "product_id": "foreign-product"}
    with pytest.raises(ValueError, match="pinned inquiry"):
        await adapter.read_native_cost(run, attempt, authority)
    assert executor.observe.await_count == 1 and executor.read_answer.await_count == 1
    run.native_job_ref = native
    executor.read_submit.return_value = {"attempt_token": token, "native_job_ref": {
        **native, "project_id": "foreign-project"}}
    assert await adapter.reconcile_submit(run, attempt, authority) is None
    executor.observe.return_value = {"native_job_ref": {**native, "turn_id": "foreign-turn"},
                                     "outcome": "completed", "observed_at": datetime.now(UTC).isoformat()}
    with pytest.raises(ValueError, match="pinned native"):
        await adapter.observe(run, native, attempt, authority)
    executor.read_answer.return_value = {**answer, "native_job_ref": {**native, "turn_id": "foreign-turn"}}
    with pytest.raises(ValueError, match="original native"):
        await adapter.collect(run, native, attempt, authority)
    run.admission.pop("native_agent_display_name")
    with pytest.raises(ValueError, match="display name"):
        await adapter.reconcile_submit(run, attempt, authority)
    run.admission["native_agent_display_name"] = "Other Agent"
    with pytest.raises(ValueError, match="display name"):
        await adapter.reconcile_submit(run, attempt, authority)
    run.admission["native_agent_display_name"] = "Example Agent"
    attempt.question_hash = "foreign-question"
    with pytest.raises(ValueError, match="durable attempt"):
        await adapter.submit(run, manifest, attempt, authority)
    assert executor.submit.await_count == 1


async def test_cost_explorer_pages_are_independent_and_reject_foreign_scope():
    query = {"start": "2026-01-01", "end": "2026-02-01", "granularity": "MONTHLY",
             "metric": "UnblendedCost", "group_by": [], "currency": "USD"}
    account = "000000000000"
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    identity = {"account_id": account, "region": "us-east-1",
                "role_arn": f"arn:aws:iam::{account}:role/Selected"}
    billing = {"view_ref": f"arn:aws:billing::{account}:billingview/selected",
               "account_ids": [account], "scope_digest": "a" * 64, "scope_generation": 3}
    run = SimpleNamespace(service_id="finops", run_id="00000000-0000-0000-0000-000000000001",
                          tenant_id=scope["tenant_id"], product_id=scope["product_id"],
                          project_id=scope["project_id"], binding_generation=3,
                          input={"query": query}, admission={"scope": scope, "billing_scope": billing,
                                                               "provider_identity": identity})
    authority = {"access_key_id": "test-key", "secret_access_key": "test-secret",
                 "session_token": "test-session", "provider_identity": identity,
                 "expires_at": (datetime.now(UTC) + timedelta(minutes=1)).isoformat()}
    raw = {"GroupDefinitions": [], "ResultsByTime": [{
        "TimePeriod": {"Start": query["start"], "End": query["end"]}, "Estimated": False,
        "Total": {"UnblendedCost": {"Amount": "0.10", "Unit": "USD"}}}]}
    provider = SimpleNamespace(get_cost_and_usage=lambda **parameters: raw.copy())
    requests = []

    def provider_session(credentials, region):
        requests.append((credentials, region))
        return SimpleNamespace(client=lambda service: provider if service == "ce" else None)

    class ObjectStore:
        def __init__(self):
            self.objects = {}

        def put_object(self, *, Bucket, Key, Body):
            self.objects[Key] = Body
            return {"VersionId": "version-one"}

        def get_object(self, *, Bucket, Key, VersionId):
            return {"VersionId": "version-one", "ContentLength": len(self.objects[Key]),
                    "Body": io.BytesIO(self.objects[Key])}

    store = ObjectStore()
    adapter = CostExplorerAdapter("example-private-bucket", store, provider_session)
    request, digest = query_identity(run)
    collection_id = "00000000-0000-0000-0000-000000000002"
    page = await adapter.collect_billing_page(run, object(), authority, request, None, collection_id)
    assert requests == [(authority, "us-east-1")]
    assert page["artifact"]["artifact_id"].startswith(f"l5/finops/work/{run.run_id}/billing/")
    assert normalize_pages(query, billing["scope_digest"], digest, collection_id,
                           [page], datetime.now(UTC).isoformat())["amount"] == "0.10"
    assert await adapter.read_billing_page(run, object(), authority, collection_id, page["artifact"]) == page
    with pytest.raises(ValueError, match="another run or collection"):
        await adapter.read_billing_page(run, object(), authority,
                                        "00000000-0000-0000-0000-000000000003", page["artifact"])
    store.objects[page["artifact"]["artifact_id"]] = b'{"response": {"ResultsByTime": []}}'
    with pytest.raises(ValueError, match="pinned query or content"):
        await adapter.read_billing_page(run, object(), authority, collection_id, page["artifact"])
    run.project_id = "foreign-project"
    with pytest.raises(ValueError, match="Product scope"):
        await adapter.collect_billing_page(run, object(), authority, request, None, collection_id)
    assert len(requests) == 1
