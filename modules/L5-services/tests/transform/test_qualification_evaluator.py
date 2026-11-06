"""Offline evaluation through the real scoped results router and independent reader boundaries."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from l5_services.adapters.transform.qualification_evaluator import TransformQualificationEvaluator
from l5_services.adapters.transform.provenance import _file_tree
from l5_services.api.results import create_results_router
from l5_services.workers.execution import AuthorityRevoked
from test_qualification_driver import observations  # noqa: F401


NOW = datetime(2026, 10, 29, tzinfo=UTC)


class Logs:
    def __init__(self):
        self.contents = {"stdout-before": b"one passed", "stdout-after": b"one passed",
                         "stderr": b""}
        self.calls = []

    async def read_log(self, *, reference, authority):
        authority.assert_current()
        self.calls.append(reference["artifact_id"])
        return self.contents[reference["artifact_id"]]


def log_ref(name, contents):
    return {"artifact_id": name, "version": "v1", "sha256": sha256(contents).hexdigest(),
            "bytes": len(contents)}


def receipt(stage, tree, policy, command, logs):
    return {"stage": stage, "argv": command["argv"], "cwd": command["cwd"],
            "environment": command["environment"], "source_tree": tree,
            "image_digest": policy["image_digest"], "bundle_sha256": policy["bundle_sha256"],
            "lock_sha256": policy["lock_sha256"], "oracle_version": policy["oracle_version"],
            "started_at": f"2026-10-29T0{'0' if stage == 'before' else '2'}:00:00Z",
            "ended_at": f"2026-10-29T0{'0' if stage == 'before' else '2'}:00:01Z",
            "timed_out": False, "exit_code": 0, "test_count": 1,
            "stdout": log_ref(f"stdout-{stage}", logs.contents[f"stdout-{stage}"]),
            "stderr": log_ref("stderr", b"")}


@pytest.fixture
async def published(observations):  # noqa: F811
    collector, run, attempt, reference, authority, native, requests = observations
    attempt = replace(attempt, started_at=datetime(2026, 10, 29, 1, tzinfo=UTC))
    observed = await collector.collect(run, attempt, reference, authority)
    command = {"argv": ["/oracle/check"], "cwd": ".", "environment": {"NO_NETWORK": "1"},
               "timeout_seconds": 120, "minimum_test_count": 1}
    policy = {"image_digest": "sha256:" + "b" * 64, "bundle_sha256": "c" * 64,
              "lock_sha256": "d" * 64, "oracle_version": "oracle-1", "commands": [command],
              "test_policy_ref": {"record_id": "policy-a"}}
    run.admission["transform_settings"]["test_policy_ref"] = policy["test_policy_ref"]
    run.admission["transform_test_policy"] = policy
    run.configuration_revision = 1
    logs = Logs()
    retained = {**reference, "artifact_id": "retained-patch", "expires_at": "2027-01-01T00:00:00Z"}
    retained_logs = {}
    for stage in ("before", "after"):
        for stream in ("stdout", "stderr"):
            contents = logs.contents[f"stdout-{stage}"] if stream == "stdout" else b""
            name = f"tests_{stage}-0-{stream}"
            retained_logs[name] = {**log_ref(f"retained-{name}", contents),
                                   "tenant_id": run.tenant_id, "project_id": run.project_id,
                                   "media_type": "text/plain", "expires_at": "2027-01-01T00:00:00Z"}
    candidate_tree = _file_tree({"src/example.py": b"changed\n"}, {"src/example.py": "100644"})
    result = {"schema_version": "1.0", "service_id": "transform", "result_id": "result-a",
              "run_id": run.run_id, "revision": 1, "scope": observed["scope"],
              "source_ref": run.manifest["sources"],
              "configuration_revision": run.configuration_revision,
              "test_policy_ref": policy["test_policy_ref"],
              "native_job_ref": observed["native_job_ref"],
              "provider_attribution": observed["provider_pins"],
              "original_patch": {**reference, "native_job_ref": observed["native_job_ref"],
                                 "provider_pins": observed["provider_pins"],
                                 "base_manifest_sha256": attempt.input_hash},
              "retained_artifacts": {"original_patch": retained, **retained_logs},
              "source_trees": {"base_tree": observed["source_tree"],
                               "candidate_tree": candidate_tree, "changed_files": ["src/example.py"]},
              "scope_status": "passed", "dependency_comparison": {"status": "not_applicable"},
              "verification_status": "succeeded", "limitations": [],
              "tests_before": [{"stage": "before", "status": "passed",
                                "receipt": receipt("before", observed["source_tree"], policy, command, logs)}],
              "tests_after": [{"stage": "after", "status": "passed",
                               "receipt": receipt("after", candidate_tree, policy, command, logs)}]}

    class Store:
        transform_reader_ready = True
        clock = staticmethod(lambda: NOW)

        async def get(self, result_id):
            assert result_id == "result-a"
            return result

        async def artifact(self, artifact_id):
            if artifact_id == "retained-patch":
                return retained, native.patch
            for name, item in retained_logs.items():
                if item["artifact_id"] == artifact_id:
                    stage = name.split("_")[1].split("-")[0]
                    return item, logs.contents[f"stdout-{stage}"] if name.endswith("stdout") else b""
            raise AssertionError("unscoped artifact read")

    class Backend:
        transform_result_reader_ready = True

        async def result_store(self, product_id, project_id, service_id):
            assert (product_id, project_id, service_id) == ("product-a", "project-a", "transform")
            return Store()

    async def backend():
        return Backend()

    app = FastAPI()
    app.include_router(create_results_router(backend),
                       prefix="/v1/products/{product_id}/projects/{project_id}/services/{service_id}")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="https://example.invalid") as client:
        evaluator = TransformQualificationEvaluator(collector, client, logs, clock=lambda: NOW)
        yield evaluator, run, attempt, reference, authority, result, retained, logs, requests


@pytest.mark.asyncio
async def test_scoped_result_and_native_evaluator_emit_sanitized_observation(published):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    report = await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
    assert report == {"schema": "transform-qualification/v1", "story": "9296",
                      "status": "evaluated", "live_accepted": False,
                      "run_sha256": sha256(run.run_id.encode()).hexdigest(),
                      "result_sha256": sha256(b"result-a").hexdigest(), "result_revision": 1,
                      "source_tree": result["source_trees"]["base_tree"],
                      "original_patch_sha256": reference["sha256"], "blockers": []}
    assert sorted(logs.calls) == ["stderr", "stderr", "stdout-after", "stdout-before"]
    # Fixture observation, independent evaluation and patch reconstruction each
    # reread the receipt before their bounded manifest and content transfers.
    assert [request.url.path.rsplit("/", 1)[-1] for request in requests] == [
        "read", "read", "manifest", "read", "content"] * 3
    assert run.run_id not in str(report) and "tenant-a" not in str(report)


@pytest.mark.asyncio
async def test_emitter_writes_only_evaluated_schema(published, tmp_path):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    output = tmp_path / "qualification.json"
    report = await evaluator.emit(run, attempt, "result-a", 1, reference, authority, output)
    assert json.loads(output.read_text()) == report
    assert report["live_accepted"] is False
    output.unlink()
    result["revision"] = 2
    with pytest.raises(ValueError):
        await evaluator.emit(run, attempt, "result-a", 1, reference, authority, output)
    assert not output.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("revision", 2), ("source_ref", []), ("native_job_ref", {}), ("scope", {}),
    ("provider_attribution", {}), ("retained_artifacts", {}), ("original_patch", {}),
    ("source_trees", {}),
])
async def test_fabricated_or_wrong_revision_result_is_rejected(published, field, value):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    result[field] = value
    with pytest.raises(ValueError):
        await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
    assert logs.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["candidate_tree", "changed_files"])
async def test_patch_must_produce_the_reported_candidate_and_changed_files(published, change):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    if change == "candidate_tree":
        result["source_trees"]["candidate_tree"] = "0" * 40
        result["tests_after"][0]["receipt"]["source_tree"] = "0" * 40
    else:
        result["source_trees"]["changed_files"] = ["src/unrelated.py"]
    with pytest.raises(ValueError):
        await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
    assert logs.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["candidate_before_submit", "baseline_after_submit",
                                     "missing_submission", "naive_submission"])
async def test_check_timeline_must_surround_durable_submission(published, change):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    if change == "candidate_before_submit":
        result["tests_after"][0]["receipt"]["started_at"] = "2026-10-29T00:30:00Z"
    elif change == "baseline_after_submit":
        result["tests_before"][0]["receipt"]["ended_at"] = "2026-10-29T01:30:00Z"
    elif change == "missing_submission":
        attempt = replace(attempt, started_at=None)
    else:
        attempt = replace(attempt, started_at=attempt.started_at.replace(tzinfo=None))
    report = await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
    assert report["status"] == "unresolved"
    assert report["blockers"]


@pytest.mark.asyncio
async def test_checks_can_meet_at_the_submission_boundary(published):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    result["tests_before"][0]["receipt"]["ended_at"] = "2026-10-29T01:00:00Z"
    result["tests_after"][0]["receipt"]["started_at"] = "2026-10-29T02:00:00+01:00"
    report = await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
    assert report["status"] == "evaluated" and report["blockers"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing", "status_only", "wrong_tree", "wrong_log", "corrupt_log"])
async def test_missing_or_falsified_checks_cannot_be_evaluated(published, change):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    if change == "missing":
        result["tests_after"] = []
    elif change == "status_only":
        result["tests_after"][0]["receipt"] = None
    elif change == "wrong_tree":
        result["tests_after"][0]["receipt"]["source_tree"] = "0" * 40
    elif change == "wrong_log":
        result["tests_after"][0]["receipt"]["stdout"]["sha256"] = "0" * 64
    else:
        logs.contents["stdout-after"] = b"changed"
    if change in {"wrong_tree"}:
        with pytest.raises(ValueError):
            await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
    else:
        report = await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)
        assert report["status"] == "unresolved" and report["blockers"]


@pytest.mark.asyncio
async def test_retained_bytes_are_not_accepted_when_falsified(published):
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    retained["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        await evaluator.evaluate(run, attempt, "result-a", 1, reference, authority)


RESULT_SCENARIOS = json.loads((Path(__file__).parent / "qualification-result-scenarios.json").read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", RESULT_SCENARIOS["cases"], ids=lambda case: case["id"])
async def test_scn03_source_bound_published_qualification(published, tmp_path, scenario):
    assert RESULT_SCENARIOS["schema_version"] == "1.0"
    assert RESULT_SCENARIOS["fixture_class"] == "synthetic_not_provider_evidence"
    assert datetime.fromisoformat(RESULT_SCENARIOS["clock_at"].replace("Z", "+00:00")) == NOW
    evaluator, run, attempt, reference, authority, result, retained, logs, requests = published
    provider = evaluator.collector.provider
    provider.calls.clear()
    requests.clear()
    change = scenario["change"]
    if change == "source_revision":
        run.manifest["sources"][0]["record_revision"] = "f" * 40
    elif change == "provider_patch":
        provider.patch = b"fabricated provider patch"
    elif change == "result_revision":
        result["revision"] = 2
    elif change == "missing_check":
        result["tests_after"] = []
    else:
        assert change == "none"
    output = tmp_path / "qualification.json"
    if scenario["expected_status"] == "rejected":
        with pytest.raises(AuthorityRevoked if change == "source_revision" else ValueError):
            await evaluator.emit(run, attempt, "result-a", 1, reference, authority, output)
        assert not output.exists() and logs.calls == []
        if change == "source_revision":
            assert provider.calls == [] and requests == []
    else:
        report = await evaluator.emit(run, attempt, "result-a", 1, reference, authority, output)
        assert json.loads(output.read_text()) == report
        assert report["status"] == scenario["expected_status"]
        assert report["live_accepted"] is False
        assert provider.calls == ["read", "collect", "patch"] and requests
        if change == "missing_check":
            assert report["blockers"] == ["after checks missing"]
        else:
            assert report["blockers"] == [] and len(logs.calls) == 4
