"""Release entry points retain real source, result, checkpoint and evaluator checks."""

import base64
import importlib.util
import json
import runpy
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import partial
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import test_qualification_driver
import test_qualification_evaluator
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from l5_services.adapters.transform import qualification_release as release
from l5_services.adapters.transform.artifacts import TransformArtifacts
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.qualification_source import RetainedProductSource, TransformQualificationAuthority
from l5_services.models import Configuration, ConfigurationVersion, Run
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.base import Base
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION
from test_isolated_executor import S3Boundary


NOW = datetime(2026, 10, 29, tzinfo=UTC)
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
observations = test_qualification_driver.observations
published = test_qualification_evaluator.published


@pytest.fixture
async def assembled(published, tmp_path):
    evaluator, run, attempt, reference, work_authority, result, retained, logs, requests = published
    original = evaluator.collector.source.client
    receipt = await original.read_source("product-a", "project-a", "sealed-a", work_authority.source_request)
    inventory = await original.read_source_manifest("product-a", "project-a", "sealed-a", work_authority.source_request)
    archive = b"".join([chunk async for chunk in original.stream_source(
        "product-a", "project-a", "sealed-a", work_authority.source_request)])
    source_status = [200]
    calls = []

    async def respond(request):
        calls.append(request)
        assert request.method == "GET" and not request.content
        if request.url.path.endswith("/source-manifest"):
            return httpx.Response(200, json=run.manifest)
        if "/source-snapshots/" in request.url.path:
            assert request.url.path.startswith("/v1/products/product-a/projects/project-a/services/transform/runs/run-a/source-snapshots/sealed-a")
            if source_status[0] != 200:
                return httpx.Response(source_status[0])
            if request.url.path.endswith("/content"):
                return httpx.Response(200, content=archive)
            if request.url.path.endswith("/manifest"):
                return httpx.Response(200, content=inventory)
            return httpx.Response(200, json=receipt)
        return await evaluator.results_client.get(request.url.path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://example.invalid")
    run.capture_limits = work_authority.capture_limits
    run.admission["provider_identity"] = work_authority.provider_identity
    source = RetainedProductSource(client, run.admission["scope"], run.run_id)
    authority = TransformQualificationAuthority(run, source, clock=lambda: NOW)
    s3 = S3Boundary()
    artifacts = TransformArtifacts(s3, "example-artifacts", clock=lambda: NOW)
    patch_ref = await artifacts.put(tenant_id=run.tenant_id, project_id=run.project_id, run_id=run.run_id,
        name="provider-original-patch", contents=evaluator.collector.provider.patch, media_type="text/x-diff", authority=authority)
    patch_ref.pop("expires_at")
    result["original_patch"].update(patch_ref)
    intent = TransformAdapter(object())._intent(run, attempt)
    owner = sha256(f"{run.tenant_id}:{run.product_id}:{run.project_id}:{run.run_id}:{intent['attempt_token']}".encode()).hexdigest()[:40]
    observation = {**intent, "executor_uid": run.native_job_ref["executor_uid"],
        "readback": evaluator.collector.provider.readback["readback"],
        "patch": base64.b64encode(evaluator.collector.provider.patch).decode(),
        "candidate_files": {"src/example.py": base64.b64encode(b"changed\n").decode()},
        "candidate_modes": {"src/example.py": "100644"}}
    checkpoint = {"observation": observation, "termination": {"exitCode": 0}, "original_patch": patch_ref,
        "cleanup": {"uid": run.native_job_ref["executor_uid"], "name": intent["executor_name"],
                    "labels": {"adp.dev/transform-work": owner}}}
    await artifacts.save_checkpoint(name=intent["executor_name"], value=checkpoint, authority=authority)
    policy = run.admission["transform_test_policy"]
    check_owner = sha256(json.dumps([authority.scope, run.run_id], sort_keys=True).encode()).hexdigest()[:40]
    for stage, contents in (("before", b"original\n"), ("after", b"changed\n")):
        entry = result["tests_" + stage][0]["receipt"]
        entry["executor_uid"] = "check-" + stage
        for stream in ("stdout", "stderr"):
            data = logs.contents["stdout-" + stage] if stream == "stdout" else b""
            log = await artifacts.put(tenant_id=run.tenant_id, project_id=run.project_id, run_id=run.run_id,
                name=stage + "-" + stream, contents=data, media_type="text/plain", authority=authority)
            entry[stream] = {key: log[key] for key in ("artifact_id", "version", "sha256", "bytes")}
        payload = {"stage": stage, "source_tree": entry["source_tree"], "command": policy["commands"][0],
            "file_modes": {"src/example.py": "100644"}, "files": {"src/example.py": base64.b64encode(contents).decode()},
            **{key: policy[key] for key in ("image_digest", "bundle_sha256", "oracle_version", "lock_sha256")}}
        identity = sha256((check_owner + json.dumps(payload, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
        name = "tf-check-" + identity[:40]
        await artifacts.save_checkpoint(name=name, value={"input_sha256": identity, "receipt": entry,
            "cleanup": {"uid": entry["executor_uid"], "name": name, "labels": {"adp.dev/transform-check": check_owner}}}, authority=authority)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runs.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(Configuration(tenant_id=run.tenant_id, configuration_id="config-a", product_id=run.product_id,
            project_id=run.project_id, service_id="transform", relationship_revision="1", owner_id="owner-a",
            current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id=run.tenant_id, configuration_id="config-a", revision=1,
            settings={}, inherited_fields=[], gaps=[], state="ready", hash="e" * 64))
        session.add(Run(tenant_id=run.tenant_id, run_id=run.run_id, product_id=run.product_id, project_id=run.project_id,
            service_id="transform", configuration_id="config-a", configuration_revision=1, binding_id="binding-a",
            trigger_id="manual-a", binding_generation=1, admission=run.admission, input=run.input, manifest=run.manifest,
            native_job_ref=run.native_job_ref, capture_limits=run.capture_limits, state="succeeded",
            deadline_at=NOW - timedelta(days=1)))
        session.add(WorkerAttempt(tenant_id=run.tenant_id, attempt_id=attempt.attempt_id, run_id=run.run_id,
            operation=TRANSFORM_SUBMIT_OPERATION, first_work_id=attempt.work_id, first_fence=attempt.fence,
            idempotency_token=attempt.idempotency_token, input_hash=attempt.input_hash, started_at=attempt.started_at,
            status="observed", native_job_ref=run.native_job_ref))
        await session.commit()
    fixture = {"source_sha": "a" * 40, "runs": [{"tenant_id": run.tenant_id, "product_id": run.product_id,
        "project_id": run.project_id, "run_id": run.run_id, "result_id": "result-a", "result_revision": 1}]}
    try:
        yield fixture, sessions, client, artifacts, result, receipt, s3, calls, source_status
    finally:
        await client.aclose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_completed_run_qualifies_without_work_lease_or_provider_effects(assembled):
    fixture, sessions, client, artifacts, result, receipt, s3, calls, source_status = assembled
    before = deepcopy(s3.objects)
    report = await release.qualify(fixture, sessions, client, artifacts, clock=lambda: NOW)
    assert report["status"] == "evaluated" and report["live_accepted"] is False
    assert report["runs"][0]["original_patch_sha256"] == result["original_patch"]["sha256"]
    assert "tenant-a" not in json.dumps(report)
    assert s3.objects == before
    assert calls and all(request.method == "GET" for request in calls)
    assert all("credential" not in request.url.path for request in calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["source", "revoked", "expired", "revision", "patch", "receipt", "checkpoint", "foreign"])
async def test_invalid_observations_cannot_emit_qualification(assembled, failure):
    fixture, sessions, client, artifacts, result, receipt, s3, calls, source_status = assembled
    if failure == "source":
        receipt["source_ref"] = {**receipt["source_ref"], "record_revision": "f" * 40}
    elif failure == "revoked":
        source_status[0] = 403
    elif failure == "expired":
        for saved in s3.objects.values():
            saved["Metadata"]["expires-at"] = (NOW - timedelta(seconds=1)).isoformat()
    elif failure == "revision":
        fixture["runs"][0]["result_revision"] = 2
    elif failure == "patch":
        result["original_patch"]["sha256"] = "f" * 64
    elif failure == "receipt":
        result["tests_after"][0]["receipt"]["executor_uid"] = "fabricated"
    elif failure == "checkpoint":
        s3.objects = {key: value for key, value in s3.objects.items() if "/checkpoints/tf-check-" not in key[0]}
    else:
        fixture["runs"][0]["project_id"] = "foreign-project"
    with pytest.raises((ValueError, httpx.HTTPStatusError)):
        await release.qualify(fixture, sessions, client, artifacts, clock=lambda: NOW)


@pytest.mark.parametrize("script, arguments, phase", [
    ("qualify-transform-release.py", ["preflight"], "preflight"),
    ("qualify-transform-release.py", ["run"], "run"),
    ("emit-transform-qualification.py", [], "emit"),
])
def test_executable_release_entry_points(monkeypatch, tmp_path, script, arguments, phase):
    calls = []

    async def invoked(source_sha, fixture, evidence, *, phase):
        calls.append((source_sha, fixture, evidence, phase))
        return {"status": "configured"}

    sys.path.insert(0, str(SCRIPTS))
    import qualification_transform_release as acceptance_entrypoint
    monkeypatch.setattr(acceptance_entrypoint, "run", invoked)
    monkeypatch.setattr(sys, "argv", [script, *arguments, "a" * 40, str(tmp_path / "fixture.json"), str(tmp_path)])
    path = SCRIPTS / script
    assert path.is_file() and not path.is_symlink() and path.stat().st_mode & 0o100
    runpy.run_path(str(path), run_name="__main__")
    assert calls == [("a" * 40, str(tmp_path / "fixture.json"), str(tmp_path), phase)]


def test_shared_dispatcher_accepts_transform_entry_points():
    path = SCRIPTS / "qualification-dispatch.py"
    if not path.exists():
        pytest.skip("shared release dispatcher is integrated by the shared owner")
    spec = importlib.util.spec_from_file_location("dispatch", path)
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    assert dispatch.check_driver("transform", SCRIPTS) == ("qualify-transform-release.py", "emit-transform-qualification.py")


def test_fixture_rejects_wrong_revision_and_untrusted_evidence(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"source_sha": "b" * 40, "runs": []}))
    fixture.chmod(0o600)
    with pytest.raises(ValueError):
        release.load_fixture("a" * 40, fixture)
    fixture.write_text(json.dumps({"source_sha": "a" * 40, "runs": [], "provider_passed": True}))
    with pytest.raises(ValueError):
        release.load_fixture("a" * 40, fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_readback", [False, True])
async def test_release_composition_rechecks_external_evidence_before_emission(assembled, monkeypatch, tmp_path, invalid_readback):
    from l5_services import runtime_support as resources

    fixture, sessions, client, artifacts, result, receipt, s3, calls, source_status = assembled
    fixture_path = tmp_path / "fixture.json"
    fixture["runtime"] = {"L5_GATEWAY_URL": "https://example.invalid", "AWS_REGION": "us-east-1",
        "L5_QUALIFICATION_API_TOKEN": "offline-test-token", "L5_TRANSFORM_ARTIFACT_BUCKET": "example-artifacts",
        "L5_RDS_HOST": "example.invalid", "L5_RDS_PORT": "5432", "L5_RDS_DATABASE": "example", "L5_RDS_USER": "example-reader"}
    fixture_path.write_text(json.dumps(fixture))
    fixture_path.chmod(0o600)
    evidence = tmp_path / "evidence"
    evidence.mkdir(mode=0o700)
    monkeypatch.setattr(resources, "sessions", lambda: sessions)
    monkeypatch.setattr(release.boto3, "client", lambda *args, **kwargs: s3)
    # The assembled checkpoints and source evidence use NOW. Keep the release
    # composition on that same clock so fixture retention does not age with CI.
    monkeypatch.setattr(release, "TransformArtifacts", partial(TransformArtifacts, clock=lambda: NOW))
    monkeypatch.setattr(release, "qualify", partial(release.qualify, clock=lambda: NOW))
    initialize = httpx.AsyncClient.__init__

    def with_transport(instance, **kwargs):
        initialize(instance, transport=client._transport, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", with_transport)
    before = deepcopy(s3.objects)
    previous_calls = len(calls)
    await release.run(fixture["source_sha"], fixture_path, evidence, phase="preflight")
    assert list(evidence.iterdir()) == [] and len(calls) == previous_calls
    await release.run(fixture["source_sha"], fixture_path, evidence, phase="run")
    assert json.loads((evidence / "observations.json").read_text())["status"] == "evaluated"
    (evidence / "observations.json").write_text('{"status":"fabricated"}')
    if invalid_readback:
        source_status[0] = 403
        with pytest.raises(httpx.HTTPStatusError):
            await release.run(fixture["source_sha"], fixture_path, evidence, phase="emit")
        assert not (evidence / "qualification.json").exists()
    else:
        await release.run(fixture["source_sha"], fixture_path, evidence, phase="emit")
        report = json.loads((evidence / "qualification.json").read_text())
        assert report["status"] == "evaluated" and report["live_accepted"] is False
    assert s3.objects == before
