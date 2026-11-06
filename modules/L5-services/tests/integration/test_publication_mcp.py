"""Shared publication API keeps result ownership and retries separate from runs."""

from datetime import timedelta

import pytest
from l5_services.models import (
    AssessmentResult,
    Configuration,
    ConfigurationVersion,
    Publication,
    PublicationRetryReceipt,
    ResultDetail,
    Run,
    Work,
)
from sqlalchemy import select
from test_results import NOW, collect_result, results_scenario
from tests.migrations.conftest_postgres import pg_server as pg_server
from tests.migrations.conftest_postgres import pg_url as pg_url


@pytest.mark.usefixtures(results_scenario.__name__)
@pytest.mark.parametrize("results_scenario", ("orm", "gateway_sqlite", "gateway_postgres"), indirect=True)
async def test_publication_read_and_retry_are_scoped_without_new_run(request):
    scenario = request.getfixturevalue(results_scenario.__name__)
    await collect_result(
        scenario,
        "run-one",
        capability="threat_model",
        findings=[],
        manifest_ref={"artifact_id": "manifest-one", "version": "1", "sha256": "a" * 64, "media_type": "application/json", "bytes": 4},
    )
    async with scenario["sessions"]() as session:
        publication = (await session.scalars(select(Publication))).one()
        publication.state = "retry_wait"
        publication.next_due_at = NOW + timedelta(hours=1)
        await session.commit()
        publication_id = publication.publication_id
        result_id = publication.result_id
    await collect_result(
        scenario,
        "run-two",
        capability="threat_model",
        findings=[],
        manifest_ref={"artifact_id": "manifest-two", "version": "1", "sha256": "b" * 64, "media_type": "application/json", "bytes": 4},
    )
    async with scenario["sessions"]() as session:
        other = (await session.scalars(select(Publication).where(Publication.publication_id != publication_id))).one()
        other.state = "retry_wait"
        await session.commit()
        other_id = other.publication_id
    prefix = "/v1/products/product-one/projects/project-one/services/security"
    async with scenario["sessions"]() as session:
        runs_before = (await session.scalars(select(Run))).all()
        work_before = (await session.scalars(select(Work))).all()
        assert len((await session.scalars(select(AssessmentResult))).all()) == 2
    response = await scenario["client"].get(f"{prefix}/publications/{publication_id}")
    assert response.status_code == 200
    assert response.json()["result_id"] == result_id
    assert response.json()["state"] == "retry_wait"
    assert response.headers["etag"] == '"1"'
    retry = await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry",
        json={"reason": "Context delivery recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )
    assert retry.status_code == 202
    assert retry.json()["state"] == "pending"
    replay = await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry",
        json={"reason": "Context delivery recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )
    assert replay.status_code == 202 and replay.json() == retry.json()
    changed_body = await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry", json={"reason": "different recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )
    assert changed_body.status_code == 409
    assert changed_body.json()["detail"]["code"] == "idempotency_conflict"
    changed_target = await scenario["client"].post(
        f"{prefix}/publications/{other_id}/retry", json={"reason": "Context delivery recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )
    assert changed_target.status_code == 409
    assert changed_target.json()["detail"]["code"] == "idempotency_conflict"
    scenario["current"]["sources_allowed"] = False
    assert (await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry", json={"reason": "Context delivery recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )).status_code == 404
    scenario["current"]["sources_allowed"] = True
    assert (await scenario["client"].get(f"{prefix}/publications/{publication_id}")).headers["etag"] == '"2"'
    async with scenario["sessions"]() as session:
        publication = await session.get(Publication, ("tenant-one", publication_id))
        other = await session.get(Publication, ("tenant-one", other_id))
        assert publication.state == "pending" and publication.revision == 2
        assert publication.next_due_at.replace(tzinfo=NOW.tzinfo) == NOW
        assert other.state == "retry_wait"
        assert len((await session.scalars(select(Run))).all()) == len(runs_before)
        assert len((await session.scalars(select(Work))).all()) == len(work_before)
        assert len((await session.scalars(select(PublicationRetryReceipt))).all()) == 1
        run = await session.get(Run, ("tenant-one", "run-one"))
        run.admission = {**run.admission, "scope": {**run.admission["scope"], "visibility": "product_team"}}
        await session.commit()
    scenario["current"]["actor_id"] = "owner-other"
    other_actor_retry = await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry", json={"reason": "other actor recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )
    assert other_actor_retry.status_code == 202
    assert other_actor_retry.json()["state"] == "pending"
    assert (await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry", json={"reason": "other actor recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )).json() == other_actor_retry.json()
    scenario["current"]["actor_id"] = "owner-one"
    async with scenario["sessions"]() as session:
        receipts = (await session.scalars(select(PublicationRetryReceipt))).all()
        assert len(receipts) == 2
        assert {item.actor_id for item in receipts} == {"owner-one", "owner-other"}
        assert len({item.request_hash for item in receipts}) == 2
        assert {item.publication_id for item in receipts} == {publication_id}
        assert len((await session.scalars(select(Run))).all()) == len(runs_before)
        assert len((await session.scalars(select(Work))).all()) == len(work_before)
        publication = await session.get(Publication, ("tenant-one", publication_id))
        publication.state = "unavailable"
        publication.revision += 1
        await session.commit()
    retained = await scenario["client"].post(
        f"{prefix}/publications/{publication_id}/retry", json={"reason": "Context delivery recovery"},
        headers={"Idempotency-Key": "retry-context-key-0001"},
    )
    assert retained.status_code == 202 and retained.json() == retry.json()
    assert (await scenario["client"].get(f"{prefix}/publications/{publication_id}")).json()["state"] == "unavailable"
    async with scenario["sessions"]() as session:
        assert len((await session.scalars(select(PublicationRetryReceipt))).all()) == 2
        assert len((await session.scalars(select(Run))).all()) == len(runs_before)
        assert len((await session.scalars(select(Work))).all()) == len(work_before)
    foreign = "/v1/products/product-other/projects/project-one/services/security"
    assert (await scenario["client"].get(f"{foreign}/publications/{publication_id}")).status_code == 404
    assert (
        await scenario["client"].post(
            f"{foreign}/publications/{publication_id}/retry", json={"reason": "wrong scope"}, headers={"Idempotency-Key": "retry-context-key-0002"}
        )
    ).status_code == 404


async def test_finops_publication_preserves_scoped_readback_and_retry_preconditions(results_scenario):
    scenario = results_scenario
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one",
             "relationship_revision": "1", "owner_id": "owner-one", "visibility": "only_me"}
    async with scenario["sessions"]() as session:
        session.add_all([
            Configuration(tenant_id="tenant-one", configuration_id="config-finops", product_id="product-one",
                          project_id="project-one", service_id="finops", owner_id="owner-one",
                          relationship_revision="1", current_revision=1, handoff_state="linked"),
            ConfigurationVersion(tenant_id="tenant-one", configuration_id="config-finops", revision=1,
                                 settings={"service_id": "finops"}, inherited_fields=[], gaps=[], state="ready", hash="a" * 64),
            Run(tenant_id="tenant-one", run_id="finops-run", product_id="product-one", project_id="project-one",
                service_id="finops", configuration_id="config-finops", configuration_revision=1,
                binding_id="finops-binding", trigger_id="manual", binding_generation=1, revision=1,
                input={"service_id": "finops", "sources": []}, admission={"scope": scope}, scope=scope,
                state="partial", provider_state="outcome_unknown", deadline_at=NOW + timedelta(hours=1)),
            AssessmentResult(tenant_id="tenant-one", result_id="finops-result", run_id="finops-run",
                             version=1, payload={}),
            ResultDetail(tenant_id="tenant-one", result_id="finops-result",
                         payload={"result_id": "finops-result", "revision": 1, "scope": scope,
                                  "native": {"status": "outcome_unknown", "measure": {"amount": None}},
                                  "oracle": {"measure": {"amount": "-12.3400"}}}),
            Publication(tenant_id="tenant-one", publication_id="finops-publication", result_id="finops-result",
                        result_version=1, destination="context", schema_version="1.0", contribution_hash="b" * 64,
                        payload={}, state="retry_wait", next_due_at=NOW + timedelta(hours=1)),
        ])
        await session.commit()
    scenario["current"]["service_id"] = "finops"
    prefix = "/v1/products/product-one/projects/project-one/services/finops"
    client = scenario["client"]
    original = await client.get(f"{prefix}/results/finops-result")
    assert original.status_code == 200 and original.json()["native"]["measure"]["amount"] is None
    publication = await client.get(f"{prefix}/publications/finops-publication")
    assert publication.status_code == 200 and publication.json()["state"] == "retry_wait"
    assert publication.headers["etag"] == '"1"'
    assert (await client.post(f"{prefix}/publications/finops-publication/retry",
                              json={"reason": "Resume publication"})).status_code == 428
    assert (await client.post(f"{prefix}/publications/finops-publication/retry",
                              json={"reason": ""}, headers={"Idempotency-Key": "finops-retry-key-0001"})).status_code == 422
    recovered = await client.post(f"{prefix}/publications/finops-publication/retry",
                                  json={"reason": "Resume publication"},
                                  headers={"Idempotency-Key": "finops-retry-key-0001"})
    assert recovered.status_code == 202 and recovered.json()["state"] == "pending"
    replay = await client.post(f"{prefix}/publications/finops-publication/retry",
                               json={"reason": "Resume publication"},
                               headers={"Idempotency-Key": "finops-retry-key-0001"})
    assert replay.status_code == 202 and replay.json() == recovered.json()
    conflict = await client.post(f"{prefix}/publications/finops-publication/retry",
                                 json={"reason": "Different request"},
                                 headers={"Idempotency-Key": "finops-retry-key-0001"})
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    assert (await client.get(f"{prefix}/publications/finops-publication")).headers["etag"] == '"2"'
    assert (await client.get(f"{prefix}/artifacts/answer-example")).status_code == 404
    for changed in ({"product_id": "product-other"}, {"tenant_id": "tenant-two", "product_id": "product-two"},
                    {"actor_id": "owner-other"}, {"relationship_revision": "2"}, {"sources_allowed": False}):
        previous = scenario["current"].copy()
        scenario["current"].update(changed)
        assert (await client.get(f"{prefix}/publications/finops-publication")).status_code == 404
        assert (await client.post(f"{prefix}/publications/finops-publication/retry",
                                  json={"reason": "Resume publication"},
                                  headers={"Idempotency-Key": "finops-retry-key-0001"})).status_code == 404
        scenario["current"].clear()
        scenario["current"].update(previous)
    assert (await client.get(f"{prefix}/results/finops-result")).json() == original.json()
    async with scenario["sessions"]() as session:
        assert len((await session.scalars(select(PublicationRetryReceipt))).all()) == 1
        assert len((await session.scalars(select(Run))).all()) == 1
        assert not (await session.scalars(select(Work))).all()


async def test_finops_ids_cursors_keys_and_artifacts_are_isolated(results_scenario):
    scenario = results_scenario
    client = scenario["client"]
    scopes = (
        ("tenant-one", "product-one", "project-one", "owner-one", "a"),
        ("tenant-one", "product-one", "project-other", "owner-one", "b"),
        ("tenant-two", "product-two", "project-one", "owner-two", "c"),
    )
    async with scenario["sessions"]() as session:
        for tenant_id, product_id, project_id, owner_id, suffix in scopes:
            scope = {"tenant_id": tenant_id, "product_id": product_id, "project_id": project_id,
                     "relationship_revision": "1", "owner_id": owner_id, "visibility": "only_me"}
            config_id = f"finops-config-{suffix}"
            session.add(Configuration(tenant_id=tenant_id, configuration_id=config_id,
                                      product_id=product_id, project_id=project_id, service_id="finops",
                                      owner_id=owner_id, relationship_revision="1", current_revision=1,
                                      handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=tenant_id, configuration_id=config_id, revision=1,
                                             settings={"service_id": "finops"}, inherited_fields=[], gaps=[],
                                             state="ready", hash="a" * 64))
            for index in (1, 2):
                run_id = f"finops-run-{suffix}-{index}"
                result_id = f"finops-result-{suffix}-{index}"
                session.add(Run(tenant_id=tenant_id, run_id=run_id, product_id=product_id,
                                project_id=project_id, service_id="finops", configuration_id=config_id,
                                configuration_revision=1, binding_id=f"finops-binding-{suffix}",
                                trigger_id="manual", binding_generation=1, revision=1,
                                input={"service_id": "finops", "sources": []}, admission={"scope": scope},
                                scope=scope, state="partial", provider_state="outcome_unknown",
                                deadline_at=NOW + timedelta(hours=1)))
                session.add(AssessmentResult(tenant_id=tenant_id, result_id=result_id, run_id=run_id,
                                             version=1, payload={}))
                session.add(ResultDetail(tenant_id=tenant_id, result_id=result_id,
                                         payload={"result_id": result_id, "revision": 1, "scope": scope,
                                                  "native": {"status": "outcome_unknown", "answer_artifact": {
                                                      "artifact_id": f"finops-answer-{suffix}-{index}"}}}))
            session.add(Publication(tenant_id=tenant_id, publication_id=f"finops-publication-{suffix}",
                                    result_id=f"finops-result-{suffix}-1", result_version=1,
                                    destination="context", schema_version="1.0", contribution_hash="b" * 64,
                                    payload={}, state="retry_wait", next_due_at=NOW + timedelta(hours=1)))
        await session.commit()

    def path(product_id, project_id):
        return f"/v1/products/{product_id}/projects/{project_id}/services/finops"

    original = path("product-one", "project-one")
    scenario["current"].update(service_id="finops", project_id="project-one")
    run_page = await client.get(f"{original}/runs", params={"limit": 1})
    result_page = await client.get(f"{original}/results", params={"limit": 1})
    assert run_page.status_code == result_page.status_code == 200
    run_cursor = run_page.json()["next_cursor"]
    result_cursor = result_page.json()["next_cursor"]
    assert run_cursor == "finops-run-a-1" and result_cursor == "finops-result-a-1"
    assert [item["run_id"] for item in (await client.get(f"{original}/runs", params={"cursor": run_cursor})).json()["items"]] == ["finops-run-a-2"]
    assert [item["result_id"] for item in (await client.get(f"{original}/results", params={"cursor": result_cursor})).json()["items"]] == ["finops-result-a-2"]
    key = "finops-shared-retry-key-0001"
    retry = {"reason": "Recover publication"}
    original_retry = await client.post(f"{original}/publications/finops-publication-a/retry",
                                       json=retry, headers={"Idempotency-Key": key})
    assert original_retry.status_code == 202

    for tenant_id, product_id, project_id, owner_id, suffix in scopes[1:]:
        scenario["current"].update(tenant_id=tenant_id, product_id=product_id,
                                   project_id=project_id, actor_id=owner_id)
        own = path(product_id, project_id)
        for route in (f"runs/{run_cursor}", f"results/{result_cursor}",
                      "publications/finops-publication-a", "artifacts/finops-answer-a-1",
                      "artifacts/finops-answer-a-1/content"):
            denied = await client.get(f"{own}/{route}")
            assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
            assert "finops-answer-a-1" not in denied.text
        for resource, cursor in (("runs", run_cursor), ("results", result_cursor)):
            denied = await client.get(f"{own}/{resource}", params={"cursor": cursor})
            assert denied.status_code == 404 and denied.json()["detail"]["code"] == "not_found"
            own_page = await client.get(f"{own}/{resource}", params={"limit": 1})
            assert own_page.status_code == 200
            assert own_page.json()["next_cursor"].endswith(f"-{suffix}-1")
        replay = await client.post(f"{own}/publications/finops-publication-a/retry",
                                   json=retry, headers={"Idempotency-Key": key})
        assert replay.status_code == 404 and replay.json()["detail"]["code"] == "not_found"
        own_retry = await client.post(f"{own}/publications/finops-publication-{suffix}/retry",
                                      json=retry, headers={"Idempotency-Key": key})
        assert own_retry.status_code == 202
        assert own_retry.json()["publication_id"] == f"finops-publication-{suffix}"

    scenario["current"].update(tenant_id="tenant-one", product_id="product-one",
                               project_id="project-one", actor_id="owner-one")
    assert (await client.post(f"{original}/publications/finops-publication-a/retry",
                              json=retry, headers={"Idempotency-Key": key})).json() == original_retry.json()
    assert (await client.get(f"{original}/results/finops-result-a-1")).status_code == 200
    async with scenario["sessions"]() as session:
        assert len((await session.scalars(select(PublicationRetryReceipt))).all()) == 3
