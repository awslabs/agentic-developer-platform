"""Same-name CI checks need authenticated workflow and current-attempt identity."""

import copy
import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.repository_evaluation_contract import Check, CheckReceipt
from src.orchestration.repository_evaluation_provider import RepositoryEvidenceProvider
from src.orchestration.review_cycle import CycleBlockedError

HEAD = "a" * 40
PREFIX = "/repos/o/r"


@pytest.fixture
def checks():
    rows, runs, jobs, requirements = [], {}, {}, []
    for index, workflow in enumerate(("tools", "shared-tools", "cyber-tools"), 1):
        selector = dict(workflow_id=10 + index, workflow_path=f".github/workflows/{workflow}.yml")
        requirements.append(dict(name="verify", app_id=15368, workflow=selector))
        rows.append(
            dict(
                id=index, name="verify", app=dict(id=15368), head_sha=HEAD, check_suite=dict(id=100 + index), status="completed", conclusion="success"
            )
        )
        runs[100 + index] = dict(
            id=200 + index,
            workflow_id=selector["workflow_id"],
            path=selector["workflow_path"],
            check_suite_id=100 + index,
            head_sha=HEAD,
            repository=dict(id=123),
            head_repository=dict(id=123),
            run_attempt=1,
            status="completed",
            conclusion="success",
        )
        jobs[200 + index] = [
            dict(
                id=300 + index,
                run_id=200 + index,
                run_attempt=1,
                name="verify",
                head_sha=HEAD,
                check_run_url=f"https://api.github.com{PREFIX}/check-runs/{index}",
                status="completed",
                conclusion="success",
            )
        ]
    return SimpleNamespace(
        rows=rows,
        runs=runs,
        jobs=jobs,
        source=dict(head_sha=HEAD, required_checks=requirements),
        calls=[],
        fresh_change={},
        listing_change=None,
        denied=False,
        paginated=False,
        unbounded=None,
    )


async def observe(data):
    def transport(request):
        assert request.method == "GET" and request.url.host == "api.github.com"
        data.calls.append((request.url.path, dict(request.url.params)))
        path = request.url.path
        if path == f"{PREFIX}/commits/{HEAD}/check-runs":
            assert request.url.params["filter"] == "latest"
            rows = [dict(name="other", app=dict(id=1))] * 100 if data.paginated and request.url.params["page"] == "1" else data.rows
            return httpx.Response(200, json=dict(check_runs=rows))
        if data.denied:
            return httpx.Response(403)
        if path == f"{PREFIX}/actions/runs":
            assert request.url.params["head_sha"] == HEAD
            run = data.runs[int(request.url.params["check_suite_id"])]
            rows = [run] if data.listing_change is None else data.listing_change([run])
            if data.unbounded == "runs":
                rows = [run] * 100
            return httpx.Response(200, json=dict(workflow_runs=rows))
        run_id = int(path.split("/")[6])
        if path.endswith("/jobs"):
            rows = data.jobs[run_id]
            if callback := getattr(data, "after_jobs_read", None):
                callback(run_id)
            if data.unbounded == "jobs" or (data.paginated and request.url.params["page"] == "1"):
                rows = [dict(check_run_url="https://api.github.com/repos/o/r/check-runs/999")] * 100
            return httpx.Response(200, json=dict(jobs=rows))
        assert path == f"{PREFIX}/actions/runs/{run_id}"
        run = next(run for run in data.runs.values() if run["id"] == run_id)
        return httpx.Response(200, json={**run, **data.fresh_change})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(client=client)
        provider.token = AsyncMock(return_value="read-only-fixture-token")
        return await provider.required_checks(SimpleNamespace(repo="o/r", provider_repository_id=123), data.source)


async def test_qualified_namesakes_keep_all_check_and_workflow_identities(checks):
    receipts = await observe(checks)
    assert [row["check_run_id"] for row in receipts] == [1, 2, 3]
    for index, receipt in enumerate(receipts, 1):
        assert CheckReceipt.model_validate(receipt).model_dump(mode="json") == receipt
        assert receipt["workflow"] == dict(
            **checks.source["required_checks"][index - 1]["workflow"],
            check_suite_id=100 + index,
            run_id=200 + index,
            run_attempt=1,
            job_id=300 + index,
        )
    listings = [params for path, params in checks.calls if path == f"{PREFIX}/actions/runs"]
    assert {row["check_suite_id"] for row in listings} == {"101", "102", "103"}
    assert len(listings) == 3  # Cache provenance only inside this one observation.


async def test_many_checks_share_one_attempt_job_read_and_final_run_fence(checks):
    for index, name in enumerate(("Lint", "Build", "Unit tests", "Smoke"), 4):
        checks.source["required_checks"].append({**checks.source["required_checks"][0], "name": name})
        checks.rows.append({**checks.rows[0], "id": index, "name": name})
        checks.jobs[201].append(
            {**checks.jobs[201][0], "id": 300 + index, "name": name, "check_run_url": f"https://api.github.com{PREFIX}/check-runs/{index}"}
        )
    receipts = await observe(checks)
    assert [receipt["check_run_id"] for receipt in receipts] == list(range(1, 8))
    same_run = [receipt for receipt in receipts if receipt["workflow"]["run_id"] == 201]
    assert len(same_run) == 5 and {receipt["workflow"]["run_attempt"] for receipt in same_run} == {1}
    paths = [path for path, _ in checks.calls]
    assert paths.count(f"{PREFIX}/actions/runs/201/attempts/1/jobs") == 1
    assert paths.count(f"{PREFIX}/actions/runs/201") == 1


async def test_final_fence_rechecks_earlier_workflow_after_later_checks_are_read(checks):
    def restart_earlier_workflow(run_id):
        if run_id == 202:
            checks.runs[101]["run_attempt"] = 2

    checks.after_jobs_read = restart_earlier_workflow
    with pytest.raises(CycleBlockedError, match="required_check_workflow_changed"):
        await observe(checks)


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("state", ["failure", "pending"])
async def test_successful_namesakes_never_replace_an_intended_failed_check(checks, order, state):
    checks.rows[0]["conclusion"] = state if state == "failure" else None
    checks.rows[0]["status"] = "completed" if state == "failure" else "in_progress"
    checks.rows = [checks.rows[index] for index in order]
    with pytest.raises(CycleBlockedError, match="required_check_not_successful"):
        await observe(checks)


async def test_legacy_ambiguous_selector_stays_closed_without_workflow_reads(checks):
    checks.source["required_checks"] = [dict(name="verify", app_id=15368)]
    with pytest.raises(CycleBlockedError, match="required_check_missing_or_ambiguous"):
        await observe(checks)
    assert len(checks.calls) == 1


async def test_legacy_single_check_contract_and_receipt_keep_exact_serialization(checks):
    checks.rows = checks.rows[:1]
    legacy = dict(name="verify", app_id=15368)
    checks.source["required_checks"] = [legacy]
    assert Check.model_validate(legacy).model_dump(mode="json") == legacy
    receipt = dict(**legacy, check_run_id=1, head_sha=HEAD)
    assert await observe(checks) == [receipt]
    assert CheckReceipt.model_validate(receipt).model_dump(mode="json") == receipt
    assert len(checks.calls) == 1


@pytest.mark.parametrize("change", [dict(workflow_id=999), dict(workflow_path=".github/workflows/other.yml")])
async def test_qualified_selector_requires_both_workflow_id_and_path(checks, change):
    checks.source["required_checks"][0]["workflow"].update(change)
    with pytest.raises(CycleBlockedError, match="required_check_missing_or_ambiguous"):
        await observe(checks)


@pytest.mark.parametrize(
    "change",
    [
        dict(repository=dict(id=999)),
        dict(head_repository=dict(id=999)),
        dict(head_sha="b" * 40),
        dict(check_suite_id=999),
        dict(id=True),
        dict(workflow_id=True),
        dict(run_attempt=0),
    ],
)
async def test_workflow_provenance_must_match_selected_repository_revision_and_suite(checks, change):
    checks.runs[101].update(change)
    with pytest.raises(CycleBlockedError, match="required_check_workflow_identity"):
        await observe(checks)


@pytest.mark.parametrize("change", [lambda rows: [], lambda rows: rows * 2])
async def test_missing_or_ambiguous_suite_provenance_cannot_select_a_green_run(checks, change):
    checks.listing_change = change
    with pytest.raises(CycleBlockedError, match="required_check_workflow_missing_or_ambiguous"):
        await observe(checks)


async def test_two_checks_within_the_same_qualified_workflow_remain_ambiguous(checks):
    checks.rows.append({**checks.rows[0], "id": 4})
    with pytest.raises(CycleBlockedError, match="required_check_missing_or_ambiguous"):
        await observe(checks)


@pytest.mark.parametrize(
    "change",
    [
        dict(id=True),
        dict(run_id=999),
        dict(run_attempt=2),
        dict(head_sha="b" * 40),
        dict(name="other"),
        dict(status="in_progress"),
        dict(conclusion="failure"),
    ],
)
async def test_attempt_job_identity_and_success_are_required(checks, change):
    checks.jobs[201][0].update(change)
    with pytest.raises(CycleBlockedError, match="required_check_job_identity_or_result"):
        await observe(checks)


@pytest.mark.parametrize("change", ["missing", "duplicate", "external_url"])
async def test_check_run_must_bind_one_current_attempt_job(checks, change):
    if change == "missing":
        checks.jobs[201] = []
    elif change == "duplicate":
        checks.jobs[201] *= 2
    else:
        checks.jobs[201][0]["check_run_url"] = "https://attacker.invalid/check-runs/1"
    with pytest.raises(CycleBlockedError, match="required_check_job_missing_or_ambiguous"):
        await observe(checks)


async def test_previous_attempt_success_cannot_satisfy_current_attempt(checks):
    checks.runs[101]["run_attempt"] = 2
    with pytest.raises(CycleBlockedError, match="required_check_job_identity_or_result"):
        await observe(checks)
    assert any(path.endswith("/201/attempts/2/jobs") for path, _ in checks.calls)


async def test_reused_suite_cannot_move_a_check_to_another_declared_workflow(checks):
    checks.rows[0]["check_suite"] = checks.rows[1]["check_suite"]
    with pytest.raises(CycleBlockedError, match="required_check_missing_or_ambiguous"):
        await observe(checks)


async def test_paginated_checks_and_attempt_jobs_retain_the_exact_qualified_check(checks):
    checks.paginated = True
    assert [row["check_run_id"] for row in await observe(checks)] == [1, 2, 3]
    assert len([1 for path, _ in checks.calls if path.endswith("/check-runs")]) == 2
    assert len([1 for path, _ in checks.calls if path.endswith("/jobs")]) == 6


@pytest.mark.parametrize("listing", ["runs", "jobs"])
async def test_workflow_provenance_reads_keep_existing_pagination_bounds(checks, listing):
    checks.unbounded = listing
    with pytest.raises(CycleBlockedError, match="deployment_provider_history_limit"):
        await observe(checks)
    suffix = "/actions/runs" if listing == "runs" else "/jobs"
    pages = [int(params["page"]) for path, params in checks.calls if path.endswith(suffix)]
    assert pages == list(range(1, 11))


@pytest.mark.parametrize("change", [dict(run_attempt=2), dict(status="in_progress"), dict(conclusion="failure")])
async def test_run_recheck_refuses_a_new_attempt_or_changed_result(checks, change):
    checks.fresh_change = change
    with pytest.raises(CycleBlockedError, match="required_check_workflow_changed"):
        await observe(checks)


@pytest.mark.parametrize(
    "change", [dict(workflow_id=999), dict(path=".github/workflows/other.yml"), dict(repository=dict(id=999)), dict(check_suite_id=999)]
)
async def test_final_run_read_cannot_rebind_workflow_or_suite_identity(checks, change):
    checks.fresh_change = change
    with pytest.raises(CycleBlockedError, match="required_check_workflow_(changed|identity)"):
        await observe(checks)


async def test_workflow_read_denial_is_not_a_reason_to_fall_back_to_another_check(checks):
    checks.denied = True
    with pytest.raises(httpx.HTTPStatusError):
        await observe(checks)
    assert len(checks.calls) == 2


def test_qualified_contracts_require_complete_strict_workflow_identity(checks):
    check = checks.source["required_checks"][0]
    assert Check.model_validate(check).model_dump(mode="json") == check
    for workflow in [
        dict(workflow_id=11),
        dict(workflow_path=".github/workflows/tools.yml"),
        dict(workflow_id=True, workflow_path=".github/workflows/tools.yml"),
        dict(workflow_id=11, workflow_path="../tools.yml"),
    ]:
        with pytest.raises(ValueError):
            Check.model_validate({**check, "workflow": workflow})
    legacy_receipt = dict(name="verify", app_id=15368, check_run_id=1, head_sha=HEAD)
    with pytest.raises(ValueError):
        CheckReceipt.model_validate({**legacy_receipt, "workflow": copy.deepcopy(check["workflow"])})
