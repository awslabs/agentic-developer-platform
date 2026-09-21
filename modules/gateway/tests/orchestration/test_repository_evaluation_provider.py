"""Exercise the real provider parser against authenticated API/archive shapes."""

import asyncio
import base64
import copy
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.orchestration.repository_evaluation_contract import Predicate, RepositoryEvaluationSpecification, predicate_passes
from src.orchestration.repository_evaluation_provider import RepositoryEvidenceProvider
from src.orchestration.review_cycle import CycleBlockedError


def archive_for(data):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("evidence.json", data)
    return stream.getvalue()


@pytest.fixture
def evidence():
    now = datetime.now(UTC)
    source = dict(
        criterion_id="external-pr",
        issue_number=1,
        pr_number=2,
        head_sha="a" * 40,
        merge_sha="b" * 40,
        required_checks=[dict(name="Tests", app_id=15368)],
    )
    spec = RepositoryEvaluationSpecification.model_validate(
        dict(
            evidence_schema="repository-evaluation/v1",
            runner=dict(adapter="engine-repository-evidence-v1", repository="o/r", repository_id=123, harness_sha256="c" * 64),
            external_pull_requests=[source],
            workflows=[
                dict(
                    criterion_id="one-off-scan",
                    path=".github/workflows/scan.yml",
                    source=dict(revision="b" * 40),
                    definition=dict(revision="b" * 40),
                    required_jobs=["Full scan", "Cleanup"],
                    artifacts=[
                        dict(
                            name="evidence-{run_attempt}",
                            path="evidence.json",
                            predicates=[
                                dict(criterion_id="coverage", pointer="/coverage", operation="equals", expected=True),
                                dict(
                                    criterion_id="inventory",
                                    pointer="/records",
                                    operation="records",
                                    expected=["source-a", "source-b"],
                                    id_field="id",
                                    required_fields={"outcome": ["fixed", "false_positive"], "owner": ["S21"]},
                                ),
                            ],
                        )
                    ],
                )
            ],
        )
    )
    document = dict(
        coverage=True, records=[dict(id="source-a", outcome="fixed", owner="S21"), dict(id="source-b", outcome="false_positive", owner="S21")]
    )
    binary = archive_for(json.dumps(document).encode())
    definition = b"on:\n  workflow_dispatch:\njobs: {}\n"
    run = dict(
        id=10,
        run_number=3,
        run_attempt=2,
        repository=dict(id=123),
        head_repository=dict(id=123),
        head_sha="b" * 40,
        event="workflow_dispatch",
        path=".github/workflows/scan.yml",
        status="completed",
        conclusion="success",
        updated_at=now.isoformat(),
        run_started_at=(now - timedelta(seconds=20)).isoformat(),
    )
    responses = {
        "/repos/o/r": dict(id=123),
        "/repos/o/r/pulls/2": dict(
            number=2,
            node_id="PR_2",
            base=dict(repo=dict(id=123)),
            head=dict(repo=dict(id=123), sha="a" * 40),
            merged=True,
            merge_commit_sha="b" * 40,
            merged_at=(now - timedelta(days=1)).isoformat(),
        ),
        "/repos/o/r/commits/" + "a" * 40 + "/check-runs": dict(
            check_runs=[
                dict(id=7, name="Tests", app=dict(id=15368), head_sha="a" * 40, status="completed", conclusion="success"),
            ]
        ),
        "/repos/o/r/contents/.github/workflows/scan.yml": dict(
            type="file",
            encoding="base64",
            size=len(definition),
            content=base64.b64encode(definition).decode(),
            sha=hashlib.sha1(b"blob " + str(len(definition)).encode() + b"\0" + definition).hexdigest(),
        ),
        "/repos/o/r/actions/workflows/scan.yml/runs": dict(workflow_runs=[run]),
        "/repos/o/r/actions/runs/10": run,
        "/repos/o/r/actions/runs/10/attempts/2/jobs": dict(
            jobs=[
                dict(id=11, run_id=10, name="Full scan", status="completed", conclusion="success"),
                dict(id=12, run_id=10, name="Cleanup", status="completed", conclusion="success"),
            ]
        ),
        "/repos/o/r/actions/runs/10/artifacts": dict(
            artifacts=[
                dict(
                    id=13,
                    name="evidence-2",
                    expired=False,
                    size_in_bytes=len(binary),
                    digest="sha256:" + hashlib.sha256(binary).hexdigest(),
                    created_at=(now - timedelta(seconds=5)).isoformat(),
                )
            ]
        ),
    }
    return SimpleNamespace(
        now=now,
        spec=spec,
        source=source,
        responses=responses,
        binary=binary,
        document=document,
        review=SimpleNamespace(review_approved=True, head_sha="a" * 40),
    )


async def observe(data):
    def transport(request):
        assert request.method == "GET" and request.url.host == "api.github.com"
        if request.url.path == "/repos/o/r/actions/artifacts/13/zip":
            if callback := getattr(data, "after_artifact_read", None):
                callback()
            return httpx.Response(200, content=data.binary)
        assert request.url.path in data.responses, request.url.path
        return httpx.Response(200, json=data.responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(
            client=client, clock=lambda: data.now, reviews=SimpleNamespace(bound_pull_request=AsyncMock(return_value=data.review))
        )
        provider.token = AsyncMock(return_value="test-read-only-token")
        return await provider.observe(
            SimpleNamespace(org_id="org", installation_id=42, repo="o/r", provider_repository_id=123), data.spec, [data.source]
        )


async def test_real_provider_observation_binds_checks_workflow_attempt_and_digested_artifact(evidence):
    receipt = await observe(evidence)
    assert receipt["mandatory_passed"] is True
    assert receipt["pull_requests"][0]["checks"] == [dict(name="Tests", app_id=15368, check_run_id=7, head_sha="a" * 40)]
    workflow = receipt["workflows"][0]
    assert workflow["run_id"] == 10 and workflow["run_attempt"] == 2 and workflow["source_revision"] == "b" * 40
    assert workflow["artifacts"][0]["digest"] == hashlib.sha256(evidence.binary).hexdigest()
    assert {item["criterion_id"] for item in workflow["criteria"]} == {"coverage", "inventory"}


@pytest.mark.parametrize(
    "case",
    [
        "head",
        "review",
        "check_failed",
        "check_skipped",
        "check_missing",
        "check_app",
        "job_skipped",
        "job_missing",
        "wrong_event",
        "wrong_workflow",
        "wrong_repository",
        "stale",
        "archive_digest",
        "prior_artifact",
        "scheduled_scan",
        "duplicate_json",
    ],
)
async def test_missing_stale_skipped_and_wrong_producer_evidence_refuses(evidence, case):
    data = evidence
    pr = data.responses["/repos/o/r/pulls/2"]
    checks = data.responses["/repos/o/r/commits/" + "a" * 40 + "/check-runs"]["check_runs"]
    run = data.responses["/repos/o/r/actions/runs/10"]
    jobs = data.responses["/repos/o/r/actions/runs/10/attempts/2/jobs"]["jobs"]
    artifact = data.responses["/repos/o/r/actions/runs/10/artifacts"]["artifacts"][0]
    if case == "head":
        pr["head"]["sha"] = "f" * 40
    elif case == "review":
        data.review.review_approved = False
    elif case in {"check_failed", "check_skipped"}:
        checks[0]["conclusion"] = "failure" if case == "check_failed" else "skipped"
    elif case == "check_missing":
        checks.clear()
    elif case == "check_app":
        checks[0]["app"]["id"] = 99
    elif case == "job_skipped":
        jobs[1]["conclusion"] = "skipped"
    elif case == "job_missing":
        jobs.pop()
    elif case == "wrong_event":
        run["event"] = "schedule"
    elif case == "wrong_workflow":
        run["path"] = ".github/workflows/other.yml"
    elif case == "wrong_repository":
        run["repository"]["id"] = 999
    elif case == "stale":
        run["updated_at"] = (data.now - timedelta(days=2)).isoformat()
    elif case == "archive_digest":
        artifact["digest"] = "sha256:" + "0" * 64
    elif case == "prior_artifact":
        artifact["created_at"] = (data.now - timedelta(days=1)).isoformat()
    elif case == "scheduled_scan":
        content = b"on:\n  workflow_dispatch:\n  schedule: []\n"
        definition = data.responses["/repos/o/r/contents/.github/workflows/scan.yml"]
        definition.update(
            content=base64.b64encode(content).decode(),
            size=len(content),
            sha=hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest(),
        )
    elif case == "duplicate_json":
        data.binary = archive_for(b'{"coverage":false,"coverage":true}')
        artifact["digest"] = "sha256:" + hashlib.sha256(data.binary).hexdigest()
    with pytest.raises(CycleBlockedError):
        await observe(data)


@pytest.mark.parametrize("change", ["missing", "duplicate", "bad_owner", "not_boolean"])
async def test_artifact_assertions_cannot_replace_required_machine_predicates(evidence, change):
    document = copy.deepcopy(evidence.document)
    if change == "missing":
        document["records"].pop()
    elif change == "duplicate":
        document["records"][1] = document["records"][0]
    elif change == "bad_owner":
        document["records"][0]["owner"] = "unowned"
    else:
        document["coverage"] = 1
    evidence.binary = archive_for(json.dumps(document).encode())
    evidence.responses["/repos/o/r/actions/runs/10/artifacts"]["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(evidence.binary).hexdigest()
    result = await observe(evidence)
    assert result["mandatory_passed"] is False


def test_machine_json_comparison_preserves_json_types_and_missing_is_not_pass():
    check = Predicate(criterion_id="ac", pointer="/nested/0/success", operation="equals", expected=True)
    assert predicate_passes(check, {"nested": [{"success": True}]})
    assert not predicate_passes(check, {"nested": [{"success": 1}]})
    assert not predicate_passes(check, {"success": True})


@pytest.mark.parametrize("included", [True, False])
async def test_workflow_revision_must_include_all_verified_source_merges(evidence, included):
    document = evidence.spec.model_dump(mode="json")
    document["workflows"][0]["source"] = {"revision": "f" * 40}
    evidence.spec = RepositoryEvaluationSpecification.model_validate(document)
    evidence.responses["/repos/o/r/actions/runs/10"]["head_sha"] = "f" * 40
    evidence.responses["/repos/o/r/compare/" + "b" * 40 + "..." + "f" * 40] = dict(
        status="ahead" if included else "diverged", base_commit=dict(sha="b" * 40), merge_base_commit=dict(sha="b" * 40 if included else "d" * 40)
    )
    if included:
        assert (await observe(evidence))["mandatory_passed"]
    else:
        with pytest.raises(CycleBlockedError, match="workflow_missing_delivered_revision"):
            await observe(evidence)


@pytest.mark.parametrize("conclusion", ["failure", None])
async def test_newer_failed_or_pending_run_during_download_invalidates_older_success(evidence, conclusion):
    newer = {
        **evidence.responses["/repos/o/r/actions/runs/10"],
        "id": 20,
        "run_number": 4,
        "run_attempt": 1,
        "status": "completed" if conclusion else "in_progress",
        "conclusion": conclusion,
    }
    evidence.after_artifact_read = lambda: evidence.responses["/repos/o/r/actions/workflows/scan.yml/runs"]["workflow_runs"].append(newer)
    with pytest.raises(CycleBlockedError, match="workflow_latest_run_changed"):
        await observe(evidence)


async def test_large_flow_checks_each_ancestry_pair_once_with_four_reads_in_flight(evidence):
    provider = RepositoryEvidenceProvider()
    sources = [{**evidence.source, "merge_sha": f"{number:040x}"} for number in range(1, 22)]
    document = evidence.spec.model_dump(mode="json")
    workflow = document["workflows"][0]
    workflow["source"] = {"revision": "f" * 40}
    second = copy.deepcopy(workflow)
    second["criterion_id"] = "another-scan"
    for artifact in second["artifacts"]:
        for predicate in artifact["predicates"]:
            predicate["criterion_id"] += "-second"
    document["workflows"].append(second)
    spec = RepositoryEvaluationSpecification.model_validate(document)
    provider.pull_request = AsyncMock(side_effect=lambda binding, source: source)
    provider.workflow = AsyncMock(return_value={"criteria": [{"passed": True}]})
    observed, active, peak = [], 0, 0

    async def request(binding, method, path, **kwargs):
        nonlocal active, peak
        if path == "/repos/o/r":
            return httpx.Response(200, json={"id": 123})
        assert method == "GET" and path.startswith("/repos/o/r/compare/")
        observed.append(path)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0)
            merge = path.rsplit("/", 1)[1].split("...")[0]
            return httpx.Response(200, json=dict(status="ahead", base_commit=dict(sha=merge), merge_base_commit=dict(sha=merge)))
        finally:
            active -= 1

    provider.request = request
    result = await provider.observe(SimpleNamespace(repo="o/r", provider_repository_id=123), spec, sources)
    assert result["mandatory_passed"] and len(result["pull_requests"]) == 21
    assert len(observed) == len(set(observed)) == 21
    assert peak == 4 and active == 0
    assert provider.workflow.await_count == 2


@pytest.mark.parametrize("changed", [None, "source", "account", "image", "provenance", "coverage", "cleanup", "correlation"])
async def test_bound_scan_artifact_must_match_actual_source_target_images_and_cleanup(evidence, changed):
    document = evidence.spec.model_dump(mode="json")
    document["workflows"][0]["artifacts"][0]["predicates"] = [
        dict(criterion_id="coverage", pointer="/coverage_complete", operation="equals", expected=True)
    ]
    document["producer"] = dict(
        mode="dispatch_once",
        inputs=dict(expected_account_id="123456789012", region="us-east-1"),
        workflow_criterion_id="one-off-scan",
        target=dict(account_id="123456789012", region="us-east-1", resource_kind="repository_scan", resource_id="o/r"),
        receipt_artifact="evidence-{run_attempt}",
        receipt_path="evidence.json",
        images={"controller": dict(digest="sha256:" + "d" * 64, provenance_sha256="e" * 64)},
    )
    spec = RepositoryEvaluationSpecification.model_validate(document)
    receipt = dict(
        evidence_schema="repository-scan-receipt/v1",
        source_revision="b" * 40,
        correlation="c" * 64,
        target=spec.producer.target.model_dump(mode="json"),
        images={name: image.model_dump(mode="json") for name, image in spec.producer.images.items()},
        coverage_complete=True,
        cleanup_complete=True,
    )
    if changed == "source":
        receipt["source_revision"] = "0" * 40
    elif changed == "account":
        receipt["target"]["account_id"] = "999999999999"
    elif changed in {"image", "provenance"}:
        receipt["images"]["controller"]["digest" if changed == "image" else "provenance_sha256"] = (
            "sha256:" if changed == "image" else ""
        ) + "0" * 64
    elif changed in {"coverage", "cleanup"}:
        receipt[changed + "_complete"] = False
    elif changed == "correlation":
        receipt["correlation"] = "0" * 64
    evidence.binary = archive_for(json.dumps(receipt).encode())
    evidence.responses["/repos/o/r/actions/runs/10/artifacts"]["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(evidence.binary).hexdigest()
    # Dispatch may use newer main while the authenticated context pins checkout
    # to the exact accepted scan revision; no tree-equivalence inference is used.
    evidence.responses["/repos/o/r/actions/runs/10"]["head_sha"] = "f" * 40
    bound = SimpleNamespace(
        run_id=10, run_attempt=2, context=SimpleNamespace(workflow_revision="f" * 40, source_revision="b" * 40, correlation="c" * 64)
    )

    def transport(request):
        if request.url.path == "/repos/o/r/actions/artifacts/13/zip":
            return httpx.Response(200, content=evidence.binary)
        return httpx.Response(200, json=evidence.responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(client=client, clock=lambda: evidence.now)
        provider.token = AsyncMock(return_value="test-read-token")
        observation = provider.workflow(
            SimpleNamespace(repo="o/r", provider_repository_id=123),
            spec.workflows[0],
            revisions={},
            max_age_seconds=spec.max_age_seconds,
            bound_run=bound,
            producer=spec.producer,
        )
        if changed:
            with pytest.raises(CycleBlockedError, match="scan_scope_coverage_images_or_cleanup_changed"):
                await observation
        else:
            result = await observation
            assert result["source_revision"] == "b" * 40 and result["run_id"] == 10


async def test_shared_app_review_reuses_scoped_read_token_without_minting_broad_token(monkeypatch):
    from src.orchestration.merge_evidence import GitHubEvidenceSource

    app = SimpleNamespace(get_installation_token=AsyncMock(side_effect=AssertionError("no broad token")), aclose=AsyncMock())
    monkeypatch.setattr("src.admin.connections.github_client.GitHubAppClient", lambda *args: app)
    monkeypatch.setattr("src.knowledge.github_app_service.resolve_tenant_app_credentials", AsyncMock(return_value=(42, "unused")))

    def transport(request):
        assert request.headers["authorization"] == "Bearer actual-scoped-read-token"
        if request.url.path == "/graphql":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "repository": {
                            "databaseId": 123,
                            "pullRequest": {
                                "id": "PR_2",
                                "headRefOid": "a" * 40,
                                "author": {"login": "shared-app[bot]"},
                                "merged": True,
                                "reviews": {"nodes": [], "pageInfo": {"hasPreviousPage": False}},
                            },
                        }
                    }
                },
            )
        assert request.url.path == "/repos/o/r/issues/2/comments"
        return httpx.Response(
            200,
            json=[
                {
                    "id": 9,
                    "updated_at": "2026-09-21T00:00:00Z",
                    "performed_via_github_app": {"id": 42},
                    "user": {"type": "Bot"},
                    "body": "## agent-codex-reviewer — APPROVE\n\n**Reviewed head:** `" + "a" * 40 + "`\n**Blockers:** 0\n**Engine:** reviewer-run\n",
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    monkeypatch.setattr("src.orchestration.merge_evidence.httpx.AsyncClient", lambda **kwargs: client)
    result = await GitHubEvidenceSource().bound_pull_request(
        org_id="org", installation_id=42, repo="o/r", pr_number=2, read_token="actual-scoped-read-token"
    )
    assert result.review_approved and result.head_sha == "a" * 40
    app.get_installation_token.assert_not_awaited()


@pytest.mark.parametrize("change", [None, "schedule", "run_name", "cancellation", "unaccepted_input", "correlation"])
async def test_scan_preflight_refuses_workflows_without_the_recoverable_one_off_protocol(evidence, change):
    from src.orchestration.deployment_workflow_provider import WorkflowDefinition
    from src.orchestration.repository_producer import RepositoryScanProvider

    document = evidence.spec.model_dump(mode="json")
    document["producer"] = dict(
        mode="dispatch_once",
        workflow_criterion_id="one-off-scan",
        inputs=dict(expected_account_id="123456789012", region="us-east-1"),
        target=dict(account_id="123456789012", region="us-east-1", resource_kind="repository_scan", resource_id="o/r"),
        receipt_artifact="evidence-{run_attempt}",
        receipt_path="evidence.json",
        images={"controller": dict(digest="sha256:" + "d" * 64, provenance_sha256="e" * 64)},
    )
    spec = RepositoryEvaluationSpecification.model_validate(document)
    workflow = {
        "on": {"workflow_dispatch": {}},
        "concurrency": {"group": "repository-scan", "cancel-in-progress": False},
        "run-name": "${{ format('ADP deployment {0}', inputs.adp_correlation) }}",
    }
    defaults = dict(adp_correlation="", adp_source_revision="", adp_definition_revision="", **spec.producer.inputs)
    if change == "schedule":
        workflow["on"]["schedule"] = []
    elif change == "run_name":
        workflow.pop("run-name")
    elif change == "cancellation":
        workflow["concurrency"]["cancel-in-progress"] = True
    elif change == "unaccepted_input":
        defaults["target"] = "somewhere-unreviewed"
    elif change == "correlation":
        defaults.pop("adp_correlation")
    import yaml

    evidence_provider = SimpleNamespace(
        verify_sources=AsyncMock(return_value=([], {})), definition_blob=AsyncMock(return_value=("d" * 40, yaml.safe_dump(workflow).encode()))
    )
    provider = RepositoryScanProvider(evidence=evidence_provider)
    provider.definition = AsyncMock(return_value=WorkflowDefinition("b" * 40, "b" * 40, "d" * 40, defaults, True, "main", "b" * 40))
    if change:
        with pytest.raises(CycleBlockedError):
            await provider.preflight(SimpleNamespace(repo="o/r"), spec, [evidence.source])
    else:
        result = await provider.preflight(SimpleNamespace(repo="o/r"), spec, [evidence.source])
        assert result["source_revision"] == "b" * 40


@pytest.mark.parametrize("case", ["absent", "found", "missing_context", "duplicate", "wrong_identity", "overflow"])
async def test_scan_recovery_filters_workflow_and_dispatch_head_before_validating_context(evidence, case):
    from src.orchestration.deployment_manifest import WorkflowRef
    from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowDefinition
    from src.orchestration.repository_producer import RepositoryScanProvider
    from src.orchestration.repository_producer_contract import ScanTarget

    correlation = "c" * 64
    target = ScanTarget(account_id="123456789012", region="us-east-1", resource_id="o/r")
    definition = WorkflowDefinition("f" * 40, "b" * 40, "d" * 40, {}, True, "main", "f" * 40)
    run = dict(evidence.responses["/repos/o/r/actions/runs/10"], head_sha="f" * 40, display_title="ADP deployment " + correlation)
    if case == "wrong_identity":
        run["head_sha"] = "b" * 40
    requested = []

    def transport(request):
        requested.append(request.url.path)
        if request.url.path == "/repos/o/r/actions/workflows/scan.yml/runs":
            assert request.url.params["event"] == "workflow_dispatch"
            assert request.url.params["head_sha"] == "f" * 40
            assert request.url.params["per_page"] == "100"
            rows = [] if case == "absent" else [run, dict(run, id=11)] if case == "duplicate" else [run]
            if case == "overflow":
                rows = [dict(run, id=i, display_title="unrelated") for i in range(100)]
            return httpx.Response(200, json=dict(workflow_runs=rows))
        # Never enumerate the repository's many thousands of unrelated runs.
        assert request.url.path == "/repos/o/r/actions/runs/10", request.url.path
        return httpx.Response(200, json=run)

    context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=10,
        run_attempt=2,
        workflow_path=".github/workflows/scan.yml",
        workflow_revision="f" * 40,
        source_revision="b" * 40,
        inputs={},
        correlation=correlation,
        **target.model_dump(),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryScanProvider(client=client)
        provider.token = AsyncMock(return_value="read-token")
        provider.context = AsyncMock(return_value=None if case == "missing_context" else (context, 20, "d" * 64))
        kwargs = dict(
            workflow=WorkflowRef(path=".github/workflows/scan.yml", definition_revision="f" * 40),
            definition=definition,
            target=target,
            source_revision="b" * 40,
            inputs={},
            correlation=correlation,
        )
        binding = SimpleNamespace(repo="o/r", provider_repository_id=123)
        if case in {"duplicate", "wrong_identity", "overflow"}:
            with pytest.raises(CycleBlockedError):
                await provider.observe(binding, **kwargs)
            provider.context.assert_not_awaited()
        else:
            observed, incomplete = await provider.observe(binding, **kwargs)
            assert incomplete == (case == "missing_context")
            assert (observed is not None) == (case == "found")
            if observed:
                assert observed.run_id == 10 and observed.context.source_revision == "b" * 40
        assert len(requested) == (10 if case == "overflow" else 2 if case in {"found", "missing_context"} else 1)
