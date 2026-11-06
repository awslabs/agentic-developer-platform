"""Transform operations use the same scoped run and retained-result routes."""

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from l5_services.api.results import create_results_router
from l5_services.api.runs import create_runs_router
from l5_services.models.run import view_run

ROOT = "/v1/products/product-a/projects/project-a/services/transform"
SOURCE = {
    "tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "source-a",
    "record_type": "repository_commit", "record_id": "repo-a", "record_revision": "commit-a",
}
RUN_INPUT = {
    "service_id": "transform", "configuration_revision": 2, "binding_id": "binding-a",
    "capability": "refactor", "sources": [SOURCE],
    "transform_input": {"goal": {"kind": "refactor", "summary": "Update code"}},
}
RETAINED = {"result_id": "result-a", "run_id": "run-a", "revision": 3, "service_id": "transform",
            "verification_status": "unknown", "tests_before": [{"status": "failed"}],
            "tests_after": [{"status": "not_run"}], "limitations": ["unverified result"]}


class Backend:
    transform_submission_ready = False
    transform_result_reader_ready = False

    def __init__(self):
        self.calls = []
        self.run = SimpleNamespace(
            run_id="run-a", revision=2, state="outcome_unknown", provider_state="unknown",
            input=RUN_INPUT, admission={"scope": {"tenant_id": "tenant-a"}},
            scope={"tenant_id": "tenant-a"}, deadline_at=datetime(2027, 1, 1, tzinfo=UTC),
            native_job_ref=None, provider_readback_ref=None, monitoring=None,
            retry_of=None, origin="adp_submitted", service_id="transform",
        )

    async def submit(self, product_id, project_id, service_id, body, key):
        self.calls.append(("submit", product_id, project_id, service_id, body, key))
        return self.run

    async def list_runs(self, product_id, project_id, service_id, cursor):
        self.calls.append(("list", product_id, project_id, service_id, cursor))
        return [self.run]

    async def get_run(self, product_id, project_id, service_id, run_id):
        self.calls.append(("get", product_id, project_id, service_id, run_id))
        return self.run

    async def cancel_run(self, product_id, project_id, service_id, run_id, reason, key):
        self.calls.append(("cancel", product_id, project_id, service_id, run_id, reason, key))
        return self.run

    async def reconcile_run(self, product_id, project_id, service_id, run_id, reason, key):
        self.calls.append(("reconcile", product_id, project_id, service_id, run_id, reason, key))
        return self.run

    async def result_store(self, product_id, project_id, service_id):
        self.calls.append(("store", product_id, project_id, service_id))
        return ResultStore()


class ResultStore:
    transform_reader_ready = True

    async def list(self, cursor):
        return [RETAINED]

    async def get(self, result_id):
        if result_id != "result-a":
            raise HTTPException(404, detail={"code": "not_found"})
        return RETAINED


@pytest.mark.asyncio
async def test_transform_scoped_routes():
    backend = Backend()

    async def scoped_backend():
        return backend

    app = FastAPI()
    prefix = "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"
    app.include_router(create_runs_router(scoped_backend), prefix=prefix)
    app.include_router(create_results_router(scoped_backend), prefix=prefix)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unavailable = await client.post(f"{ROOT}/runs", json=RUN_INPUT, headers={"Idempotency-Key": "transform-route-001"})
        assert unavailable.status_code == 503
        assert unavailable.json()["detail"]["code"] == "capability_unavailable"
        assert not backend.calls
        listing = await client.get(f"{ROOT}/runs")
        detail = await client.get(f"{ROOT}/runs/run-a")
        assert listing.json()["items"] == [view_run(backend.run)]
        assert detail.json() == view_run(backend.run) and detail.headers["etag"] == '"2"'
        blocked = await client.get(f"{ROOT}/results")
        assert blocked.status_code == 503 and backend.calls[-1][0] == "get"
        backend.transform_submission_ready = True
        backend.transform_result_reader_ready = True
        admitted = await client.post(f"{ROOT}/runs", json=RUN_INPUT, headers={"Idempotency-Key": "transform-route-001"})
        assert admitted.status_code == 202 and admitted.headers["etag"] == '"2"'
        assert backend.calls[-1] == ("submit", "product-a", "project-a", "transform", RUN_INPUT, "transform-route-001")
        assert (await client.post(f"{ROOT}/runs/run-a/cancel", json={"reason": "stop"},
                                  headers={"Idempotency-Key": "transform-route-002"})).status_code == 202
        assert (await client.post(f"{ROOT}/runs/run-a/reconcile", json={"reason": "check"},
                                  headers={"Idempotency-Key": "transform-route-003"})).status_code == 202
        results = await client.get(f"{ROOT}/results")
        result = await client.get(f"{ROOT}/results/result-a")
        assert results.json()["items"] == [RETAINED]
        assert result.json() == RETAINED and result.headers["etag"] == '"3"'
        assert RETAINED["tests_before"][0]["status"] == "failed"
        assert RETAINED["tests_after"][0]["status"] == "not_run"
