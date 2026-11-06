"""Independent fixed-value checks for native/billing comparison."""

from copy import deepcopy

import pytest

from l5_services.finops_reconciliation import compare_cost_series


QUERY = {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost",
         "currency": "USD", "granularity": "MONTHLY", "group_by": ["SERVICE"]}


def series(group, amount):
    return {"period": {"start": QUERY["start"], "end": QUERY["end"]},
            "group": [group], "amount": amount, "metric": QUERY["metric"],
            "currency": QUERY["currency"], "granularity": QUERY["granularity"],
            "group_by": QUERY["group_by"]}


def sources():
    native = {"amount": "10.00000001", "currency": "USD", "coverage": "complete", "estimated": False,
              "scope_digest": "a" * 64, "query_hash": "b" * 64,
              "series": [series("usage", "10.125"), series("credit", "-0.125"),
                         series("tiny", "0.00000001")]}
    return native, deepcopy(native)


def test_decimal_reconciliation_unit():
    native, billing = sources()
    comparison = compare_cost_series(QUERY, native, billing)
    assert comparison == {"state": "exact", "delta": "0.00000000",
                          "display_quantum": None, "reason": None}
    billing["series"][-1]["amount"] = "0.00000000"
    billing["amount"] = "10.00000000"
    assert compare_cost_series(QUERY, native, billing)["state"] == "mismatch"
    assert compare_cost_series(QUERY, native, billing, display_quantum="0.01")["state"] == "rounded"
    billing["series"][-1]["group"] = ["different"]
    assert compare_cost_series(QUERY, native, billing)["state"] == "incomparable"


def test_comparison_rejects_lossy_or_inconsistent_evidence():
    native, billing = sources()
    billing["series"][1]["amount"] = "0.125"
    with pytest.raises(ValueError, match="total differs"):
        compare_cost_series(QUERY, native, billing)
    billing = deepcopy(native)
    billing["series"][0]["currency"] = "EUR"
    with pytest.raises(ValueError, match="dimensions"):
        compare_cost_series(QUERY, native, billing)
    billing = deepcopy(native)
    billing["series"][0]["amount"] = 10.125
    with pytest.raises(ValueError, match="decimal string"):
        compare_cost_series(QUERY, native, billing)
    native, billing = sources()
    billing["currency"] = "EUR"
    assert compare_cost_series(QUERY, native, billing)["state"] == "incomparable"
    native, billing = sources()
    for source in (native, billing):
        for item in source["series"]:
            item["period"]["end"] = "2026-01-15"
    assert compare_cost_series(QUERY, native, billing)["state"] == "incomparable"


def test_cost_result_retains_native_identity_and_billing_provenance():
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from jsonschema import Draft202012Validator, FormatChecker

    from l5_services.finops_reconciliation import normalize_cost_result

    artifact = {"artifact_id": "evidence-example", "version": "1", "sha256": "a" * 64}
    answer = {"kind": "finops_native_answer", "native_job_ref": {"turn_id": "turn-example", "turn_locator": "ordinal:1"},
              "status": "answered", "observed_at": "2026-02-02T00:00:00Z", "answer_artifact": artifact}
    run = SimpleNamespace(run_id="run-example", native_job_ref=answer["native_job_ref"],
                          provider_readback_ref=answer, binding_generation=1, input={"query": QUERY},
                          admission={"billing_scope": {"scope_digest": "a" * 64,
                              "account_ids": ["123456789012"], "view_ref": "view-example",
                              "snapshot_artifact": artifact}})
    native, billing = sources()
    native.update(answer_artifact=artifact, status="answered", observed_at=answer["observed_at"],
                  query=QUERY, currency="USD", source_refreshed_at=None, freshness_reason=None)
    billing.update(query=QUERY, collection_id="collection-example", pages=[artifact],
                   observed_at="2026-02-03T00:00:00Z", currency="USD",
                   source_refreshed_at=None, freshness_reason=None)
    result = normalize_cost_result(run, native, billing)
    assert result["native"]["answer_artifact"] == answer["answer_artifact"]
    assert result["oracle"]["pages"] == [artifact]
    assert result["comparison"]["state"] == "exact"
    assert result["native"]["measure"]["amount"] == "10.00000001"
    schema = json.loads((Path(__file__).parents[4] / "docs/l5-services/finops/contract.json").read_text())
    Draft202012Validator({**schema, "$ref": "#/$defs/FinopsCostResult"},
                         format_checker=FormatChecker()).validate(result)
    tools = json.loads((Path(__file__).parents[4] / "docs/l5-services/contracts/v1/mcp-tools.json").read_text())
    tool = next(tool for tool in tools if tool["x-http-operation"] == "getResult")
    payload = {**result, "result_id": "result-example", "revision": 1,
        "scope": {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example",
                  "relationship_revision": "1", "owner_id": "owner-example", "visibility": "only_me"}}
    assert payload["native"]["turn_locator"] == "ordinal:1"
    validator = Draft202012Validator(tool["outputSchema"], format_checker=FormatChecker())
    validator.validate({"data": payload, "http_status": 200})
    assert not validator.is_valid({"data": {**payload, "native": {**payload["native"], "turn_locator": 1}}, "http_status": 200})
    with pytest.raises(ValueError, match="original answer"):
        normalize_cost_result(run, {**native, "answer_artifact": {**artifact, "version": "2"}}, billing)
    with pytest.raises(ValueError, match="pinned scope"):
        normalize_cost_result(run, native, {**billing, "scope_digest": "b" * 64})


def test_high_precision_difference_never_disappears():
    native, billing = sources()
    native["series"] = [series("usage", "1.0000000000000000000000000000000000000001")]
    native["amount"] = native["series"][0]["amount"]
    billing["series"] = [series("usage", "1.0000000000000000000000000000000000000000")]
    billing["amount"] = billing["series"][0]["amount"]
    comparison = compare_cost_series(QUERY, native, billing)
    assert comparison["state"] == "mismatch"
    assert comparison["delta"] == "0.0000000000000000000000000000000000000001"


async def test_decimal_reconciliation(tmp_path):
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.api.router import create_router
    from l5_services.findings import ResultStore
    from l5_services.models import ResultDetail
    from l5_services.models.base import Base
    from l5_services.models.evidence import AssessmentResult
    from l5_services.models.run import Run
    from l5_services.models.work import Work
    from l5_services.workers.execution import ExecutionService

    now = datetime(2026, 2, 3, tzinfo=UTC)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cost.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    native_artifact = {"artifact_id": "native-example", "version": "1", "sha256": "a" * 64}
    from l5_services.adapters.finops.billing import normalize_pages

    raw_pages = raw_billing_pages()
    snapshot = {"artifact_id": "scope-example", "version": "3", "sha256": "c" * 64}
    job = {"turn_id": "turn-example"}
    observed_at = "2026-02-02T00:00:00Z"
    answer = {"kind": "finops_native_answer", "native_job_ref": job, "status": "answered",
              "observed_at": observed_at, "answer_artifact": native_artifact}
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example"}
    native, billing = sources()
    native.update(answer_artifact=native_artifact, status="answered", observed_at=observed_at,
                  query=QUERY, currency="USD", source_refreshed_at=None, freshness_reason=None)
    billing = normalize_pages(QUERY, "a" * 64, "b" * 64, "collection-example", raw_pages,
                              "2026-02-03T00:00:00Z")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(tenant_id=scope["tenant_id"], run_id="run-example", product_id=scope["product_id"],
                            project_id=scope["project_id"], service_id="finops", configuration_id="config-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=1, state="collecting", provider_state="completed",
                            input={"query": QUERY}, admission={"scope": scope, "limits": {"max_ce_requests": 2},
                                "monitoring_policy": {"poll_grace_seconds": 60},
                                "billing_scope": {"scope_digest": "a" * 64, "account_ids": ["123456789012"],
                                                  "view_ref": "view-example", "snapshot_artifact": snapshot}},
                            native_job_ref=job,
                            deadline_at=now + timedelta(hours=1)))
            session.add(Work(tenant_id=scope["tenant_id"], run_id="run-example", work_id="work-example",
                             kind="collect", generation=1, step=1, due_at=now))
            await session.commit()

        calls = []

        class BillingFixture(BillingPageFixture):
            async def collect(self, run, native_job_ref, claim, provider_session):
                calls.append((run.run_id, claim.kind))
                assert native_job_ref == job and provider_session is not None
                return AdapterOutcome("completed", result=answer)

            async def collect_billing_page(self, run, claim, provider_session, request, cursor, collection_id):
                calls.append((run.run_id, claim.kind))
                return await super().collect_billing_page(run, claim, provider_session,
                                                          request, cursor, collection_id)

        async def authorize(run, claim, operation):
            assert operation == "collect" and claim.kind in {"collect", "billing"}
            return object()

        worker = ExecutionService(sessions, None, None, None, authorize,
                                  clock=lambda: now, finops_adapter=BillingFixture(raw_pages, native))
        assert await worker.execute({"schema_version": "1.0", "work_id": "work-example", "generation": 1})
        assert calls == [("run-example", "collect")]
        async with sessions() as session:
            run = await session.get(Run, (scope["tenant_id"], "run-example"))
            assert run.state == "collecting" and run.provider_readback_ref == answer
            pending = (await session.scalars(select(Work).where(Work.kind == "billing"))).one()
        assert await worker.execute({"schema_version": "1.0", "work_id": pending.work_id, "generation": 1})
        async with sessions() as session:
            pending = (await session.scalars(select(Work).where(Work.kind == "billing",
                                                             Work.completed_at.is_(None)))).one()
        assert await worker.execute({"schema_version": "1.0", "work_id": pending.work_id, "generation": 1})
        assert calls == [("run-example", "collect"), ("run-example", "billing"), ("run-example", "billing")]
        async with sessions() as session:
            assert (await session.get(Run, (scope["tenant_id"], "run-example"))).state == "succeeded"
            evidence = (await session.scalars(select(AssessmentResult))).one()
            assert evidence.payload["billing_series"][1]["amount"] == "-0.125"
            assert evidence.payload["native_artifact"] != evidence.payload["billing_pages"][0]
            assert len((await session.scalars(select(ResultDetail))).all()) == 1

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != (scope["product_id"], scope["project_id"], "finops"):
                    raise HTTPException(404, detail={"code": "not_found"})
                run = await self.session.get(Run, (scope["tenant_id"], "run-example"))
                return ResultStore(self.session, [run])

        async def backend():
            async with sessions() as session:
                yield Backend(session)

        app = FastAPI()
        app.include_router(create_router(backend))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            path = "/v1/products/product-example/projects/project-example/services/finops/results"
            listed = await client.get(path)
            assert listed.status_code == 200 and len(listed.json()["items"]) == 1
            result_id = listed.json()["items"][0]["result_id"]
            read = await client.get(f"{path}/{result_id}")
            assert read.status_code == 200 and read.headers["etag"] == '"1"'
            assert read.json()["native"]["answer_artifact"] == native_artifact
            assert read.json()["oracle"]["pages"] == [page["artifact"] for page in raw_pages]
            assert read.json()["comparison"]["state"] == "exact"
            assert "private-cursor" not in read.text
            assert (await client.get(path.replace("project-example", "other-project"))).status_code == 404
            assert (await client.get(f"{path}/{result_id}/findings")).status_code == 404
    finally:
        await engine.dispose()


@pytest.mark.parametrize("fault", ["lost_ack", "expired_cursor", "request_limit", "foreign_page",
                                  "repeated_token", "native_unstructured"])
async def test_collection_recovery(fault, tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta
    from hashlib import sha256

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.billing_collection import BillingCursorExpired, active_collection
    from l5_services.models import CostCollection, ResultDetail
    from l5_services.models.base import Base
    from l5_services.models.run import Run
    from l5_services.models.work import Work
    from l5_services.workers import execution
    from l5_services.workers.execution import ExecutionService

    fixture, native, _ = cost_case("account")
    now = datetime(2026, 2, 3, tzinfo=UTC)
    clock = [now]
    fixture.admission.update(limits={"max_ce_requests": 1 if fault == "request_limit" else 4},
                             monitoring_policy={"poll_grace_seconds": 300})
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    pages = fixture.pages
    combined = deepcopy(pages[0])
    combined["response"]["ResultsByTime"][0]["Groups"].extend(
        pages[1]["response"]["ResultsByTime"][0]["Groups"])
    combined["response"].pop("NextPageToken")
    combined["artifact"] = {**combined["artifact"], "artifact_id": "replacement-page",
                            "sha256": sha256(json.dumps(combined["response"], sort_keys=True,
                                                      separators=(",", ":")).encode()).hexdigest()}

    class FaultFixture(BillingPageFixture):
        def __init__(self):
            super().__init__(pages, native)
            self.expired = False

        async def collect_billing_page(self, run, claim, provider_session, request, cursor, collection_id):
            if fault == "expired_cursor" and cursor is not None and not self.expired:
                self.expired = True
                self.requests.append((collection_id, cursor))
                raise BillingCursorExpired()
            if fault == "expired_cursor" and self.expired and cursor is None:
                self.requests.append((collection_id, cursor))
                page = {**combined, "collection_id": collection_id}
                self.saved[(collection_id, page["artifact"]["artifact_id"])] = page
                return page
            page = await super().collect_billing_page(run, claim, provider_session,
                                                      request, cursor, collection_id)
            if fault == "repeated_token" and cursor is not None:
                page = deepcopy(page)
                page["response"]["NextPageToken"] = cursor
                page["artifact"]["sha256"] = sha256(json.dumps(
                    page["response"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            return {**page, "collection_id": "foreign-collection"} if fault == "foreign_page" else page

        async def read_native_cost(self, run, claim, provider_session):
            if fault == "native_unstructured":
                return None
            return await super().read_native_cost(run, claim, provider_session)

        async def submit(self, *args, **kwargs):
            raise AssertionError("billing repair must not resubmit a native turn")

    adapter = FaultFixture()
    async def authorize(run, claim, operation):
        assert operation == "collect" and run.native_job_ref == fixture.native_job_ref
        return object()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(tenant_id="tenant-example", run_id="run-example", product_id="product-example",
                            project_id="project-example", service_id="finops", configuration_id="config-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=1, state="collecting", provider_state="completed",
                            input=fixture.input, admission=fixture.admission, native_job_ref=fixture.native_job_ref,
                            provider_readback_ref=fixture.provider_readback_ref,
                            deadline_at=now + timedelta(hours=1)))
            session.add(Work(tenant_id="tenant-example", run_id="run-example", work_id="billing-work",
                             kind="billing", generation=1, step=2, due_at=now))
            await session.commit()
        worker = ExecutionService(sessions, None, None, None, authorize,
                                  clock=lambda: clock[0], finops_adapter=adapter)
        envelope = {"schema_version": "1.0", "work_id": "billing-work", "generation": 1}
        if fault == "lost_ack":
            original_finish = execution.finish_work
            async def lost_ack(*args, **kwargs):
                return None
            monkeypatch.setattr(execution, "finish_work", lost_ack)
            assert await worker.execute(envelope) is False
            assert await worker.execute(envelope) is False
            async with sessions() as session:
                checkpoint = (await session.scalars(select(CostCollection))).one()
                assert checkpoint.request_count == checkpoint.page_count == 1
            monkeypatch.setattr(execution, "finish_work", original_finish)
            clock[0] += timedelta(seconds=61)
        if fault == "foreign_page":
            with pytest.raises(ValueError, match="another collection|reserved collection"):
                await worker.execute(envelope)
        else:
            assert await worker.execute(envelope)
        async with sessions() as session:
            first = (await session.scalars(select(CostCollection))).one()
            assert first.request_count == (2 if fault == "lost_ack" else 1)
            assert first.page_count == (0 if fault == "foreign_page" else
                                        2 if fault == "lost_ack" else 1)
            assert "private-cursor" not in str(first.checkpoint_ref)
            assert await session.get(Run, ("tenant-example", "run-example")) is not None
            run = await session.get(Run, ("tenant-example", "run-example"))
            assert run.native_job_ref == fixture.native_job_ref and run.provider_readback_ref == fixture.provider_readback_ref
            changed_scope = deepcopy(run.admission)
            changed_scope["billing_scope"]["view_ref"] = "different-view"
            run.admission = changed_scope
            with pytest.raises(ValueError, match="pinned request"):
                await active_collection(session, run, clock[0])
            await session.rollback()
        if fault in {"expired_cursor", "repeated_token", "native_unstructured"}:
            for _ in range(1 if fault == "native_unstructured" else 2):
                async with sessions() as session:
                    next_work = (await session.scalars(select(Work).where(
                        Work.kind == "billing", Work.completed_at.is_(None)))).one()
                if fault == "repeated_token":
                    with pytest.raises(ValueError, match="repeats a cursor"):
                        await worker.execute({**envelope, "work_id": next_work.work_id})
                    break
                assert await worker.execute({**envelope, "work_id": next_work.work_id})
        async with sessions() as session:
            run = await session.get(Run, ("tenant-example", "run-example"))
            collections = (await session.scalars(select(CostCollection))).all()
            assert sum(item.request_count for item in collections) == len(adapter.requests)
            assert sum(item.request_count for item in collections) <= fixture.admission["limits"]["max_ce_requests"]
            assert "private-cursor" not in str([item.checkpoint_ref for item in collections])
            assert run.native_job_ref == fixture.native_job_ref
            if fault == "expired_cursor":
                assert len(collections) == 2 and collections[0].status == "superseded"
                assert collections[1].supersedes_id == collections[0].collection_id
                assert collections[1].checkpoint_ref["pages"] == [combined["artifact"]]
            if fault in {"expired_cursor", "lost_ack"}:
                assert run.state == "succeeded"
                assert (await session.scalars(select(ResultDetail))).one().payload["comparison"]["state"] == "exact"
            if fault == "native_unstructured":
                result = (await session.scalars(select(ResultDetail))).one().payload
                assert run.state == "partial"
                assert result["native"]["status"] == "answered"
                assert result["native"]["measure"]["coverage"] == "unavailable"
                assert result["native"]["measure"]["amount"] is None
                assert result["native"]["answer_artifact"] == fixture.provider_readback_ref["answer_artifact"]
                assert result["oracle"]["measure"]["amount"] == "10.00000001"
                assert len(result["oracle"]["pages"]) == 2
                assert result["comparison"]["state"] == "unavailable"
            if fault == "request_limit":
                assert run.state == "partial" and collections[0].reason == "request_limit"
                assert (await session.scalars(select(ResultDetail))).one().payload["oracle"]["measure"]["amount"] is None
            if fault == "foreign_page":
                assert collections[0].page_count == 0 and not (await session.scalars(select(ResultDetail))).all()
            if fault == "repeated_token":
                assert collections[0].page_count == 1 and not (await session.scalars(select(ResultDetail))).all()
    finally:
        await engine.dispose()


def test_billing_requires_explicit_admitted_request_limit():
    from types import SimpleNamespace

    from l5_services.billing_collection import request_limit

    assert request_limit(SimpleNamespace(admission={"limits": {}})) is None
    assert request_limit(SimpleNamespace(admission={"limits": {"max_ce_requests": True}})) is None
    assert request_limit(SimpleNamespace(admission={"limits": {"max_ce_requests": 0}})) is None
    assert request_limit(SimpleNamespace(admission={"limits": {"max_ce_requests": 1}})) == 1


def raw_billing_pages():
    import json
    from hashlib import sha256

    def page(groups, request_token=None, next_token=None, version="1"):
        response = {"GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"}],
                    "ResultsByTime": [{"TimePeriod": {"Start": QUERY["start"], "End": QUERY["end"]},
                                       "Estimated": False, "Groups": [
                                           {"Keys": [name], "Metrics": {"UnblendedCost": {"Amount": amount,
                                                                                              "Unit": "USD"}}}
                                           for name, amount in groups]}]}
        if next_token is not None:
            response["NextPageToken"] = next_token
        digest = sha256(json.dumps(response, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        artifact = {"artifact_id": f"page-{version}", "version": version, "sha256": digest}
        return {"request_token": request_token, "response": response, "artifact": artifact}

    return [page([("usage", "10.125"), ("credit", "-0.125")], next_token="private-cursor", version="1"),
            page([("tiny", "0.00000001")], request_token="private-cursor", version="2")]


class BillingPageFixture:
    def __init__(self, pages, native, metadata=None):
        self.pages = pages
        self.native = native
        self.metadata = metadata or {}
        self.saved = {}
        self.requests = []

    async def collect_billing_page(self, run, claim, provider_session, request, cursor, collection_id):
        from l5_services.billing_collection import BillingUnavailable

        assert provider_session is not None
        assert request["query"] == run.input["query"]
        self.requests.append((collection_id, cursor))
        if len(self.requests) > len(self.pages):
            raise BillingUnavailable()
        page = {**self.pages[len(self.requests) - 1], "collection_id": collection_id}
        self.saved[(collection_id, page["artifact"]["artifact_id"])] = page
        return page

    async def read_billing_page(self, run, claim, provider_session, collection_id, reference):
        return self.saved[(collection_id, reference["artifact_id"])]

    async def read_native_cost(self, run, claim, provider_session):
        from l5_services.billing_collection import query_identity

        return {**self.native, "query_hash": query_identity(run)[1]}

    async def read_billing_metadata(self, run, claim, provider_session, collection_id):
        return self.metadata


def test_raw_ce_page_sequence_preserves_signed_amounts_and_private_cursor():
    from l5_services.adapters.finops.billing import normalize_pages

    pages = raw_billing_pages()
    native, _ = sources()
    billing = normalize_pages(QUERY, "a" * 64, "b" * 64, "collection-example", pages,
                              "2026-02-03T00:00:00Z")
    assert billing["amount"] == "10.00000001"
    assert billing["series"][1]["amount"] == "-0.125"
    assert billing["pages"] == [page["artifact"] for page in pages]
    assert "private-cursor" not in str(billing)
    assert compare_cost_series(QUERY, native, billing)["state"] == "exact"
    with pytest.raises(ValueError, match="cursor"):
        normalize_pages(QUERY, "a" * 64, "b" * 64, "collection-example",
                        [pages[0], {**pages[1], "request_token": "wrong"}], "2026-02-03T00:00:00Z")
    with pytest.raises(ValueError, match="digest"):
        normalize_pages(QUERY, "a" * 64, "b" * 64, "collection-example",
                        [{**pages[0], "artifact": {**pages[0]["artifact"], "sha256": "f" * 64}}, pages[1]],
                        "2026-02-03T00:00:00Z")


def cost_case(case):
    import json
    from hashlib import sha256
    from types import SimpleNamespace

    from l5_services.adapters.finops.billing import normalize_pages

    query = deepcopy(QUERY)
    native, _ = sources()
    artifact = {"artifact_id": "native-example", "version": "1", "sha256": "a" * 64}
    snapshot = {"artifact_id": "scope-example", "version": "1", "sha256": "c" * 64}
    observed_at = "2026-02-02T00:00:00Z"
    answer = {"kind": "finops_native_answer", "native_job_ref": {"turn_id": "turn-example"},
              "answer_artifact": artifact, "status": "answered", "observed_at": observed_at}
    native.update(query=query, answer_artifact=artifact, status="answered", observed_at=observed_at,
                  source_refreshed_at=None, freshness_reason=None)
    pages = raw_billing_pages()
    if case == "missing":
        pages = []
    elif case in {"empty", "unknown"}:
        if case == "empty":
            pages = pages[:1]
            pages[0]["response"]["ResultsByTime"] = []
            pages[0]["response"].pop("NextPageToken")
        else:
            for page in pages:
                page["response"]["ResultsByTime"][0].pop("Estimated")
        for page in pages:
            page["artifact"]["sha256"] = sha256(json.dumps(
                page["response"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    elif case == "partial":
        pages = pages[:1]
    elif case == "zero":
        query["group_by"] = []
        native["query"] = query
        native["series"] = [{**series("unused", "0.000"), "group": [], "group_by": []}]
        native["amount"] = "0.000"
        response = {"GroupDefinitions": [], "ResultsByTime": [
            {"TimePeriod": {"Start": QUERY["start"], "End": QUERY["end"]}, "Estimated": False,
             "Total": {"UnblendedCost": {"Amount": "0.000", "Unit": "USD"}}}]}
        pages = [{"request_token": None, "response": response,
                  "artifact": {"artifact_id": "zero-example", "version": "1",
                               "sha256": sha256(json.dumps(response, sort_keys=True,
                                                           separators=(",", ":")).encode()).hexdigest()}}]
    billing = normalize_pages(query, "a" * 64, "b" * 64, "collection-example", pages,
                              "2026-02-03T00:00:00Z")
    if case == "stale":
        billing["source_refreshed_at"] = "2025-12-01T00:00:00Z"
        billing["freshness_reason"] = "stale"
    if case == "missing":
        billing["amount"] = "0"
    scope = {"tenant_id": "tenant-example", "product_id": "product-example",
             "project_id": "project-example", "owner_id": "owner-example"}
    run = SimpleNamespace(run_id="run-example", native_job_ref=answer["native_job_ref"],
                          provider_readback_ref=answer, input={"query": query}, binding_generation=1,
                          admission={"scope": scope, "billing_scope": {"scope_digest": "a" * 64,
                                     "account_ids": ["123456789012"], "view_ref": "view-example",
                                     "snapshot_artifact": snapshot}})
    run.pages = pages
    return run, native, billing


@pytest.mark.parametrize("case", ["missing", "empty", "partial", "unknown", "stale", "zero"])
async def test_truthful_results(case, tmp_path):
    import json
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    import httpx
    from fastapi import FastAPI, HTTPException
    from jsonschema import Draft202012Validator, FormatChecker
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.api.router import create_router
    from l5_services.adapters.protocol import AdapterOutcome
    from l5_services.findings import ResultStore
    from l5_services.finops_reconciliation import normalize_cost_result
    from l5_services.models import ResultDetail
    from l5_services.models.base import Base
    from l5_services.models.evidence import AssessmentResult
    from l5_services.models.run import Run
    from l5_services.models.work import Work
    from l5_services.workers.execution import ExecutionService

    fixture, native, billing = cost_case(case)
    result = normalize_cost_result(fixture, native, billing)
    expected_amount = "0.000" if case == "zero" else None if case in {"missing", "empty", "partial"} else "10.00000001"
    expected_coverage = "unavailable" if case in {"missing", "empty"} else "partial" if case == "partial" else "complete"
    expected_state = {"missing": "unavailable", "empty": "unavailable", "partial": "incomparable",
                      "unknown": "incomparable", "stale": "incomparable", "zero": "exact"}[case]
    assert result["oracle"]["measure"]["amount"] == expected_amount
    assert result["oracle"]["measure"]["coverage"] == expected_coverage
    assert result["comparison"]["state"] == expected_state
    assert result["native"]["measure"]["amount"] == native["amount"]
    assert result["query"] == fixture.input["query"]
    assert result["native"]["observed_at"] == fixture.provider_readback_ref["observed_at"]
    assert result["oracle"]["measure"]["source_refreshed_at"] == (
        "2025-12-01T00:00:00Z" if case == "stale" else None)
    assert result["oracle"]["measure"]["freshness_reason"] == (
        "stale" if case == "stale" else "source_refresh_unknown")
    expected_estimated = None if case in {"missing", "empty", "unknown"} else False
    assert result["oracle"]["measure"]["estimated"] is expected_estimated
    assert result["attribution"] == ("unknown" if case in {"missing", "empty", "partial"} else "account")
    schema = json.loads((Path(__file__).parents[4] / "docs/l5-services/finops/contract.json").read_text())
    Draft202012Validator({**schema, "$ref": "#/$defs/FinopsCostResult"},
                         format_checker=FormatChecker()).validate(result)

    now = datetime(2026, 2, 3, tzinfo=UTC)
    fixture.admission["monitoring_policy"] = {"poll_grace_seconds": 60}
    fixture.admission["limits"] = {"max_ce_requests": 1 if case == "partial" else 2}
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'truth.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Run(tenant_id="tenant-example", run_id="run-example", product_id="product-example",
                            project_id="project-example", service_id="finops", configuration_id="config-example",
                            configuration_revision=1, binding_id="binding-example", trigger_id="manual-example",
                            binding_generation=1, state="collecting", provider_state="completed",
                            input=fixture.input, admission=fixture.admission,
                            native_job_ref=fixture.native_job_ref, provider_readback_ref=fixture.provider_readback_ref,
                            deadline_at=datetime(2026, 2, 4, tzinfo=UTC)))
            session.add(Work(tenant_id="tenant-example", run_id="run-example", work_id="billing-work",
                             kind="billing", generation=1, step=2, due_at=now))
            await session.commit()

        async def authorize(run, claim, operation):
            assert operation == "collect" and claim.kind == "billing"
            return object()

        worker = ExecutionService(sessions, None, None, None, authorize, clock=lambda: now,
                                  finops_adapter=BillingPageFixture(fixture.pages, native, {
                                      "source_refreshed_at": billing["source_refreshed_at"],
                                      "freshness_reason": billing["freshness_reason"]}))
        assert await worker.execute({"schema_version": "1.0", "work_id": "billing-work", "generation": 1})
        async with sessions() as session:
            pending = (await session.scalars(select(Work).where(Work.kind == "billing",
                                                             Work.completed_at.is_(None)))).all()
        if pending:
            assert await worker.execute({"schema_version": "1.0", "work_id": pending[0].work_id,
                                         "generation": 1})
        async with sessions() as session:
            stored = await session.get(Run, ("tenant-example", "run-example"))
            assert stored.state == ("succeeded" if expected_state == "exact" else "partial")
            detail = (await session.scalars(select(ResultDetail))).one()
            result_id = detail.result_id
            evidence = (await session.scalars(select(AssessmentResult))).one()
            assert evidence.payload["native_artifact"] == native["answer_artifact"]
            assert evidence.payload["billing_pages"] == billing["pages"]
            from l5_services.models.publication import Publication

            publications = (await session.scalars(select(Publication))).all()
            assert {item.destination for item in publications} == {"product-context", "engineering-insights"}
            assert all(item.result_id == result_id and item.state == "pending"
                       and item.receipt is None for item in publications)

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != ("product-example", "project-example", "finops"):
                    raise HTTPException(404, detail={"code": "not_found"})
                run = await self.session.get(Run, ("tenant-example", "run-example"))
                return ResultStore(self.session, [run])

        async def backend():
            async with sessions() as session:
                yield Backend(session)

        app = FastAPI()
        app.include_router(create_router(backend))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            path = f"/v1/products/product-example/projects/project-example/services/finops/results/{result_id}"
            response = await client.get(path)
            assert response.status_code == 200 and response.headers["etag"] == '"1"'
            assert response.json()["oracle"]["measure"]["amount"] == expected_amount
            assert response.json()["native"]["answer_artifact"] == native["answer_artifact"]
            assert response.json()["comparison"]["state"] == expected_state
            assert response.json()["scope"] == fixture.admission["scope"]
            assert response.json()["query"] == fixture.input["query"]
            assert response.json()["native"]["observed_at"] == fixture.provider_readback_ref["observed_at"]
            assert response.json()["oracle"]["measure"]["estimated"] is expected_estimated
            assert response.json()["oracle"]["measure"]["source_refreshed_at"] == (
                "2025-12-01T00:00:00Z" if case == "stale" else None)
    finally:
        await engine.dispose()


async def test_account_only_attribution_and_project_tenant_isolation(tmp_path):
    from datetime import UTC, datetime

    import httpx
    from fastapi import FastAPI, HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.api.router import create_router
    from l5_services.findings import ResultStore
    from l5_services.finops_reconciliation import normalize_cost_result
    from l5_services.models import ResultDetail
    from l5_services.models.base import Base
    from l5_services.models.evidence import AssessmentResult
    from l5_services.models.run import Run

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'scopes.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    targets = [("tenant-example", "project-example", "owned"),
               ("tenant-example", "other-project", "other-project"),
               ("other-tenant", "project-example", "other-tenant")]
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            for tenant_id, project_id, suffix in targets:
                fixture, native, billing = cost_case("account")
                fixture.run_id = f"run-{suffix}"
                fixture.admission["scope"].update(tenant_id=tenant_id, project_id=project_id)
                digest = {"owned": "a", "other-project": "d", "other-tenant": "e"}[suffix] * 64
                fixture.admission["billing_scope"]["scope_digest"] = digest
                native["scope_digest"] = digest
                billing["scope_digest"] = digest
                billing["attribution"] = "product_verified"
                billing["attribution_evidence"] = [
                    {"artifact_id": f"unqualified-{suffix}", "version": "1", "sha256": digest}]
                native["attribution"] = "product_verified"
                result = normalize_cost_result(fixture, native, billing)
                assert result["attribution"] == "account" and result["attribution_evidence"] == []
                assert result["scope_digest"] == digest
                session.add(Run(tenant_id=tenant_id, run_id=fixture.run_id, product_id="product-example",
                                project_id=project_id, service_id="finops", configuration_id=f"config-{suffix}",
                                configuration_revision=1, binding_id=f"binding-{suffix}",
                                trigger_id=f"manual-{suffix}", binding_generation=1,
                                input=fixture.input, admission=fixture.admission,
                                native_job_ref=fixture.native_job_ref,
                                provider_readback_ref=fixture.provider_readback_ref,
                                deadline_at=datetime(2026, 2, 4, tzinfo=UTC)))
                session.add(AssessmentResult(tenant_id=tenant_id, result_id=f"result-{suffix}",
                                             run_id=fixture.run_id, version=1, payload={"kind": "cost_evidence"}))
                session.add(ResultDetail(tenant_id=tenant_id, result_id=f"result-{suffix}",
                                         payload={"result_id": f"result-{suffix}", "revision": 1,
                                                  "scope": fixture.admission["scope"], **result}))
            await session.commit()

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                if (product_id, project_id, service_id) != ("product-example", "project-example", "finops"):
                    raise HTTPException(404, detail={"code": "not_found"})
                runs = (await self.session.scalars(select(Run).where(
                    Run.tenant_id == "tenant-example", Run.product_id == product_id,
                    Run.project_id == project_id, Run.service_id == service_id))).all()
                return ResultStore(self.session, runs)

        async def backend():
            async with sessions() as session:
                yield Backend(session)

        app = FastAPI()
        app.include_router(create_router(backend))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local.test") as client:
            path = "/v1/products/product-example/projects/project-example/services/finops/results"
            listed = await client.get(path)
            assert listed.status_code == 200
            assert [item["result_id"] for item in listed.json()["items"]] == ["result-owned"]
            owned = await client.get(f"{path}/result-owned")
            assert owned.status_code == 200 and owned.json()["attribution"] == "account"
            assert (await client.get(f"{path}/result-other-project")).status_code == 404
            assert (await client.get(f"{path}/result-other-tenant")).status_code == 404
            assert (await client.get(path.replace("project-example", "other-project"))).status_code == 404
    finally:
        await engine.dispose()
