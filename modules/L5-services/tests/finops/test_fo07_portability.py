"""Independent offline cost and route fixtures; native acceptance needs a live provider."""

import json
import os
import subprocess
import sys
import zipfile
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import httpx
import l5_services
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.finops.billing import normalize_pages
from l5_services.api.router import create_router
from l5_services.findings import ResultStore
from l5_services.finops_reconciliation import compare_cost_series, normalize_cost_result
from l5_services.models import ResultDetail, Run
from l5_services.models.base import Base
from l5_services.models.evidence import AssessmentResult


QUERY = {"start": "2026-09-01", "end": "2026-10-01", "metric": "UnblendedCost",
         "currency": "USD", "granularity": "MONTHLY", "group_by": ["SERVICE"]}
OBSERVED_AT = "2026-10-27T00:00:00Z"
QUERY_HASH = "c" * 64
CASES = (
    ("project-north", "a" * 64,
     ("10.125", "-0.125", "0.00000001"),
     ("10.125", "-0.125", "0.00000001"), "10.00000001"),
    ("project-south", "b" * 64,
     ("7.100", "-1.250", "0.00000001"),
     ("7.100", "-1.250", "0.00000001"), "5.85000001"),
)


def installed_identity():
    installed = os.environ.get("FO07_INSTALLED_ROOT")
    if installed is None:
        return None
    assert Path(l5_services.__file__).resolve().is_relative_to(Path(installed).resolve())
    wheel = Path(os.environ["FO07_WHEEL_PATH"])
    digest = sha256(wheel.read_bytes()).hexdigest()
    assert digest == os.environ["FO07_WHEEL_SHA256"]
    return digest


def cost_row(group, amount):
    return {"period": {"start": QUERY["start"], "end": QUERY["end"]},
            "group": [group], "amount": amount, "metric": QUERY["metric"],
            "currency": QUERY["currency"], "granularity": QUERY["granularity"],
            "group_by": QUERY["group_by"]}


def ce_pages(project_id, amounts):
    pages = []
    for index, subset in enumerate((amounts[:2], amounts[2:])):
        groups = ("compute", "credit", "tiny")[:2] if index == 0 else ("tiny",)
        response = {
            "GroupDefinitions": [{"Type": "DIMENSION", "Key": "SERVICE"}],
            "ResultsByTime": [{"TimePeriod": {"Start": QUERY["start"], "End": QUERY["end"]},
                               "Estimated": False,
                               "Groups": [{"Keys": [name], "Metrics": {"UnblendedCost": {
                                   "Amount": amount, "Unit": "USD"}}}
                                          for name, amount in zip(groups, subset)]}],
        }
        if index == 0:
            response["NextPageToken"] = f"cursor-{project_id}"
        payload = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        pages.append({"request_token": None if index == 0 else f"cursor-{project_id}",
                      "response": response, "artifact": {
                          "artifact_id": f"billing-{project_id}-{index}", "version": "1",
                          "sha256": sha256(payload).hexdigest()}})
    return pages


def evidence(case):
    project_id, scope_digest, native_amounts, ce_amounts, expected_total = case
    native_ref = {"turn_id": f"turn-{project_id}"}
    artifact = {"artifact_id": f"answer-{project_id}", "version": "1", "sha256": "d" * 64}
    answer = {"kind": "finops_native_answer", "native_job_ref": native_ref,
              "status": "answered", "observed_at": OBSERVED_AT, "answer_artifact": artifact}
    run = SimpleNamespace(
        run_id=f"run-{project_id}", product_id="product-example", project_id=project_id,
        tenant_id="tenant-example", configuration_id=f"config-{project_id}",
        binding_id=f"binding-{project_id}", trigger_id=f"manual-{project_id}",
        binding_generation=1, native_job_ref=native_ref, provider_readback_ref=answer,
        input={"query": QUERY}, admission={"scope": {
            "tenant_id": "tenant-example", "product_id": "product-example", "project_id": project_id,
            "owner_id": "owner-example", "visibility": "only_me"},
            "billing_scope": {"scope_digest": scope_digest,
                              "account_ids": ["123456789012" if project_id == "project-north" else "000000000001"],
                              "view_ref": f"view-{project_id}", "snapshot_artifact": artifact}},
    )
    native = {"status": "answered", "coverage": "complete", "estimated": False,
              "currency": "USD", "scope_digest": scope_digest, "query_hash": QUERY_HASH,
              "query": QUERY, "answer_artifact": artifact, "observed_at": OBSERVED_AT,
              "amount": expected_total, "series": [cost_row(group, amount) for group, amount in
                                                  zip(("compute", "credit", "tiny"), native_amounts)]}
    pages = ce_pages(project_id, ce_amounts)
    billing = normalize_pages(QUERY, scope_digest, QUERY_HASH, f"collection-{project_id}",
                              pages, OBSERVED_AT)
    return run, native, billing, pages


def test_two_project_reconciliation():
    release_digest = installed_identity()
    assert CASES[0][1] != CASES[1][1]
    assert CASES[0][4] != CASES[1][4]
    results = []
    for case in CASES:
        run, native, billing, pages = evidence(case)
        result = normalize_cost_result(run, native, billing)
        assert result["comparison"]["state"] == "exact"
        assert result["native"]["measure"]["amount"] == case[4]
        assert result["oracle"]["measure"]["amount"] == case[4]
        assert result["oracle"]["pages"] == [page["artifact"] for page in pages]
        assert result["native"]["answer_artifact"] == run.provider_readback_ref["answer_artifact"]
        assert result["attribution"] == "account"
        results.append((run, native, billing, pages))

    first_run, first_native, _, first_pages = results[0]
    _, _, other_billing, _ = results[1]
    assert compare_cost_series(QUERY, first_native, other_billing)["reason"] == "scope_or_query_mismatch"
    with pytest.raises(ValueError, match="pinned scope or query"):
        normalize_cost_result(first_run, first_native, other_billing)
    with pytest.raises(ValueError, match="cursor"):
        normalize_pages(QUERY, first_run.admission["billing_scope"]["scope_digest"], QUERY_HASH,
                        "collection-example", [first_pages[0], {**first_pages[1], "request_token": "foreign"}],
                        OBSERVED_AT)
    with pytest.raises(ValueError, match="digest"):
        tampered = deepcopy(first_pages)
        tampered[0]["response"]["ResultsByTime"][0]["Groups"][1]["Metrics"]["UnblendedCost"]["Amount"] = "0.125"
        normalize_pages(QUERY, first_run.admission["billing_scope"]["scope_digest"], QUERY_HASH,
                        "collection-example", tampered, OBSERVED_AT)
    assert installed_identity() == release_digest


def test_scn02_independent_inputs_and_counterexamples():
    expected = {
        "project-north": {"compute": "10.125", "credit": "-0.125", "tiny": "0.00000001"},
        "project-south": {"compute": "7.100", "credit": "-1.250", "tiny": "0.00000001"},
    }
    assert len(CASES) == len(expected)
    scopes = set()
    for case in CASES:
        run, native, billing, pages = evidence(case)
        scope = run.admission["billing_scope"]
        assert run.project_id in expected and run.product_id == "product-example"
        assert run.admission["scope"]["tenant_id"] == "tenant-example"
        assert scope["view_ref"] and scope["account_ids"] and scope["snapshot_artifact"]
        assert run.provider_readback_ref["native_job_ref"] == run.native_job_ref
        assert native["answer_artifact"] == run.provider_readback_ref["answer_artifact"]
        assert native["answer_artifact"] not in billing["pages"]
        assert native["query"] == billing["query"] == QUERY
        assert native["scope_digest"] == billing["scope_digest"] == scope["scope_digest"]
        assert native["estimated"] is billing["estimated"] is False
        assert native["coverage"] == billing["coverage"] == "complete"
        assert len(pages) == len(billing["pages"]) == 2
        assert billing["pages"] == [page["artifact"] for page in pages]
        scopes.add(scope["scope_digest"])
        for source in (native, billing):
            observed = {item["group"][0]: item["amount"] for item in source["series"]}
            assert observed == expected[run.project_id]
            assert Decimal(source["amount"]) == sum(map(Decimal, expected[run.project_id].values()))

        changed = deepcopy(billing)
        credit = Decimal(changed["series"][1]["amount"])
        changed["series"][1]["amount"] = str(-credit)
        changed["amount"] = str(Decimal(changed["amount"]) - 2 * credit)
        assert compare_cost_series(QUERY, native, changed)["state"] == "mismatch"
        changed = deepcopy(billing)
        changed["estimated"] = None
        assert compare_cost_series(QUERY, native, changed)["state"] == "incomparable"
        partial = normalize_pages(QUERY, scope["scope_digest"], QUERY_HASH,
                                  billing["collection_id"], pages[:1], OBSERVED_AT)
        assert partial["coverage"] == "partial" and partial["amount"] is None
    assert len(scopes) == 2


async def test_two_project_results_stay_scoped_on_host_route(tmp_path):
    release_digest = installed_identity()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'finops-projects.db'}")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    def add_result(session, fixture, result_id, result):
        session.add(Run(tenant_id=fixture.tenant_id, run_id=fixture.run_id,
                        product_id=fixture.product_id, project_id=fixture.project_id,
                        service_id="finops", configuration_id=fixture.configuration_id,
                        configuration_revision=1, binding_id=fixture.binding_id,
                        trigger_id=fixture.trigger_id, binding_generation=1,
                        input=fixture.input, admission=fixture.admission,
                        native_job_ref=fixture.native_job_ref,
                        provider_readback_ref=fixture.provider_readback_ref,
                        deadline_at=datetime(2026, 10, 28, tzinfo=UTC)))
        session.add(AssessmentResult(tenant_id=fixture.tenant_id, result_id=result_id,
                                     run_id=fixture.run_id, version=1,
                                     payload={"kind": "cost_evidence"}))
        session.add(ResultDetail(tenant_id=fixture.tenant_id, result_id=result_id,
                                 payload={"result_id": result_id, "revision": 1,
                                          "scope": fixture.admission["scope"], **result}))

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            for case in CASES:
                fixture, native, billing, _ = evidence(case)
                result = normalize_cost_result(fixture, native, billing)
                add_result(session, fixture, f"result-{fixture.project_id}", result)
            foreign = ("project-north", "e" * 64,
                       ("1.000", "-0.250", "0.00000001"),
                       ("1.000", "-0.250", "0.00000001"), "0.75000001")
            fixture, native, billing, _ = evidence(foreign)
            fixture.tenant_id = "tenant-foreign"
            fixture.run_id = "run-foreign"
            fixture.admission["scope"]["tenant_id"] = "tenant-foreign"
            add_result(session, fixture, "result-project-north", normalize_cost_result(fixture, native, billing))
            await session.commit()

        class Backend:
            def __init__(self, session):
                self.session = session

            async def result_store(self, product_id, project_id, service_id):
                if product_id != "product-example" or service_id != "finops":
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
            for own, foreign in (("project-north", "project-south"),
                                 ("project-south", "project-north")):
                path = f"/v1/products/product-example/projects/{own}/services/finops/results"
                owned = await client.get(f"{path}/result-{own}")
                assert owned.status_code == 200
                assert owned.json()["scope_digest"] == next(case[1] for case in CASES if case[0] == own)
                assert (await client.get(f"{path}/result-{foreign}")).status_code == 404
                assert [item["result_id"] for item in (await client.get(path)).json()["items"]] == [
                    f"result-{own}"]
    finally:
        await engine.dispose()
    assert installed_identity() == release_digest


def test_scoped_route_uses_same_installed_wheel_for_both_projects(tmp_path):
    module = Path(__file__).resolve().parents[2]
    output = tmp_path / "release"
    built = subprocess.run(["bash", str(module / "scripts/build-package.sh"), str(output)],
                           capture_output=True, text=True, check=True)
    wheel = Path(built.stdout.strip().splitlines()[-1])
    assert wheel.parent == output
    installed = tmp_path / "installed"
    with zipfile.ZipFile(wheel) as package:
        names = set(package.namelist())
        assert {"l5_services/api/router.py", "l5_services/adapters/finops/billing.py",
                "l5_services/finops_reconciliation.py"} <= names
        package.extractall(installed)
    digest = sha256(wheel.read_bytes()).hexdigest()
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join((
        str(installed), str(module.parent / "gateway/src"))),
        "FO07_INSTALLED_ROOT": str(installed), "FO07_WHEEL_PATH": str(wheel),
        "FO07_WHEEL_SHA256": digest, "AWS_CONFIG_FILE": "/dev/null",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null", "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_DEFAULT_REGION": "us-east-1"}
    result = subprocess.run([
        sys.executable, "-m", "pytest", "-q",
        f"{__file__}::test_two_project_reconciliation",
        f"{__file__}::test_scn02_independent_inputs_and_counterexamples",
        f"{__file__}::test_two_project_results_stay_scoped_on_host_route",
        str(module / "tests/finops/test_fo07_recovery.py"),
        str(module / "tests/finops/test_fo03.py") + "::test_submit_recovery",
        str(module / "tests/finops/test_fo03.py") + "::test_session_recovery",
        str(module / "tests/finops/test_fo04.py") + "::test_collection_recovery",
        str(module / "tests/finops/test_fo06.py") + "::test_publication",
        str(module / "tests/finops/test_fo06.py") + "::test_cost_source_revocation",
        str(module / "tests/finops/test_fo07_retention.py"),
        str(module / "tests/finops/test_retained_scope.py") +
        "::test_retained_agent_scope_masks_cross_project_and_tenant_collisions",
        str(module / "tests/integration/test_security_binding.py") +
        "::test_finops_retirement_keeps_adopted_and_referenced_resources",
        str(module.parent / "gateway/tests/internal/test_finops_mcp.py") + "::test_client_outcomes",
        str(module.parent / "gateway/tests/l5/test_finops_reuse.py") +
        "::test_finops_result_source_rights_fail_closed_after_revocation",
    ], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sha256(wheel.read_bytes()).hexdigest() == digest
