"""Offline transport tests of the installed result and artifact routes."""

import hashlib
import json
import time

import httpx
import pytest

from installed_results import read_investigation_result
from journey_driver import FirstPartyDriver

pytest_plugins = ("test_selector",)


def result_fixture(run):
    native = run["native_job_ref"]
    task = {"agentSpaceId": run["admission"]["native_space_ref"]["record_id"],
            "taskId": native["record_revision"], "executionId": native["record_id"],
            "taskType": "INVESTIGATION", "status": "COMPLETED"}
    evidence = {"kind": "devops_investigation", "native_job_ref": native, "task": task,
                "execution": {"executionId": task["executionId"], "agentSpaceId": task["agentSpaceId"]},
                "journal_records": [], "operational_recommendations": [],
                "costs": [{"kind": "unknown", "currency": "USD"}]}
    content = json.dumps(evidence).encode()
    result_id = "result-" + run["run_id"]
    result = {"result_id": result_id, "run_id": run["run_id"], "native_job_ref": native,
              "native_task_id": task["taskId"], "scope": run["scope"], "service_id": "devops",
              "origin": "adp_submitted", "provider_outcome": "completed", "admission": run["admission"],
              "revision": 1, "evidence_ref": {"artifact_id": "readback-" + result_id, "version": "1",
                  "media_type": "application/json", "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}}
    return result, content, task


def installed_result_response(request, run):
    result, content, _ = result_fixture(run)
    if request.url.path.endswith("/results"):
        return httpx.Response(200, json={"items": [result]})
    if request.url.path.endswith("/results/" + result["result_id"]):
        return httpx.Response(200, json=result)
    if request.url.path.endswith("/artifacts/" + result["evidence_ref"]["artifact_id"] + "/content"):
        return httpx.Response(200, content=content)
    return None


@pytest.mark.parametrize("mutation", [None, "missing", "foreign_scope", "foreign_task", "changed_digest",
                                     "foreign_evidence", "zero_cost", "duplicate", "repeated_cursor"])
def test_installed_readback_uses_scoped_routes_and_rejects_tampering(selector_case, mutation):
    manifest, _, directory, _ = selector_case
    run = {"run_id": "run-one", "scope": manifest["journey_driver"]["scope"],
           "native_job_ref": {"record_id": "execution-one", "record_revision": "task-one"},
           "admission": {"native_space_ref": {"record_id": "space-one"}}}
    result, content, task = result_fixture(run)
    if mutation == "foreign_scope":
        result["scope"] = {**run["scope"], "product_id": "foreign"}
    if mutation == "foreign_task":
        result["native_task_id"] = "foreign"
    if mutation == "changed_digest":
        result["evidence_ref"]["sha256"] = "0" * 64
    if mutation in {"foreign_evidence", "zero_cost"}:
        evidence = json.loads(content)
        if mutation == "foreign_evidence":
            evidence["task"]["executionId"] = "foreign"
        else:
            evidence["costs"] = [{"kind": "observed", "amount": "0", "currency": "USD"}]
        content = json.dumps(evidence).encode()
        result["evidence_ref"].update(bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    paths = []

    def transport(request):
        paths.append(request.url.path)
        assert request.url.host == "gateway.example.org"
        assert request.headers["Authorization"] == "Bearer private-product-token"
        assert "/products/example/projects/example/services/devops/" in request.url.path
        if request.url.path.endswith("/results"):
            items = [] if mutation == "missing" else [result, result] if mutation == "duplicate" else [result]
            return httpx.Response(200, json={"items": items, **({"next_cursor": "repeat"} if mutation == "repeated_cursor" else {})})
        if request.url.path.endswith("/results/" + result["result_id"]):
            return httpx.Response(200, json=result)
        assert request.url.path.endswith("/artifacts/" + result["evidence_ref"]["artifact_id"] + "/content")
        return httpx.Response(200, content=content)

    driver = FirstPartyDriver(manifest, directory, deadline=time.monotonic() + 20, max_requests=12)
    driver.client.close()
    driver.client = httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=False)
    try:
        if mutation:
            with pytest.raises(ValueError, match="installed investigation"):
                read_investigation_result(driver, run, task)
        else:
            assert read_investigation_result(driver, run, task) == {
                "result_id": result["result_id"], "evidence_ref": result["evidence_ref"]}
            assert len(paths) == 3
    finally:
        driver.close()
