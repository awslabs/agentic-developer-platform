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


def archive_for(data, path="evidence.json"):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(path, data)
    return stream.getvalue()


@pytest.mark.parametrize(
    "fault", [None, "wrong-attempt", "bad-digest", "wrong-file", "duplicate-json-key", "wrong-run-artifact", "artifact-before-run"]
)
async def test_deployed_probe_reads_only_bound_run_artifact(fault):
    provider = RepositoryEvidenceProvider()
    binding = SimpleNamespace(repo="fixture/harness", provider_repository_id=303)
    run = SimpleNamespace(run_id=812, run_attempt=2, started_at="2026-10-20T12:00:00+00:00")
    probe = SimpleNamespace(artifact_name="integration-evidence", artifact_path="result.json")
    name = "integration-evidence-812-2" if fault != "wrong-attempt" else "integration-evidence-812-1"
    content = b'{"verdict":"pass"}' if fault != "duplicate-json-key" else b'{"verdict":"pass","verdict":"fail"}'
    archive = archive_for(content, path="other.json" if fault == "wrong-file" else "result.json")
    artifact = {
        "id": 83,
        "workflow_run": {"id": 811 if fault == "wrong-run-artifact" else 812},
        "created_at": "2026-10-20T11:59:00Z" if fault == "artifact-before-run" else "2026-10-20T12:01:00Z",
        "name": name,
        "expired": False,
        "size_in_bytes": len(archive),
        "digest": "sha256:" + ("0" * 64 if fault == "bad-digest" else hashlib.sha256(archive).hexdigest()),
    }
    provider.pages = AsyncMock(return_value=[artifact])
    provider.request = AsyncMock(return_value=SimpleNamespace(content=archive))
    if fault:
        reason = "artifact_digest" if fault == "bad-digest" else "duplicate_json_key" if fault == "duplicate-json-key" else "probe_"
        with pytest.raises(CycleBlockedError, match=reason):
            await provider.multi_probe_receipt(binding, run, probe)
    else:
        assert await provider.multi_probe_receipt(binding, run, probe) == {"verdict": "pass"}
    provider.pages.assert_awaited_once()
    assert provider.pages.await_args.args[1] == "/repos/fixture/harness/actions/runs/812/artifacts"


async def test_failed_required_check_in_second_repository_cannot_use_first_repository_success():
    revision = "a" * 40
    calls = []

    def read(request):
        repo = request.url.path.split("/")[3]
        calls.append((repo, request.headers["Authorization"]))
        assert request.url.path == f"/repos/fixture/{repo}/commits/{revision}/check-runs"
        assert request.headers["Authorization"] == f"Bearer fixture-{repo}"
        return httpx.Response(
            200,
            json={
                "check_runs": [
                    {
                        "id": 17,
                        "name": "ci",
                        "app": {"id": 7},
                        "head_sha": revision,
                        "status": "completed",
                        "conclusion": "success" if repo == "source-a" else "failure",
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(read)) as client:
        provider = RepositoryEvidenceProvider(client=client)

        async def token(binding, *, write=False):
            assert not write
            return f"fixture-{binding.repo.split('/')[-1]}"

        provider.token = token
        source = {"head_sha": revision, "required_checks": [{"name": "ci", "app_id": 7}]}
        first = SimpleNamespace(org_id="org-fixture", repo="fixture/source-a", provider_repository_id=101, installation_id=901)
        second = SimpleNamespace(org_id="org-fixture", repo="fixture/source-b", provider_repository_id=202, installation_id=902)
        assert (await provider.required_checks(first, source))[0]["head_sha"] == revision
        with pytest.raises(CycleBlockedError, match="repository_evaluation_required_check_not_successful"):
            await provider.required_checks(second, source)
    assert calls == [("source-a", "Bearer fixture-source-a"), ("source-b", "Bearer fixture-source-b")]


@pytest.mark.parametrize(
    ("substitution", "reason"),
    [
        (None, None),
        ("source_repository", "source_repository_identity"),
        ("pull_request", "pull_request_identity_changed"),
        ("check_app", "required_check_missing_or_ambiguous"),
        ("workflow_repository", "required_check_workflow_identity"),
        ("workflow_path", "required_check_missing_or_ambiguous"),
        ("workflow_attempt", "required_check_job_identity_or_result"),
        ("workflow_pins_changed_at_head", "required_check_workflow_run_definition_changed"),
        ("workflow_pins_compatible_separate_revisions", None),
    ],
)
async def test_multi_source_rejects_equal_display_values_from_other_repository(substitution, reason):
    from src.orchestration.evaluation_contract import multi_repository_models

    revision, merge = "a" * 40, "b" * 40
    workflow_path = ".github/workflows/ci.yml"
    pins = (
        {"workflow_source_revision": "c" * 40, "workflow_definition_revision": "d" * 40}
        if substitution and substitution.startswith("workflow_pins_")
        else {}
    )
    declared = multi_repository_models().DeclaredInput.model_validate(
        dict(
            address="flow/epic/wave/source-b",
            repository_id=202,
            evidence_kind="merge",
            required_checks=[dict(name="ci", app_id=7, workflow_path=workflow_path, **pins)],
        )
    )
    sources = {
        repository: dict(
            repository_id=repository_id,
            pr_number=17,
            provider_pr_node_id=f"PR_{repository}",
            head_sha=revision,
            merge_sha=merge,
            required_checks=[],
        )
        for repository, repository_id in (("source-a", 101), ("source-b", 202))
    }
    selected = []

    def read(request):
        parts = request.url.path.split("/")
        repository = parts[3]
        assert repository in sources and request.headers["Authorization"] == f"Bearer scoped-{repository}"
        selected.append((repository, request.url.path))
        prefix = f"/repos/fixture/{repository}"
        repository_id = sources[repository]["repository_id"]
        if request.url.path == prefix + "/pulls/17":
            return httpx.Response(
                200,
                json=dict(
                    number=17,
                    node_id=f"PR_{repository}",
                    merged=True,
                    merged_at="2026-10-21T00:00:00Z",
                    merge_commit_sha=merge,
                    base={"repo": {"id": repository_id}},
                    head={"sha": revision, "repo": {"id": repository_id}},
                ),
            )
        if request.url.path == prefix + "/contents/" + workflow_path:
            revision_ref = request.url.params["ref"]
            assert revision_ref in {revision, "c" * 40, "d" * 40}
            body = (
                b"jobs: {injected: {}}\n"
                if substitution == "workflow_pins_changed_at_head" and repository == "source-b" and revision_ref == revision
                else b"jobs: {ci: {}}\n"
            )
            blob = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
            return httpx.Response(
                200, json={"type": "file", "encoding": "base64", "size": len(body), "content": base64.b64encode(body).decode(), "sha": blob}
            )
        if request.url.path == prefix + f"/commits/{revision}/check-runs":
            return httpx.Response(
                200,
                json={
                    "check_runs": [
                        dict(
                            id=17,
                            name="ci",
                            app={"id": 8 if substitution == "check_app" and repository == "source-b" else 7},
                            check_suite={"id": 31},
                            head_sha=revision,
                            status="completed",
                            conclusion="success",
                        )
                    ]
                },
            )
        run = dict(
            id=30,
            workflow_id=40,
            check_suite_id=31,
            run_attempt=1,
            head_sha=revision,
            repository={"id": 101 if substitution == "workflow_repository" and repository == "source-b" else repository_id},
            head_repository={"id": repository_id},
            path=".github/workflows/other.yml" if substitution == "workflow_path" and repository == "source-b" else workflow_path,
            status="completed",
            conclusion="success",
        )
        if request.url.path == prefix + "/actions/runs":
            return httpx.Response(200, json={"workflow_runs": [run]})
        if request.url.path == prefix + "/actions/runs/30":
            return httpx.Response(200, json=run)
        if request.url.path == prefix + "/actions/runs/30/attempts/1/jobs":
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        dict(
                            id=50,
                            name="ci",
                            run_id=30,
                            run_attempt=2 if substitution == "workflow_attempt" and repository == "source-b" else 1,
                            head_sha=revision,
                            status="completed",
                            conclusion="success",
                            check_run_url=f"https://api.github.com{prefix}/check-runs/17",
                        )
                    ]
                },
            )
        pytest.fail(f"unexpected provider read: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(read)) as client:
        provider = RepositoryEvidenceProvider(client=client)
        provider.token = AsyncMock(side_effect=lambda binding: f"scoped-{binding.repo.split('/')[-1]}")
        provider.reviews.bound_pull_request = AsyncMock(return_value=SimpleNamespace(head_sha=revision, review_approved=True))
        first = SimpleNamespace(org_id="org-fixture", repo="fixture/source-a", provider_repository_id=101, installation_id=901)
        second = SimpleNamespace(org_id="org-fixture", repo="fixture/source-b", provider_repository_id=202, installation_id=902)
        own = declared.model_copy(update={"repository_id": 101})
        receipts, observations = await provider.verified_multi_source(first, sources["source-a"], own)
        assert receipts[0]["head_sha"] == revision and observations[0]["repository_id"] == 101
        candidate = dict(sources["source-b"])
        if substitution in (None, "workflow_pins_compatible_separate_revisions"):
            second_receipts, second_observations = await provider.verified_multi_source(second, candidate, declared)
            assert second_receipts[0]["app_id"] == 7 and second_observations[0]["repository_id"] == 202
            assert second_observations[0]["pr_node_id"] == "PR_source-b"
            if substitution == "workflow_pins_compatible_separate_revisions":
                definition = b"jobs: {ci: {}}\n"
                blob = hashlib.sha1(b"blob " + str(len(definition)).encode() + b"\0" + definition).hexdigest()
                assert second_observations[0]["workflow"]["workflow_blob_sha"] == blob
                assert second_receipts[0]["workflow_source_revision"] == "c" * 40
                assert second_receipts[0]["workflow_definition_revision"] == "d" * 40
            assert {repo for repo, _ in selected} == {"source-a", "source-b"}
            return
        if substitution == "source_repository":
            candidate["repository_id"] = 101
        if substitution == "pull_request":
            candidate["provider_pr_node_id"] = "PR_source-a"
        with pytest.raises(CycleBlockedError, match="repository_evaluation_" + reason):
            await provider.verified_multi_source(second, candidate, declared)
        assert selected and all(repo == "source-a" or repo == "source-b" for repo, _ in selected)
        assert (
            all(repo == "source-a" for repo, _ in selected)
            if substitution == "source_repository"
            else any(repo == "source-b" for repo, _ in selected)
        )


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
        if request.url.path.startswith("/repos/o/r/contents/") and request.url.params.get("ref") in getattr(data, "definitions", {}):
            return httpx.Response(200, json=data.definitions[request.url.params["ref"]])
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


def test_existing_evaluator_spec_hash_is_unchanged_and_explicit_binding_changes_it(evidence):
    from src.orchestration.evaluation_acceptance import digest
    from src.orchestration.repository_evaluation_contract import canonical, native_specification

    legacy = evidence.spec.model_dump(mode="json")
    artifact = legacy["workflows"][0]["artifacts"][0]
    assert "bind_workflow_provenance" not in artifact
    assert digest(legacy) == "b50e22263fc9994fe641f89790548f43a0337aecb463fadb8969d5aebb87c2f4"
    assert canonical(native_specification(legacy).model_dump(mode="json")) == canonical(legacy)
    artifact["bind_workflow_provenance"] = False
    assert native_specification(legacy).model_dump(mode="json") == evidence.spec.model_dump(mode="json")
    artifact["bind_workflow_provenance"] = True
    bound = native_specification(legacy)
    assert bound.workflows[0].artifacts[0].bind_workflow_provenance is True
    assert bound.model_dump(mode="json")["workflows"][0]["artifacts"][0]["bind_workflow_provenance"] is True
    assert digest(bound.model_dump(mode="json")) != digest(evidence.spec.model_dump(mode="json"))


def atlassian_observer_case(evidence):
    candidate = evidence.spec.model_dump()
    workflow = candidate["workflows"][0]
    workflow["definition"] = {"revision": "f" * 40}
    artifact = workflow["artifacts"][0]
    artifact.update(name="atlassian-stage-{run_id}-{run_attempt}", path="atlassian-stage-summary.json", bind_workflow_provenance=True)
    evidence.spec = RepositoryEvaluationSpecification.model_validate(candidate)
    evidence.document.update(source_revision="b" * 40, definition_revision="f" * 40, gateway_revision="b" * 40, run_id="10", run_attempt="2")
    return evidence


def atlassian_observer_archive(evidence):
    evidence.binary = archive_for(json.dumps(evidence.document).encode(), path="atlassian-stage-summary.json")
    artifact = evidence.responses["/repos/o/r/actions/runs/10/artifacts"]["artifacts"][0]
    artifact.update(name="atlassian-stage-10-2", size_in_bytes=len(evidence.binary), digest="sha256:" + hashlib.sha256(evidence.binary).hexdigest())


async def test_atlassian_provenance_matches_observer_selected_release_and_attempt(evidence):
    atlassian_observer_archive(atlassian_observer_case(evidence))
    receipt = await observe(evidence)
    assert receipt["mandatory_passed"] is True
    workflow = receipt["workflows"][0]
    assert workflow["source_revision"] == "b" * 40
    assert workflow["definition_revision"] == "f" * 40
    assert (workflow["run_id"], workflow["run_attempt"]) == (10, 2)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_revision", "c" * 40),
        ("definition_revision", "c" * 40),
        ("gateway_revision", "c" * 40),
        ("run_id", "11"),
        ("run_attempt", "3"),
        ("run_id", 10),
        ("run_attempt", 2),
    ],
)
async def test_atlassian_provenance_rejects_mismatched_selected_fact(evidence, field, value):
    atlassian_observer_case(evidence).document[field] = value
    atlassian_observer_archive(evidence)
    with pytest.raises(CycleBlockedError, match="artifact_workflow_provenance_mismatch"):
        await observe(evidence)


@pytest.mark.parametrize("field", ["source_revision", "definition_revision", "gateway_revision", "run_id", "run_attempt"])
async def test_atlassian_provenance_rejects_missing_selected_fact(evidence, field):
    atlassian_observer_case(evidence).document.pop(field)
    atlassian_observer_archive(evidence)
    with pytest.raises(CycleBlockedError, match="artifact_workflow_provenance_mismatch"):
        await observe(evidence)


@pytest.mark.parametrize("case", ["review", "check"])
async def test_owner_accepted_merge_still_requires_provider_review_and_checks(evidence, case):
    evidence.source["accepted_merge_decision_id"] = "owner-acceptance"
    if case == "review":
        evidence.review.review_approved = False
    else:
        evidence.responses["/repos/o/r/commits/" + "a" * 40 + "/check-runs"]["check_runs"][0]["conclusion"] = "failure"
    with pytest.raises(CycleBlockedError, match="review_missing_or_stale|required_check_not_successful"):
        await observe(evidence)


@pytest.mark.parametrize("state", ["changes_requested", "stale"])
async def test_owner_acceptance_settles_only_older_reviews_and_preserves_provenance(evidence, state):
    evidence.source.update(accepted_merge_decision_id="owner-acceptance", accepted_merge_at=(evidence.now - timedelta(hours=1)).isoformat())
    evidence.review.review_approved = False
    evidence.review.review_state = state
    evidence.review.review_updated_at = (evidence.now - timedelta(hours=2)).isoformat()
    receipt = await observe(evidence)
    pull = receipt["pull_requests"][0]
    assert pull["review_acceptance"] == "owner_acceptance"
    assert pull["provider_review_state"] == state
    assert pull["accepted_merge_decision_id"] == "owner-acceptance"
    assert len(pull["checks"]) == 1


async def test_merged_predecessor_passes_when_only_the_engine_snapshot_requests_changes(evidence):
    # The reviewer App approved the exact merged head, then recorded its CI snapshot
    # as a change request GitHub will not dismiss after merge (#7639). No owner
    # acceptance exists; the live check re-verification below still has to pass.
    evidence.review = SimpleNamespace(
        review_approved=False,
        head_sha="a" * 40,
        merged=True,
        review_state="changes_requested",
        foreign_changes_requested=False,
        own_approval_on_head=True,
    )
    receipt = await observe(evidence)
    pull = receipt["pull_requests"][0]
    assert pull["review_acceptance"] == "own_snapshot_withdrawn"
    assert pull["provider_review_state"] == "changes_requested"
    assert len(pull["checks"]) == 1


def test_receipt_schema_records_the_engine_snapshot_withdrawal_provenance():
    # The receipt is what the engine persists; a provenance value the provider can
    # emit but the receipt refuses would fail every such evaluation at write time.
    from src.orchestration.repository_evaluation_contract import PullReceipt

    pull = dict(
        pr_number=2,
        head_sha="a" * 40,
        merge_sha="b" * 40,
        provider_pr_node_id="PR_2",
        merged_at=datetime.now(UTC).isoformat(),
        checks=[dict(name="Tests", app_id=15368, check_run_id=7, head_sha="a" * 40)],
        provider_review_state="changes_requested",
        review_acceptance="own_snapshot_withdrawn",
    )
    assert PullReceipt.model_validate(pull).review_acceptance == "own_snapshot_withdrawn"
    with pytest.raises(ValueError):
        PullReceipt.model_validate({**pull, "review_acceptance": "unreviewed"})


@pytest.mark.parametrize("case", ["foreign", "unknown_origin", "no_own_approval", "not_merged", "stale", "checks"])
async def test_engine_snapshot_withdrawal_never_hides_other_reviewers_or_red_checks(evidence, case):
    evidence.review = SimpleNamespace(
        review_approved=False,
        head_sha="a" * 40,
        merged=True,
        review_state="changes_requested",
        foreign_changes_requested=False,
        own_approval_on_head=True,
    )
    if case == "foreign":
        evidence.review.foreign_changes_requested = True
    elif case == "unknown_origin":
        evidence.review.foreign_changes_requested = None
    elif case == "no_own_approval":
        evidence.review.own_approval_on_head = False
    elif case == "not_merged":
        evidence.review.merged = False
    elif case == "stale":
        evidence.review.review_state = "stale"
    else:
        evidence.responses["/repos/o/r/commits/" + "a" * 40 + "/check-runs"]["check_runs"][0]["conclusion"] = "failure"
    with pytest.raises(CycleBlockedError, match="review_missing_or_stale|required_check_not_successful"):
        await observe(evidence)


@pytest.mark.parametrize(
    "case", ["new_review", "unverified", "required", "missing", "missing_time", "future_acceptance", "before_merge", "head", "checks"]
)
async def test_owner_acceptance_cannot_hide_new_or_unverifiable_evidence(evidence, case):
    evidence.source.update(accepted_merge_decision_id="owner-acceptance", accepted_merge_at=(evidence.now - timedelta(hours=1)).isoformat())
    evidence.review.review_approved = False
    evidence.review.review_state = "changes_requested"
    evidence.review.review_updated_at = (evidence.now - timedelta(hours=2)).isoformat()
    if case == "new_review":
        evidence.review.review_updated_at = evidence.now.isoformat()
    elif case in {"unverified", "required", "missing"}:
        evidence.review.review_state = case
    elif case == "missing_time":
        evidence.review.review_updated_at = None
    elif case == "future_acceptance":
        evidence.source["accepted_merge_at"] = (evidence.now + timedelta(hours=1)).isoformat()
    elif case == "before_merge":
        evidence.source["accepted_merge_at"] = (evidence.now - timedelta(days=2)).isoformat()
    elif case == "head":
        evidence.review.head_sha = "f" * 40
    else:
        evidence.responses["/repos/o/r/commits/" + "a" * 40 + "/check-runs"]["check_runs"][0]["conclusion"] = "failure"
    with pytest.raises(CycleBlockedError, match="review_missing_or_stale|required_check_not_successful"):
        await observe(evidence)


async def test_workflow_definition_requires_matching_content_even_when_blob_ids_match(evidence):
    provider = RepositoryEvidenceProvider()
    provider.definition_blob = AsyncMock(
        side_effect=[
            ("d" * 40, b"on:\n  workflow_dispatch:\n"),
            ("d" * 40, b"on:\n  schedule: []\n"),
        ]
    )

    with pytest.raises(CycleBlockedError, match="repository_evaluation_workflow_definition_changed"):
        await provider.workflow(
            SimpleNamespace(repo="o/r", provider_repository_id=123),
            evidence.spec.workflows[0],
            revisions={},
            max_age_seconds=evidence.spec.max_age_seconds,
        )


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
        images={
            "controller": {
                "grype": dict(digest="sha256:" + "d" * 64, provenance_sha256="e" * 64),
                "syft": dict(digest="sha256:" + "f" * 64, provenance_sha256="1" * 64),
            }
        },
    )
    spec = RepositoryEvaluationSpecification.model_validate(document)
    receipt = dict(
        evidence_schema="repository-scan-receipt/v2",
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
        receipt["images"]["controller"]["grype"]["digest" if changed == "image" else "provenance_sha256"] = (
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


@pytest.mark.parametrize("formal", [None, "older", "newer", "missing_time", "truncated"])
async def test_shared_app_review_reuses_scoped_read_token_without_minting_broad_token(monkeypatch, formal):
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
                                "reviews": {
                                    "nodes": (
                                        [
                                            {
                                                "author": {"login": "independent-reviewer"},
                                                "state": "CHANGES_REQUESTED",
                                                "commit": {"oid": "a" * 40},
                                                "submittedAt": None
                                                if formal == "missing_time"
                                                else "2026-09-22T00:00:00Z"
                                                if formal == "newer"
                                                else "2026-09-20T00:00:00Z",
                                            }
                                        ]
                                        if formal
                                        else []
                                    ),
                                    "pageInfo": {"hasPreviousPage": formal == "truncated"},
                                },
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
    assert result.head_sha == "a" * 40
    assert result.review_approved is (formal is None)
    expected_time = None if formal in {"missing_time", "truncated"} else "2026-09-22T00:00:00Z" if formal == "newer" else "2026-09-21T00:00:00Z"
    assert result.review_updated_at == expected_time
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
        images={
            "controller": {
                "grype": dict(digest="sha256:" + "d" * 64, provenance_sha256="e" * 64),
                "syft": dict(digest="sha256:" + "f" * 64, provenance_sha256="1" * 64),
            }
        },
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


@pytest.mark.parametrize("event", ["workflow_dispatch", "pull_request", "workflow_call", "schedule"])
async def test_bound_knowledge_result_uses_dispatch_event_not_exclusive_workflow_triggers(evidence, event):
    from src.orchestration.workflow_evaluation_contract import WorkflowProducer

    path = ".github/workflows/eval-cli-uplift.yml"
    workflow = evidence.spec.workflows[0].model_copy(update={"path": path})
    content = b"on:\n  workflow_dispatch:\n  workflow_call:\n  pull_request:\njobs: {}\n"
    blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
    evidence.responses["/repos/o/r/contents/" + path] = dict(
        type="file", path=path, size=len(content), encoding="base64", content=base64.b64encode(content).decode(), sha=blob
    )
    evidence.responses["/repos/o/r/actions/runs/10"].update(event=event, path=path)
    producer = WorkflowProducer(
        mode="dispatch_once",
        workflow_criterion_id="one-off-scan",
        target=dict(account_id="123456789012", region="us-east-1", resource_kind="cli-evaluation", resource_id="dev"),
        inputs=dict(
            environment="dev", expected_revision="b" * 40, suites="knowledge", mode="start", inject_fault="none", fixtures_json="{}", evaluation_id=""
        ),
        receipt_artifact="result",
        receipt_path="report.json",
    )
    bound = SimpleNamespace(run_id=10, run_attempt=2, context=SimpleNamespace(workflow_revision="b" * 40, source_revision="b" * 40))

    def transport(request):
        if request.url.path == "/repos/o/r/actions/artifacts/13/zip":
            return httpx.Response(200, content=evidence.binary)
        return httpx.Response(200, json=evidence.responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(client=client, clock=lambda: evidence.now)
        provider.token = AsyncMock(return_value="test-read-token")
        call = provider.workflow(
            SimpleNamespace(repo="o/r", provider_repository_id=123),
            workflow,
            revisions={},
            max_age_seconds=evidence.spec.max_age_seconds,
            bound_run=bound,
            producer=producer,
        )
        if event == "workflow_dispatch":
            result = await call
            assert result["run_id"] == 10 and result["event"] == "workflow_dispatch"
        else:
            with pytest.raises(CycleBlockedError, match="workflow_run_not_successful_or_changed"):
                await call


@pytest.mark.parametrize("failure", [None, "missing", "digest", "predicate", "unbound"])
async def test_s3_scan_archive_preserves_provider_and_predicate_checks(evidence, failure):
    from src.orchestration.repository_evaluation_contract import WorkflowReceipt

    path = ".github/workflows/security-scan.yml"
    definition = b"on:\n  workflow_dispatch:\nenv:\n  ADP_REPOSITORY_EVIDENCE_STORE: s3-oidc-v1\njobs: {}\n"
    evidence.responses["/repos/o/r/contents/" + path] = dict(
        type="file",
        encoding="base64",
        size=len(definition),
        content=base64.b64encode(definition).decode(),
        sha=hashlib.sha1(b"blob " + str(len(definition)).encode() + b"\0" + definition).hexdigest(),
    )
    run = evidence.responses["/repos/o/r/actions/runs/10"]
    run["path"] = path
    evidence.responses["/repos/o/r/actions/workflows/security-scan.yml/runs"] = {"workflow_runs": [run]}
    workflow = evidence.spec.workflows[0].model_copy(
        update={
            "path": path,
            "artifacts": [evidence.spec.workflows[0].artifacts[0].model_copy(update={"name": "security-reconciliation-{run_attempt}"})],
        }
    )
    if failure == "predicate":
        evidence.document["coverage"] = False
        evidence.binary = archive_for(json.dumps(evidence.document).encode())
    calls = []

    def read_archive(**kw):
        calls.append(kw)
        if failure == "missing":
            return None
        digest = "0" * 64 if failure == "digest" else hashlib.sha256(evidence.binary).hexdigest()
        return evidence.binary, digest, "s3://evidence/descriptor.json?versionId=v1"

    def transport(request):
        assert "/artifacts" not in request.url.path  # Quota-independent, including discovery.
        return httpx.Response(200, json=evidence.responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(client=client, clock=lambda: evidence.now, evidence_store=SimpleNamespace(read_archive=read_archive))
        provider.token = AsyncMock(return_value="read-only")
        producer = SimpleNamespace(target=SimpleNamespace(account_id="123456789012", region="us-east-1"), inputs={})
        bound = SimpleNamespace(run_id=10, run_attempt=2, context=SimpleNamespace(workflow_revision="b" * 40, source_revision="b" * 40))
        arguments = dict(revisions={}, max_age_seconds=3600, producer=producer, bound_run=None if failure == "unbound" else bound)
        binding = SimpleNamespace(repo="o/r", provider_repository_id=123)
        if failure in {"missing", "digest", "unbound"}:
            with pytest.raises(CycleBlockedError):
                await provider.workflow(binding, workflow, **arguments)
        else:
            result = await provider.workflow(binding, workflow, **arguments)
            WorkflowReceipt.model_validate(result)
            assert result["artifacts"][0]["artifact_id"] is None
            assert result["artifacts"][0]["artifact_ref"].endswith("versionId=v1")
            assert result["criteria"][0]["passed"] is (failure != "predicate")
            assert calls[0]["source_revision"] == "b" * 40
            assert calls[0]["attempt"] == 2 and calls[0]["account"] == "123456789012"


def _deployment_source(**overrides):
    return {
        **dict(
            address="flow/epic/live-2/door",
            node_id="node-door",
            attempt=4,
            accepted_plan_version=52,
            execution_id="exec-door",
            deployment_receipt_key="operations-runtime:38851653127:1",
            workflow_operation_keys=["operations-workflow:38851653127:1"],
            components=["knowledge-door"],
            head_sha="d" * 40,
            merge_sha="d" * 40,
            required_checks=[dict(name="Lint", app_id=15368)],
        ),
        **overrides,
    }


async def _observe_deployment(source, responses, spec=None):
    def transport(request):
        assert request.method == "GET" and request.url.host == "api.github.com"
        assert request.url.path in responses, request.url.path
        return httpx.Response(200, json=responses[request.url.path])

    spec = spec or RepositoryEvaluationSpecification.model_validate(
        dict(
            evidence_schema="repository-evaluation/v1",
            runner=dict(adapter="engine-repository-evidence-v1", repository="o/r", repository_id=123, harness_sha256="c" * 64),
            predecessors=[dict(address=source["address"], required_checks=source["required_checks"])],
        )
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(
            client=client, reviews=SimpleNamespace(bound_pull_request=AsyncMock(side_effect=AssertionError("no PR")))
        )
        provider.token = AsyncMock(return_value="test-read-only-token")
        return await provider.observe(SimpleNamespace(org_id="org", installation_id=42, repo="o/r", provider_repository_id=123), spec, [source])


def _deployment_responses(source, *, check_conclusion="success", check_name="Lint", commit_sha=None):
    return {
        "/repos/o/r": dict(id=123),
        "/repos/o/r/commits/" + source["head_sha"]: dict(sha=commit_sha or source["head_sha"]),
        "/repos/o/r/commits/" + source["head_sha"] + "/check-runs": dict(
            check_runs=[dict(id=9, name=check_name, app=dict(id=15368), head_sha=source["head_sha"], status="completed", conclusion=check_conclusion)]
        ),
    }


async def test_deployment_predecessor_source_verifies_the_deployed_revision_and_its_checks_without_a_pull_request():
    """#7880: an operate-role predecessor delivered a deployment, not a PR. Its source is the
    runtime-verified deployment receipt; the provider proves the deployed revision exists and
    the declared checks are green on it, and never reads a pull request or a review."""
    source = _deployment_source()
    observed = await _observe_deployment(source, _deployment_responses(source))
    assert observed["pull_requests"] == [] and observed["workflows"] == [] and observed["mandatory_passed"] is True
    (deployment,) = observed["deployments"]
    assert deployment["deployment_receipt_key"] == "operations-runtime:38851653127:1"
    assert deployment["checks"] == [dict(name="Lint", app_id=15368, check_run_id=9, head_sha="d" * 40)]
    assert "required_checks" not in deployment and deployment.get("pr_number") is None
    from src.orchestration.repository_evaluation import RepositoryEvaluationReceipt

    receipt = RepositoryEvaluationReceipt(
        org_id="org",
        flow_id="flow",
        node_id="eval",
        cycle=1,
        accepted_plan_version=52,
        accepted_plan_hash="a" * 64,
        evaluation_policy_hash="b" * 64,
        specification_hash="c" * 64,
        harness_sha256="d" * 64,
        source_snapshot_hash="e" * 64,
        observed_at=datetime.now(UTC),
        **observed,
    )
    assert receipt.deployments[0].merge_sha == "d" * 40 and receipt.model_dump(mode="json")["deployments"][0]["components"] == ["knowledge-door"]


async def test_deployment_predecessor_source_without_declared_checks_still_needs_the_revision():
    source = _deployment_source(required_checks=[])
    responses = _deployment_responses(source)
    responses.pop("/repos/o/r/commits/" + source["head_sha"] + "/check-runs")
    spec = RepositoryEvaluationSpecification.model_validate(
        dict(
            evidence_schema="repository-evaluation/v1",
            runner=dict(adapter="engine-repository-evidence-v1", repository="o/r", repository_id=123, harness_sha256="c" * 64),
            predecessors=[dict(address=source["address"], required_checks=[])],
        )
    )
    observed = await _observe_deployment(source, responses, spec=spec)
    assert observed["deployments"][0]["checks"] == []


def operation_workflow_case(evidence):
    source = _deployment_source(
        deployment_receipt_key="operations-runtime:10:2",
        workflow_operation_keys=["operations-workflow:10:2"],
        head_sha="b" * 40,
        merge_sha="b" * 40,
        required_checks=[],
    )
    atlassian_observer_case(evidence)
    document = evidence.spec.model_dump()
    document["external_pull_requests"] = []
    document["predecessors"] = [{"address": source["address"]}]
    workflow = document["workflows"][0]
    workflow["source"] = workflow["definition"] = {"predecessor": source["address"]}
    evidence.spec = RepositoryEvaluationSpecification.model_validate(document)
    evidence.source = source
    evidence.responses["/repos/o/r/commits/" + "b" * 40] = {"sha": "b" * 40}
    evidence.responses["/repos/o/r/actions/runs/10"]["head_sha"] = "f" * 40
    # A correlated dispatch uses current main, while its deployed source stays
    # pinned. Searching GitHub by the deployed SHA therefore finds no run.
    evidence.responses["/repos/o/r/actions/workflows/scan.yml/runs"] = {"workflow_runs": []}
    evidence.responses["/repos/o/r/compare/" + "b" * 40 + "..." + "f" * 40] = {
        "status": "ahead",
        "base_commit": {"sha": "b" * 40},
        "merge_base_commit": {"sha": "b" * 40},
    }
    atlassian_observer_archive(evidence)
    return evidence


async def test_operation_evaluation_reads_receipted_run_when_main_advanced(evidence):
    operation_workflow_case(evidence)
    receipt = await observe(evidence)
    assert receipt["mandatory_passed"] is True
    workflow = receipt["workflows"][0]
    assert (workflow["run_id"], workflow["run_attempt"]) == (10, 2)
    assert workflow["source_revision"] == "b" * 40
    assert workflow["definition_revision"] == "f" * 40
    assert receipt["deployments"][0]["deployment_receipt_key"] == "operations-runtime:10:2"


@pytest.mark.parametrize("failure", [None, "missing", "digest", "predicate", "source", "attempt", "target", "jobs", "workflow", "rerun"])
@pytest.mark.parametrize("foundation", [False, True])
async def test_identity_proof_reads_only_signed_s3_evidence_for_the_bound_runtime(evidence, failure, foundation):
    from src.orchestration.repository_evaluation_contract import WorkflowReceipt

    operation_workflow_case(evidence)
    path = ".github/workflows/gateway-deploy.yml"
    definition = b"on:\n  workflow_dispatch:\nenv:\n  ADP_IDENTITY_PROOF_EVIDENCE_STORE: s3-oidc-v1\njobs: {}\n"
    attempt = 1 if foundation else 2
    archive_name = "atlassian-foundation" if foundation else "identity-proof"
    target_field = "foundation_artifact_target" if foundation else "identity_proof_artifact_target"
    if foundation:
        path = ".github/workflows/tools-platform-deploy.yml"
        definition = b"on:\n  workflow_dispatch:\njobs:\n  adapter-foundation:\n    env:\n      ADP_FOUNDATION_EVIDENCE_STORE: s3-oidc-v1\n"
        evidence.source.update(deployment_receipt_key="operations-runtime:10:1", workflow_operation_keys=["operations-workflow:10:1"])
        evidence.responses["/repos/o/r/actions/runs/10"]["run_attempt"] = 1
        evidence.responses["/repos/o/r/actions/runs/10/attempts/1/jobs"] = evidence.responses["/repos/o/r/actions/runs/10/attempts/2/jobs"]
        evidence.document.update(gateway_revision="c" * 40, run_attempt="1")
    evidence.responses["/repos/o/r/contents/" + path] = dict(
        type="file",
        encoding="base64",
        size=len(definition),
        content=base64.b64encode(definition).decode(),
        sha=hashlib.sha1(b"blob " + str(len(definition)).encode() + b"\0" + definition).hexdigest(),
    )
    evidence.responses["/repos/o/r/actions/runs/10"]["path"] = path
    jobs = evidence.responses["/repos/o/r/actions/runs/10/attempts/2/jobs"]["jobs"]
    jobs[0]["name"], jobs[1]["name"] = "Build and Deploy Backend", "adapter-foundation" if foundation else "identity-proof"
    workflow = evidence.spec.workflows[0].model_copy(update={"path": path, "required_jobs": [job["name"] for job in jobs]})
    if foundation:
        artifact = workflow.artifacts[0].model_copy(update={"name": "tools-live-evaluation-{run_id}-{run_attempt}"})
        workflow = workflow.model_copy(update={"artifacts": [artifact]})
    evidence.source[target_field] = dict(account_id="123456789012", region="us-east-1", environment="dev")
    evidence.source["components"] = ["tool-adapters" if foundation else "gateway-backend"]
    if failure == "target":
        evidence.source.pop(target_field)
    if failure == "jobs":
        workflow = workflow.model_copy(update={"required_jobs": ["Build and Deploy Backend"]})
    if failure == "workflow":
        other = ".github/workflows/other.yml"
        evidence.responses["/repos/o/r/contents/" + other] = evidence.responses["/repos/o/r/contents/" + path]
        evidence.responses["/repos/o/r/actions/runs/10"]["path"] = other
        workflow = workflow.model_copy(update={"path": other})
    if failure == "predicate":
        evidence.document["coverage"] = False
    if failure == "source":
        evidence.document["source_revision"] = "e" * 40
    if failure == "attempt":
        evidence.document["run_attempt"] = str(attempt + 1)
    atlassian_observer_archive(evidence)
    calls = []

    def read_archive(**kwargs):
        calls.append(kwargs)
        if failure == "missing":
            return None
        if failure == "rerun":
            evidence.responses["/repos/o/r/actions/runs/10"]["run_attempt"] = 3
        digest = "0" * 64 if failure == "digest" else hashlib.sha256(evidence.binary).hexdigest()
        return evidence.binary, digest, "s3://evidence/identity-proof.json?versionId=v1"

    def transport(request):
        assert "/artifacts" not in request.url.path
        return httpx.Response(200, json=evidence.responses[request.url.path])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        provider = RepositoryEvidenceProvider(client=client, clock=lambda: evidence.now, evidence_store=SimpleNamespace(read_archive=read_archive))
        provider.token = AsyncMock(return_value="test-read-only-token")
        call = provider.workflow(
            SimpleNamespace(repo="o/r", provider_repository_id=123),
            workflow,
            revisions={evidence.source["address"]: "b" * 40},
            max_age_seconds=3600,
            operation_source=evidence.source,
        )
        if failure not in {None, "predicate"}:
            with pytest.raises(CycleBlockedError):
                await call
        else:
            result = await call
            WorkflowReceipt.model_validate(result)
            assert result["artifacts"][0]["artifact_id"] is None
            assert result["artifacts"][0]["artifact_ref"].endswith("versionId=v1")
            assert result["criteria"][0]["passed"] is (failure != "predicate")
            assert calls == [
                dict(
                    account="123456789012",
                    environment="dev",
                    region="us-east-1",
                    repository="o/r",
                    repository_id=123,
                    run_id=10,
                    attempt=attempt,
                    kind="repository-artifact",
                    name=archive_name,
                    workflow_path=path,
                    workflow_revision="f" * 40,
                    source_revision="b" * 40,
                )
            ]


@pytest.mark.parametrize(
    "changes",
    [
        {"deployment_receipt_key": "operations-runtime:10:not-an-attempt"},
        {"workflow_operation_keys": ["operations-workflow:11:2"]},
        {"workflow_operation_keys": ["operations-workflow:10:2", "operations-workflow:11:2"]},
    ],
)
async def test_operation_evaluation_refuses_inconsistent_runtime_workflow_keys(evidence, changes):
    operation_workflow_case(evidence).source.update(changes)
    with pytest.raises(CycleBlockedError, match="operation_workflow_binding_changed"):
        await observe(evidence)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"run_attempt": 3}, "operation_workflow_run_changed"),
        ({"head_sha": "invalid"}, "operation_workflow_run_changed"),
        ({"id": 11}, "workflow_run_not_successful_or_changed"),
        ({"path": ".github/workflows/other.yml"}, "workflow_run_not_successful_or_changed"),
        ({"repository": {"id": 456}}, "workflow_run_not_successful_or_changed"),
        ({"head_repository": {"id": 456}}, "workflow_run_not_successful_or_changed"),
        ({"conclusion": "failure"}, "workflow_run_not_successful_or_changed"),
    ],
)
async def test_operation_evaluation_refuses_a_changed_or_failed_receipted_run(evidence, changes, reason):
    operation_workflow_case(evidence).responses["/repos/o/r/actions/runs/10"].update(changes)
    with pytest.raises(CycleBlockedError, match=reason):
        await observe(evidence)


async def test_operation_evaluation_refuses_changed_workflow_at_dispatch_head(evidence):
    operation_workflow_case(evidence)
    changed = b"on:\n  workflow_dispatch:\njobs: {changed: {}}\n"
    evidence.definitions = {
        "f" * 40: dict(
            type="file",
            encoding="base64",
            size=len(changed),
            content=base64.b64encode(changed).decode(),
            sha=hashlib.sha1(b"blob " + str(len(changed)).encode() + b"\0" + changed).hexdigest(),
        )
    }
    with pytest.raises(CycleBlockedError, match="workflow_definition_changed"):
        await observe(evidence)


async def test_operation_evaluation_refuses_unrelated_dispatch_head(evidence):
    operation_workflow_case(evidence)
    evidence.responses["/repos/o/r/compare/" + "b" * 40 + "..." + "f" * 40]["status"] = "diverged"
    with pytest.raises(CycleBlockedError, match="workflow_missing_delivered_revision"):
        await observe(evidence)


@pytest.mark.parametrize(
    "field,value",
    [("source_revision", "f" * 40), ("definition_revision", "b" * 40), ("run_id", "11"), ("run_attempt", "3")],
)
async def test_operation_evaluation_refuses_artifacts_from_another_release_or_attempt(evidence, field, value):
    operation_workflow_case(evidence).document[field] = value
    atlassian_observer_archive(evidence)
    with pytest.raises(CycleBlockedError, match="artifact_workflow_provenance_mismatch"):
        await observe(evidence)


async def test_operation_evaluation_refuses_rerun_while_reading_artifact(evidence):
    operation_workflow_case(evidence)
    evidence.after_artifact_read = lambda: evidence.responses["/repos/o/r/actions/runs/10"].update(run_attempt=3)
    with pytest.raises(CycleBlockedError, match="workflow_run_changed_during_observation"):
        await observe(evidence)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (dict(commit_sha="e" * 40), "deployed_revision_missing"),
        (dict(check_name="Other"), "required_check_missing_or_ambiguous"),
        (dict(check_conclusion="failure"), "required_check_not_successful"),
    ],
)
async def test_deployment_predecessor_source_refuses_missing_revision_or_red_checks(change, reason):
    source = _deployment_source()
    with pytest.raises(CycleBlockedError, match=reason):
        await _observe_deployment(source, _deployment_responses(source, **change))


def test_receipt_omits_deployments_when_absent_and_requires_some_evidence():
    from src.orchestration.repository_evaluation import RepositoryEvaluationReceipt
    from src.orchestration.repository_evaluation_contract import Predecessor

    base = dict(
        org_id="org",
        flow_id="flow",
        node_id="eval",
        cycle=1,
        accepted_plan_version=1,
        accepted_plan_hash="a" * 64,
        evaluation_policy_hash="b" * 64,
        specification_hash="c" * 64,
        harness_sha256="d" * 64,
        source_snapshot_hash="e" * 64,
        observed_at=datetime.now(UTC),
        workflows=[],
        mandatory_passed=True,
    )
    with pytest.raises(ValueError, match="cannot be empty"):
        RepositoryEvaluationReceipt(**base, pull_requests=[])
    document = RepositoryEvaluationReceipt(
        **base,
        pull_requests=[
            dict(
                pr_number=1,
                head_sha="a" * 40,
                merge_sha="b" * 40,
                provider_pr_node_id="PR_1",
                merged_at=datetime.now(UTC),
                checks=[dict(name="Lint", app_id=15368, check_run_id=1, head_sha="a" * 40)],
            )
        ],
    ).model_dump(mode="json")
    assert "deployments" not in document  # receipts without deployment sources serialize exactly as before
    # An operations predecessor may declare no checks; the pull-request path refuses that in sources_for.
    assert Predecessor(address="flow/epic/w/ops").required_checks == []
