"""Original-answer retrieval from a Product-authorized, version-pinned result."""

import io
from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from l5_services.adapters.finops.answer_reader import FinOpsAnswerReader
from l5_services.findings import ResultStore


RUN_ID = "11111111-1111-4111-8111-111111111111"
SCOPE = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
ANSWER = "Original native answer"
REFERENCE = {"artifact_id": f"l5/finops/work/{RUN_ID}/native/answers/original.txt",
             "version": "s3-version", "sha256": sha256(ANSWER.encode()).hexdigest()}


class VersionedS3:
    def __init__(self):
        self.calls = []
        self.contents = ANSWER.encode()
        self.version = REFERENCE["version"]

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"VersionId": self.version, "ContentLength": len(self.contents), "Body": io.BytesIO(self.contents)}


@pytest.mark.asyncio
async def test_read_only_original_version_and_digest():
    client = VersionedS3()
    reader = FinOpsAnswerReader("private-example", client)
    assert await reader.read(RUN_ID, REFERENCE) == ANSWER
    assert client.calls == [{"Bucket": "private-example", "Key": REFERENCE["artifact_id"],
                             "VersionId": REFERENCE["version"]}]
    for reference in ({**REFERENCE, "version": ""}, {**REFERENCE, "artifact_id": "l5/finops/other"}):
        with pytest.raises(ValueError, match="reference"):
            await reader.read(RUN_ID, reference)
    assert len(client.calls) == 1
    client.version = "wrong-version"
    with pytest.raises(ValueError, match="version"):
        await reader.read(RUN_ID, REFERENCE)
    client.version = REFERENCE["version"]
    client.contents = b"forged native answer"
    with pytest.raises(ValueError, match="digest"):
        await reader.read(RUN_ID, REFERENCE)


@pytest.mark.asyncio
async def test_result_scope_and_revision_are_checked_before_s3(monkeypatch):
    client = VersionedS3()
    run = SimpleNamespace(run_id=RUN_ID, admission={"scope": SCOPE},
                          provider_readback_ref={"answer_artifact": REFERENCE})
    store = ResultStore(None, [run], service_id="finops", actor_id="actor-example",
                        finops_answer_reader=FinOpsAnswerReader("private-example", client))
    result = {"result_id": "result-example", "run_id": RUN_ID, "revision": 1,
              "scope": SCOPE, "kind": "finops_cost_inquiry",
              "native": {"status": "answered", "answer_artifact": REFERENCE}}

    async def get(_result_id):
        return result

    monkeypatch.setattr(store, "get", get)
    assert (await store.finops_answer("result-example", 1))["text"] == ANSWER
    client.calls.clear()
    for mismatch in ({"revision": 2}, {"scope": {**SCOPE, "project_id": "foreign"}},
                     {"native": {"status": "answered", "answer_artifact": {**REFERENCE, "version": "other"}}},
                     {"native": {"status": "outcome_unknown", "answer_artifact": REFERENCE}}):
        async def mismatched(_result_id, mismatch=mismatch):
            return {**result, **mismatch}

        monkeypatch.setattr(store, "get", mismatched)
        with pytest.raises(HTTPException) as denied:
            await store.finops_answer("result-example", 1)
        assert denied.value.status_code == 404


@pytest.mark.asyncio
async def test_product_route_requires_matching_result_revision(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from l5_services.api.results import create_results_router

    run = SimpleNamespace(run_id=RUN_ID, admission={"scope": SCOPE},
                          provider_readback_ref={"answer_artifact": REFERENCE})
    store = ResultStore(None, [run], service_id="finops", actor_id="actor-example",
                        finops_answer_reader=FinOpsAnswerReader("private-example", VersionedS3()))

    async def get(_result_id):
        return {"result_id": "result-example", "run_id": RUN_ID, "revision": 1,
                "scope": SCOPE, "kind": "finops_cost_inquiry",
                "native": {"status": "answered", "answer_artifact": REFERENCE}}

    monkeypatch.setattr(store, "get", get)
    app = FastAPI()
    app.include_router(create_results_router(lambda: SimpleNamespace(result_store=lambda *args: get_store(store))),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    path = "/v1/products/product-example/projects/project-example/services/finops/results/result-example/native-answer"
    with TestClient(app) as client:
        response = client.get(path + "?revision=1")
        assert response.status_code == 200
        assert response.json()["text"] == ANSWER
        assert response.headers["cache-control"] == "no-store"
        assert client.get(path + "?revision=2").status_code == 404
        assert client.get(path).status_code == 422
        assert client.get(path.replace("/finops/", "/security/") + "?revision=1").status_code == 404


async def get_store(store):
    return store
