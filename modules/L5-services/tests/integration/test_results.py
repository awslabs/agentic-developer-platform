"""Read public findings independently of the worker's durable result rows."""

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.api.router import create_router
from l5_services.findings import ResultStore
from l5_services.models import AssessmentResult, Configuration, ConfigurationVersion, Finding, ResultDetail, Run, Triage, Work
from l5_services.models.base import Base
from l5_services.run_access import RunStore
from l5_services.work_store import claim_work, finish_work
from tests.migrations.conftest_postgres import to_async_url, upgrade
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
async def results_scenario(tmp_path, request):
    schema_source = getattr(request, "param", "orm")
    if schema_source == "gateway_postgres":
        database_url = request.getfixturevalue("pg_url")
        upgrade(database_url)
        engine = create_async_engine(to_async_url(database_url))
    else:
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'results.db'}")
        async with engine.begin() as connection:
            await connection.run_sync(upgrade_l5_gateway_schema if schema_source == "gateway_sqlite" else Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "connector-one",
              "record_type": "repository", "record_id": "repository-one", "record_revision": "rev-one"}
    current = {"tenant_id": "tenant-one", "product_id": "product-one", "actor_id": "owner-one",
               "sources_allowed": True, "permitted_sources": None, "relationship_revision": "1"}

    async with sessions() as session:
        for tenant_id, product_id, suffix in (("tenant-one", "product-one", "one"),
                                               ("tenant-one", "product-other", "other"),
                                               ("tenant-two", "product-two", "two")):
            configuration_id = f"config-{suffix}"
            session.add(Configuration(tenant_id=tenant_id, configuration_id=configuration_id,
                                      product_id=product_id, project_id="project-one", service_id="security",
                                      relationship_revision="1", owner_id=f"owner-{suffix}", current_revision=1,
                                      handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=tenant_id, configuration_id=configuration_id,
                                             revision=1, settings={}, inherited_fields=[], gaps=[],
                                             state="ready", hash="f" * 64))
        await session.commit()

    class Backend:
        def __init__(self, session):
            self.session = session

        def run_store(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id) != (current["product_id"], current.get("project_id", "project-one"), current.get("service_id", "security")):
                raise HTTPException(404, detail={"code": "not_found"})
            return RunStore(self.session, current["tenant_id"], product_id, project_id, service_id,
                            current["actor_id"], current["relationship_revision"])

        async def get_run(self, product_id, project_id, service_id, run_id):
            if not current["sources_allowed"]:
                raise HTTPException(404, detail={"code": "not_found"})
            return await self.run_store(product_id, project_id, service_id).get(run_id)

        async def list_runs(self, product_id, project_id, service_id, cursor=None):
            if not current["sources_allowed"]:
                return []
            return await self.run_store(product_id, project_id, service_id).list(cursor=cursor)

        async def result_store(self, product_id, project_id, service_id):
            if (product_id, project_id, service_id) != (current["product_id"], current.get("project_id", "project-one"), current.get("service_id", "security")):
                raise HTTPException(404, detail={"code": "not_found"})
            if not current["sources_allowed"]:
                return ResultStore(self.session, [], actor_id=current["actor_id"], clock=lambda: NOW)
            store = RunStore(self.session, current["tenant_id"], product_id, project_id, service_id,
                             current["actor_id"], current["relationship_revision"])
            runs = await store.list()
            if current["permitted_sources"] is not None:
                runs = [run for run in runs if all(
                    (source["source_connection_id"], source["record_id"]) in current["permitted_sources"]
                    for source in run.input["sources"])]
            return ResultStore(self.session, runs, actor_id=current["actor_id"], clock=lambda: NOW)

    app = FastAPI()

    async def backend():
        async with sessions() as session:
            yield Backend(session)

    app.include_router(create_router(backend))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            yield {"client": client, "sessions": sessions, "source": source, "current": current,
                   "backend_factory": Backend}
    finally:
        await engine.dispose()


async def collect_result(scenario, run_id, *, capability, findings, coverage="complete", product_id="product-one",
                         tenant_id="tenant-one", owner_id="owner-one", source=None, files=None,
                         provider_version="provider-v1", rule_version="rules-v1", manifest_ref=None):
    sessions = scenario["sessions"]
    source = source or scenario["source"]
    suffix = {"product-one": "one", "product-other": "other", "product-two": "two"}[product_id]
    native = {"tenant_id": tenant_id, "project_id": "project-one", "source_connection_id": "provider",
              "record_type": "job", "record_id": f"job-{run_id}", "record_revision": "1"}
    async with sessions() as session:
        session.add(Run(tenant_id=tenant_id, run_id=run_id, product_id=product_id, project_id="project-one",
                        service_id="security", configuration_id=f"config-{suffix}", configuration_revision=1,
                        binding_id="binding", trigger_id="trigger", binding_generation=1, revision=1,
                        input={"capability": capability, "sources": [source]},
                        admission={"scope": {"tenant_id": tenant_id, "product_id": product_id,
                                             "project_id": "project-one", "relationship_revision": "1",
                                             "owner_id": owner_id, "visibility": "only_me"},
                                   "adapter_revision": "adapter-one"},
                        state="collecting", provider_state="completed", native_job_ref=native,
                        manifest={"files": [{"source_ref": source, "path": path, "sha256": digest}
                                            for path, digest in (files or {}).items()]},
                        deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id=tenant_id, work_id=f"work-{run_id}", run_id=run_id, kind="collect",
                         generation=1, step=0, due_at=NOW))
        await session.commit()
    claim = await claim_work(sessions, f"work-{run_id}", 1, now=NOW)
    assert claim is not None
    result = {"native_job_ref": native, "coverage": coverage,
              "provider_version": provider_version, "rule_version": rule_version,
              "coverage_mode": "full_snapshot", "findings": findings}
    if manifest_ref is not None:
        result["manifest_ref"] = manifest_ref
    assert await finish_work(sessions, claim, state="succeeded", provider_state="completed", now=NOW,
                             result=result) == "completed"
    return claim, result


async def test_normalization_and_readback(results_scenario):
    scenario = results_scenario
    source = scenario["source"]
    threat = {"native_id": "threat-native", "kind": "candidate_threat", "claim": "possible attack",
              "provider_severity": "medium", "source_refs": [source]}
    scan = {"native_id": "scan-native", "kind": "static_finding", "claim": "code issue",
            "provider_severity": "high", "validation": "supported", "source_refs": [source]}
    claim, provider_result = await collect_result(scenario, "run-threat", capability="threat_model",
                                                  findings=[threat, threat])
    await collect_result(scenario, "run-scan", capability="code_scan", findings=[scan])
    assert await finish_work(scenario["sessions"], claim, state="succeeded", provider_state="completed",
                             now=NOW, result=provider_result) is None
    await collect_result(scenario, "run-other", capability="threat_model", findings=[threat],
                         product_id="product-other", owner_id="owner-other")
    foreign_source = {**source, "tenant_id": "tenant-two"}
    await collect_result(scenario, "run-foreign", capability="threat_model",
                         findings=[{**threat, "source_refs": [foreign_source]}],
                         tenant_id="tenant-two", product_id="product-two", owner_id="owner-two",
                         source=foreign_source)
    async with scenario["sessions"]() as independent:
        raw = (await independent.scalars(select(AssessmentResult))).all()
        normalized = (await independent.scalars(select(Finding))).all()
        details = (await independent.scalars(select(ResultDetail))).all()
        assert len(raw) == len(details) == 4
        assert len(normalized) == 4
        assert {row.native_id for row in normalized} == {"threat-native", "scan-native"}
        assert len({row.finding_id for row in normalized if row.native_id == "threat-native"}) == 3
        assert len({row.result_id for row in normalized if row.native_id == "threat-native"}) == 3
        assert raw[0].payload["findings"]
    path = "/v1/products/product-one/projects/project-one/services/security/results"
    response = await scenario["client"].get(path)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert len(items) == 2
    assert {item["finding_count"] for item in items} == {1}
    assert all(item["costs"] == [{"kind": "unknown", "currency": "USD"}] for item in items)
    for item in items:
        findings = (await scenario["client"].get(f'{path}/{item["result_id"]}/findings')).json()["items"]
        assert len(findings) == 1
        if item["run_id"] == "run-threat":
            assert findings[0]["validation"] == "candidate"
        else:
            assert findings[0]["native_id"] == "scan-native"
            assert findings[0]["validation"] == "supported"
        assert findings[0]["native_job_ref"] == item["native_job_ref"]
        assert (await scenario["client"].get(f'{path}/{item["result_id"]}')).json() == item
    scenario["current"].update(product_id="product-other", actor_id="owner-other")
    other = (await scenario["client"].get(path.replace("product-one", "product-other"))).json()["items"]
    assert len(other) == 1 and other[0]["run_id"] == "run-other"
    scenario["current"].update(tenant_id="tenant-two", product_id="product-two", actor_id="owner-two")
    foreign = (await scenario["client"].get(path.replace("product-one", "product-two"))).json()["items"]
    assert len(foreign) == 1 and foreign[0]["run_id"] == "run-foreign"


async def test_comparison_and_triage(results_scenario):
    scenario = results_scenario
    client = scenario["client"]
    path = "/v1/products/product-one/projects/project-one/services/security"
    source = scenario["source"]
    baseline_only = {"native_id": "baseline-only", "kind": "static_finding", "claim": "previous issue",
                     "provider_severity": "medium", "validation": "supported", "source_refs": [source],
                     "locations": ["src/same.py:10"]}
    shared = {**baseline_only, "native_id": "shared", "claim": "same issue"}
    added = {**baseline_only, "native_id": "added", "claim": "new issue"}
    await collect_result(scenario, "run-base", capability="code_review", findings=[baseline_only, shared],
                         files={"src/same.py": "a" * 64})
    await collect_result(scenario, "run-next", capability="code_review", findings=[shared, added],
                         files={"src/same.py": "a" * 64})
    await collect_result(scenario, "run-partial", capability="code_review", findings=[],
                         files={"src/same.py": "a" * 64}, coverage="partial")
    await collect_result(scenario, "run-changed", capability="code_review", findings=[],
                         files={"src/same.py": "b" * 64})
    await collect_result(scenario, "run-different", capability="code_review", findings=[],
                         files={"src/same.py": "a" * 64}, source={**source, "record_id": "another-repository"})
    await collect_result(scenario, "run-other-provider", capability="code_review", findings=[],
                         files={"src/same.py": "a" * 64}, provider_version="provider-v2")
    await collect_result(scenario, "run-other-product", capability="code_review", findings=[],
                         files={"src/same.py": "a" * 64}, product_id="product-other", owner_id="owner-other")
    results = {item["run_id"]: item for item in (await client.get(path + "/results")).json()["items"]}

    async def findings(run_id):
        result_id = results[run_id]["result_id"]
        return {item["native_id"]: item for item in
                (await client.get(f"{path}/results/{result_id}/findings")).json()["items"]}

    baseline = await findings("run-base")
    candidate = await findings("run-next")

    async def compare(candidate_id):
        response = await client.post(path + "/comparisons", json={
            "baseline_result_id": results["run-base"]["result_id"], "candidate_result_id": candidate_id,
        })
        assert response.status_code == 200, response.text
        return response.json()

    matching = await compare(results["run-next"]["result_id"])
    schema = json.loads((Path(__file__).resolve().parents[4] /
                         "docs/l5-services/contracts/v1/schemas.json").read_text())
    Draft202012Validator({**schema, "$ref": "#/$defs/Comparison"}).validate(matching)
    Draft202012Validator({**schema, "$ref": "#/$defs/Result"}).validate(results["run-next"])
    Draft202012Validator({**schema, "$ref": "#/$defs/Finding"}).validate(candidate["shared"])
    assert matching == {"comparability": "comparable", "reasons": [],
                        "baseline_revision": 1, "candidate_revision": 1,
                        "new_findings": [candidate["added"]["finding_id"]],
                        "unchanged_findings": [candidate["shared"]["finding_id"]],
                        "resolved_findings": [baseline["baseline-only"]["finding_id"]]}
    partial = await compare(results["run-partial"]["result_id"])
    assert partial["comparability"] == "partial" and partial["resolved_findings"] == []
    assert "coverage_incomplete" in partial["reasons"]
    changed = await compare(results["run-changed"]["result_id"])
    assert changed["comparability"] == "partial" and changed["resolved_findings"] == []
    assert "source_files_changed_or_unscanned" in changed["reasons"]
    different = await compare(results["run-different"]["result_id"])
    assert different["comparability"] == "incomparable" and different["resolved_findings"] == []
    assert "source_selection_changed" in different["reasons"]
    other_provider = await compare(results["run-other-provider"]["result_id"])
    assert other_provider["comparability"] == "incomparable" and other_provider["resolved_findings"] == []
    assert "provider_version_changed" in other_provider["reasons"]
    foreign_id = (await client.get(path.replace("product-one", "product-other") + "/results")).status_code
    assert foreign_id == 404
    triage_path = f'{path}/results/{results["run-base"]["result_id"]}/findings/{baseline["shared"]["finding_id"]}/triage'
    initial = await client.get(triage_path)
    assert initial.status_code == 200 and initial.headers["etag"] == '"0"' and initial.json() == {"items": []}
    update = await client.put(triage_path, json={"decision": "accepted_risk", "reason": "reviewed"},
                              headers={"If-Match": initial.headers["etag"]})
    assert update.status_code == 200, update.text
    assert update.headers["etag"] == '"1"' and update.json()["actor_id"] == "owner-one"
    Draft202012Validator({**schema, "$ref": "#/$defs/Triage"}).validate(update.json())
    stale = await client.put(triage_path, json={"decision": "resolved", "reason": "stale edit"},
                             headers={"If-Match": initial.headers["etag"]})
    assert stale.status_code == 412
    history = await client.get(triage_path)
    assert history.headers["etag"] == '"1"' and history.json()["items"] == [update.json()]
    assert await compare(results["run-next"]["result_id"]) == matching
    assert (await client.get(f'{path}/results/{results["run-base"]["result_id"]}')).headers["etag"] == '"1"'
    async with scenario["sessions"]() as independent:
        rows = (await independent.scalars(select(Triage))).all()
        native = await independent.get(Finding, ("tenant-one", baseline["shared"]["finding_id"]))
        assert len(rows) == 1 and rows[0].revision == 1
        assert native.payload["validation"] == "supported" and native.triage_revision == 1
        assert (await independent.get(ResultDetail, ("tenant-one", results["run-base"]["result_id"]))
                ).payload["revision"] == 1


async def test_stale_triage_preserves_history(results_scenario):
    scenario = results_scenario
    client = scenario["client"]
    await collect_result(scenario, "run-audit", capability="threat_model",
                         findings=[{"native_id": "native-audit", "kind": "candidate_threat",
                                    "claim": "candidate", "provider_severity": "low",
                                    "source_refs": [scenario["source"]]}])
    path = "/v1/products/product-one/projects/project-one/services/security/results"
    result_id = (await client.get(path)).json()["items"][0]["result_id"]
    finding_id = (await client.get(f"{path}/{result_id}/findings")).json()["items"][0]["finding_id"]
    triage_path = f"{path}/{result_id}/findings/{finding_id}/triage"
    assert (await client.put(triage_path, json={"decision": "open", "reason": "review"})).status_code == 428
    first = await client.put(triage_path, json={"decision": "open", "reason": "initial review"},
                             headers={"If-Match": '"0"'})
    assert first.status_code == 200 and first.headers["etag"] == '"1"'
    second = await client.put(triage_path, json={"decision": "false_positive", "reason": "checked source"},
                              headers={"If-Match": '"1"'})
    assert second.status_code == 200 and second.headers["etag"] == '"2"'
    stale = await client.put(triage_path, json={"decision": "resolved", "reason": "stale request"},
                             headers={"If-Match": '"1"'})
    assert stale.status_code == 412 and stale.json()["detail"]["code"] == "stale_revision"
    readback = await client.get(triage_path)
    assert readback.status_code == 200 and readback.headers["etag"] == '"2"'
    assert readback.json()["items"] == [first.json(), second.json()]
    async with scenario["sessions"]() as independent:
        history = (await independent.scalars(select(Triage).order_by(Triage.revision))).all()
        finding = await independent.get(Finding, ("tenant-one", finding_id))
        assert [entry.revision for entry in history] == [1, 2]
        assert [entry.decision["reason"] for entry in history] == ["initial review", "checked source"]
        assert finding.triage_revision == 2 and finding.payload["validation"] == "candidate"


async def test_missing_finding_readback_cannot_claim_complete(results_scenario):
    from l5_services.findings import normalize_result

    scenario = results_scenario
    await collect_result(scenario, "run-count", capability="code_review", findings=[],
                         files={"src/same.py": "a" * 64})
    async with scenario["sessions"]() as session:
        run = await session.get(Run, ("tenant-one", "run-count"))
        unverified, _ = normalize_result(run, "new-result", 1, {
            "native_job_ref": run.native_job_ref, "coverage": "complete",
        }, NOW)
        assert unverified.payload["coverage"] == "unknown"
        with pytest.raises(ValueError, match="finding count does not match"):
            normalize_result(run, "new-result", 1, {
                "native_job_ref": run.native_job_ref, "coverage": "complete", "finding_count": 3,
                "findings_artifact": {"artifact_id": "unread"},
            }, NOW)


async def test_result_visibility(results_scenario):
    scenario = results_scenario
    client = scenario["client"]
    source = scenario["source"]
    allowed_source = {**source, "record_id": "repository-allowed"}
    private = {"native_id": "private-id", "kind": "static_finding",
               "claim": "private finding text", "provider_severity": "high", "validation": "unverified",
               "source_refs": [source], "locations": ["private-source.py:5"]}
    public = {**private, "native_id": "public-id", "claim": "allowed finding text",
              "source_refs": [allowed_source], "locations": ["allowed.py:5"]}
    await collect_result(scenario, "run-private", capability="code_review", findings=[private])
    await collect_result(scenario, "run-visible", capability="code_review", findings=[public],
                         source=allowed_source)
    await collect_result(scenario, "run-other-product", capability="code_review", findings=[private],
                         product_id="product-other", owner_id="owner-other")
    other_tenant_source = {**source, "tenant_id": "tenant-two"}
    await collect_result(scenario, "run-other-tenant", capability="code_review",
                         findings=[{**private, "source_refs": [other_tenant_source]}],
                         tenant_id="tenant-two", product_id="product-two", owner_id="owner-two",
                         source=other_tenant_source)

    publication_attempts = []

    def unavailable_publication(request):
        publication_attempts.append(request.url.path)
        raise httpx.ConnectError("synthetic Context outage", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable_publication),
                                 base_url="http://context.example.test") as context:
        with pytest.raises(httpx.ConnectError, match="synthetic Context outage"):
            await context.post("/contributions", json={"run_id": "run-private"})
    assert publication_attempts == ["/contributions"]
    async with scenario["sessions"]() as independent:
        assert len((await independent.scalars(select(AssessmentResult))).all()) == 4
        assert len((await independent.scalars(select(ResultDetail))).all()) == 4
    path = "/v1/products/product-one/projects/project-one/services/security"
    listed = (await client.get(path + "/results")).json()["items"]
    results = {item["run_id"]: item for item in listed}
    assert set(results) == {"run-private", "run-visible"}
    assert all("publication_id" not in item for item in listed)
    private_result = results["run-private"]
    private_id = private_result["result_id"]
    private_ref = private_result["findings_artifact"]
    metadata = await client.get(f'{path}/artifacts/{private_ref["artifact_id"]}')
    assert metadata.status_code == 200 and metadata.headers["cache-control"] == "no-store"
    schema = json.loads((Path(__file__).resolve().parents[4] /
                         "docs/l5-services/contracts/v1/schemas.json").read_text())
    Draft202012Validator({**schema, "$ref": "#/$defs/ArtifactAccess"}).validate(metadata.json())
    download_path = metadata.json()["download_path"]
    download = await client.get(download_path)
    assert download.status_code == 200 and download.headers["content-disposition"] == "attachment"
    assert download.headers["x-content-type-options"] == "nosniff"
    assert sha256(download.content).hexdigest() == private_ref["sha256"]
    assert len(download.content) == private_ref["bytes"]
    assert json.loads(download.content)["items"][0]["claim"] == "private finding text"
    finding_id = json.loads(download.content)["items"][0]["finding_id"]

    scenario["current"]["permitted_sources"] = {
        (allowed_source["source_connection_id"], allowed_source["record_id"])}
    responses = [
        await client.get(path + "/results", params={"limit": 1}),
        await client.get(path + "/results", params={"limit": 1, "cursor": private_id}),
        await client.get(f"{path}/results/{private_id}"),
        await client.get(f"{path}/results/{private_id}/findings"),
        await client.get(f"{path}/results/{private_id}/findings/{finding_id}/triage"),
        await client.put(f"{path}/results/{private_id}/findings/{finding_id}/triage",
                         json={"decision": "open", "reason": "should not apply"}, headers={"If-Match": '"0"'}),
        await client.post(path + "/comparisons", json={"baseline_result_id": private_id,
                                                       "candidate_result_id": results["run-visible"]["result_id"]}),
        await client.get(f'{path}/artifacts/{private_ref["artifact_id"]}'),
        await client.get(download_path),
    ]
    assert {item["run_id"] for item in responses[0].json()["items"]} == {"run-visible"}
    assert "next_cursor" not in responses[0].json()
    assert all(response.status_code in {200, 404} for response in responses)
    assert all(response.status_code == 404 for response in responses[2:])
    assert all("private finding text" not in response.text and "private-source.py" not in response.text
               and source["record_id"] not in response.text for response in responses)
    visible_ref = results["run-visible"]["findings_artifact"]
    allowed_download = await client.get(f'{path}/artifacts/{visible_ref["artifact_id"]}/content')
    assert allowed_download.status_code == 200
    assert json.loads(allowed_download.content)["items"][0]["claim"] == "allowed finding text"
    assert (await client.get(f'{path}/artifacts/raw-provider-evidence')).status_code == 404

    scenario["current"]["actor_id"] = "different-owner"
    assert (await client.get(path + "/results")).json() == {"items": []}
    scenario["current"]["actor_id"] = "owner-one"
    scenario["current"]["relationship_revision"] = "2"
    assert (await client.get(path + "/results")).json() == {"items": []}
    scenario["current"]["relationship_revision"] = "1"
    assert (await client.get(path.replace("product-one", "product-other") + "/results")).status_code == 404
    async with scenario["sessions"]() as independent:
        details = (await independent.scalars(select(ResultDetail))).all()
        assert len(details) == 4
        other_tenant = next(row for row in details if row.tenant_id == "tenant-two")
        assert len((await independent.scalars(select(Finding))).all()) == 4
        assert (await independent.scalars(select(Triage))).all() == []
    assert (await client.get(f"{path}/results/{other_tenant.result_id}")).status_code == 404


async def test_denied_artifact_and_pagination(results_scenario):
    scenario = results_scenario
    client = scenario["client"]
    source = scenario["source"]
    allowed_source = {**source, "record_id": "allowed-repository"}
    private_finding = {"native_id": "private", "kind": "candidate_threat",
                       "claim": "private bytes", "provider_severity": "high", "source_refs": [source]}
    allowed_finding = {**private_finding, "native_id": "allowed", "claim": "allowed bytes",
                       "source_refs": [allowed_source]}
    await collect_result(scenario, "run-private", capability="threat_model", findings=[private_finding])
    await collect_result(scenario, "run-allowed", capability="threat_model", findings=[allowed_finding],
                         source=allowed_source)
    path = "/v1/products/product-one/projects/project-one/services/security"
    results = {item["run_id"]: item for item in (await client.get(path + "/results")).json()["items"]}
    reference = results["run-private"]["findings_artifact"]
    access = await client.get(f'{path}/artifacts/{reference["artifact_id"]}')
    assert access.status_code == 200
    scenario["current"]["permitted_sources"] = {
        (allowed_source["source_connection_id"], allowed_source["record_id"])}
    for cursor in (None, results["run-private"]["result_id"]):
        page = await client.get(path + "/results", params={"limit": 1, **({"cursor": cursor} if cursor else {})})
        assert page.status_code == 200
        assert {item["run_id"] for item in page.json()["items"]} <= {"run-allowed"}
        assert "next_cursor" not in page.json()
    for uri in (f'{path}/artifacts/{reference["artifact_id"]}', access.json()["download_path"]):
        denied = await client.get(uri)
        assert denied.status_code == 404 and "private bytes" not in denied.text
        assert "x-adp-content-sha256" not in denied.headers
    scenario["current"]["permitted_sources"] = None
    async with scenario["sessions"]() as independent:
        finding = (await independent.scalars(select(Finding).where(Finding.native_id == "private"))).one()
        finding.payload = {**finding.payload, "claim": "tampered private bytes"}
        await independent.commit()
    corrupted = await client.get(access.json()["download_path"])
    assert corrupted.status_code == 503 and "tampered private bytes" not in corrupted.text


async def test_tool_revoked_artifact_access(results_scenario, monkeypatch, request):
    from src.internal import tools_l5

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
    request.addfinalizer(tools_l5.l5_operations.cache_clear)
    scenario = results_scenario
    source = scenario["source"]
    finding = {"native_id": "private-findings", "kind": "candidate_threat", "claim": "private evidence",
               "provider_severity": "high", "source_refs": [source]}
    await collect_result(scenario, "run-mcp", capability="threat_model", findings=[finding])
    path = "/v1/products/product-one/projects/project-one/services/security"
    readback = await scenario["client"].get(path + "/results")
    assert readback.status_code == 200
    artifact_id = readback.json()["items"][0]["findings_artifact"]["artifact_id"]
    arguments = {"product_id": "product-one", "project_id": "project-one", "service_id": "security",
                 "artifact_id": artifact_id}
    async with scenario["sessions"]() as session:
        backend = scenario["backend_factory"](session)
        metadata = await tools_l5.dispatch_l5("l5___get_artifact_access", arguments, backend)
        assert metadata["structuredContent"]["data"]["artifact"]["artifact_id"] == artifact_id
        assert json.loads(metadata["content"][0]["text"]) == metadata["structuredContent"]
        scenario["current"]["permitted_sources"] = set()
        for tool in ("l5___get_artifact_access", "l5___download_artifact"):
            denied = await tools_l5.dispatch_l5(tool, arguments, backend)
            assert denied["isError"] is True
            assert denied["structuredContent"]["http_status"] == 404
            assert denied["structuredContent"]["error"]["code"] == "not_found"
            schema = json.loads((Path(__file__).resolve().parents[4] /
                                 "docs/l5-services/contracts/v1/schemas.json").read_text())
            Draft202012Validator({**schema, "$ref": "#/$defs/McpError"}).validate(denied["structuredContent"])
            assert json.loads(denied["content"][0]["text"]) == denied["structuredContent"]
            assert "private evidence" not in str(denied)
        direct = await scenario["client"].get(path + "/artifacts/" + artifact_id)
        assert direct.status_code == 404
    async with scenario["sessions"]() as independent:
        stored = (await independent.scalars(select(ResultDetail))).one()
        assert stored.payload["findings_artifact"]["artifact_id"] == artifact_id


@pytest.mark.asyncio
async def test_tool_empty_triage_head_and_edit(results_scenario, monkeypatch, request):
    from src.internal import tools_l5

    monkeypatch.setattr(tools_l5, "files", lambda package: Path(__file__).resolve().parents[4] / "docs/l5-services")
    tools_l5.l5_tool_catalogue.cache_clear()
    tools_l5.l5_operations.cache_clear()
    request.addfinalizer(tools_l5.l5_tool_catalogue.cache_clear)
    request.addfinalizer(tools_l5.l5_operations.cache_clear)
    scenario = results_scenario
    await collect_result(scenario, "run-triage-mcp", capability="threat_model", findings=[
        {"native_id": "native-triage", "kind": "candidate_threat", "claim": "candidate",
         "provider_severity": "low", "source_refs": [scenario["source"]]},
    ])
    path = "/v1/products/product-one/projects/project-one/services/security/results"
    result_id = (await scenario["client"].get(path)).json()["items"][0]["result_id"]
    finding_id = (await scenario["client"].get(f"{path}/{result_id}/findings")).json()["items"][0]["finding_id"]
    triage_path = f"{path}/{result_id}/findings/{finding_id}/triage"
    scope = {"product_id": "product-one", "project_id": "project-one", "service_id": "security",
             "result_id": result_id, "finding_id": finding_id}
    operations = {item["name"]: item["outputSchema"] for item in tools_l5.available_l5_catalogue()["tools"]}

    async with scenario["sessions"]() as session:
        backend = scenario["backend_factory"](session)

        async def invoke(tool, arguments):
            result = await tools_l5.dispatch_l5(tool, arguments, backend)
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
            if not result.get("isError"):
                Draft202012Validator(operations[tool.replace("___", ".", 1)]).validate(result["structuredContent"])
            return result["structuredContent"]

        head = await invoke("l5___list_triage", scope)
        assert head["etag"] == '"0"' and head["data"]["items"] == []
        assert head["etag"] == (await scenario["client"].get(triage_path)).headers["etag"]
        first = await invoke("l5___set_triage", {**scope, "input": {"decision": "open", "reason": "first review"},
                                                   "idempotency_key": "mcp_triage_first_00001", "if_match": head["etag"]})
        assert first["etag"] == '"1"'
        stale = await invoke("l5___set_triage", {**scope, "input": {"decision": "resolved", "reason": "stale"},
                                                   "idempotency_key": "mcp_triage_stale_00001", "if_match": head["etag"]})
        assert stale["http_status"] == 412 and stale["error"]["code"] == "stale_revision"
        readback = await scenario["client"].get(triage_path)
        assert readback.headers["etag"] == first["etag"]
        assert readback.json()["items"] == [first["data"]]
    async with scenario["sessions"]() as independent:
        history = (await independent.scalars(select(Triage).where(Triage.tenant_id == "tenant-one"))).all()
        assert [(entry.finding_id, entry.revision) for entry in history] == [(finding_id, 1)]
