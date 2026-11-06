"""Executable, read-only Transform qualification using current Product authority."""

import argparse
import asyncio
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import boto3
import httpx
from botocore.config import Config
from sqlalchemy import select

from l5_services.adapters.transform.artifacts import TransformArtifacts
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.qualification_driver import TransformQualificationCollector
from l5_services.adapters.transform.qualification_evaluator import TransformQualificationEvaluator
from l5_services.adapters.transform.qualification_readback import RetainedTransformObserver
from l5_services.adapters.transform.qualification_source import RetainedProductSource, TransformQualificationAuthority
from l5_services.adapters.transform.qualification_bootstrap import BootstrapIncomplete, prepare, validate_bootstrap
from l5_services.models.attempt import WorkerAttempt
from l5_services.models.run import Run
from l5_services.work_store import TRANSFORM_SUBMIT_OPERATION, AttemptContext


RUNTIME_KEYS = {"L5_GATEWAY_URL", "L5_QUALIFICATION_API_TOKEN", "AWS_REGION", "L5_TRANSFORM_ARTIFACT_BUCKET",
                "L5_RDS_HOST", "L5_RDS_PORT", "L5_RDS_DATABASE", "L5_RDS_USER"}


def load_fixture(source_sha, path):
    path = Path(path)
    if (not re.fullmatch(r"[a-f0-9]{40}", source_sha) or path.is_symlink()
            or not path.is_file() or path.stat().st_mode & 0o077 or path.stat().st_size > 262144):
        raise ValueError("Select a reviewed revision and private bounded fixture")
    fixture = json.loads(path.read_bytes())
    if (not isinstance(fixture, dict) or set(fixture) - {"source_sha", "runs", "bootstrap", "runtime", "release_acceptance"}
            or fixture.get("source_sha") != source_sha or ("runs" in fixture) == ("bootstrap" in fixture)):
        raise ValueError("Transform fixture belongs to another revision or has no selected runs")
    if not isinstance(fixture.get("runtime", {}), dict) or set(fixture.get("runtime", {})) - RUNTIME_KEYS:
        raise ValueError("Transform fixture contains unsupported runtime configuration")
    if "bootstrap" in fixture:
        validate_bootstrap(fixture["bootstrap"])
        return fixture
    if not isinstance(fixture["runs"], list) or not 1 <= len(fixture["runs"]) <= 10:
        raise ValueError("Transform fixture requires one to ten selected runs")
    seen = set()
    for selected in fixture["runs"]:
        fields = {"tenant_id", "product_id", "project_id", "run_id", "result_id", "result_revision"}
        if (not isinstance(selected, dict) or set(selected) != fields
                or any(not isinstance(selected[key], str) or not selected[key]
                       or len(selected[key]) > 255 or any(character in selected[key] for character in "/?#\r\n")
                       for key in fields - {"result_revision"})
                or type(selected["result_revision"]) is not int or selected["result_revision"] < 1):
            raise ValueError("Transform fixture requires exact Product run and result identities")
        identity = selected["tenant_id"], selected["run_id"]
        if identity in seen:
            raise ValueError("Transform fixture repeats a run")
        seen.add(identity)
    return fixture


async def read_run(sessions, selected):
    async with sessions() as session:
        run = await session.scalar(select(Run).where(
            Run.tenant_id == selected["tenant_id"], Run.run_id == selected["run_id"],
            Run.product_id == selected["product_id"], Run.project_id == selected["project_id"],
            Run.service_id == "transform"))
        attempt = await session.scalar(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == selected["tenant_id"], WorkerAttempt.run_id == selected["run_id"],
            WorkerAttempt.operation == TRANSFORM_SUBMIT_OPERATION))
        if run is None or attempt is None or not run.native_job_ref or attempt.native_job_ref != run.native_job_ref:
            raise ValueError("Transform durable run and original provider attempt are unavailable")
        context = AttemptContext(attempt_id=attempt.attempt_id, run_id=attempt.run_id,
            work_id=attempt.first_work_id, fence=attempt.first_fence, idempotency_token=attempt.idempotency_token,
            input_hash=attempt.input_hash, started_at=attempt.started_at.replace(tzinfo=attempt.started_at.tzinfo or UTC),
            can_reconcile=False)
        return run, context


async def qualify(fixture, sessions, client, artifacts, *, clock=None, on_verified_case=None, on_retained_case=None):
    clock = clock or (lambda: datetime.now(UTC))
    observer = RetainedTransformObserver(artifacts)
    reports = []
    for selected in fixture["runs"]:
        run, attempt = await read_run(sessions, selected)
        scope = run.admission.get("scope")
        if not isinstance(scope, dict) or any(scope.get(key) != selected[key] for key in ("tenant_id", "product_id", "project_id")):
            raise ValueError("Transform durable admission differs from selected Product scope")
        source = RetainedProductSource(client, scope, run.run_id)
        authority = TransformQualificationAuthority(run, source, clock=clock)
        files = await source.read_sealed_source(manifest=run.manifest, authority=authority)
        root = (f"/v1/products/{quote(scope['product_id'], safe='')}/projects/"
                f"{quote(scope['project_id'], safe='')}/services/transform")
        response = await client.get(root + "/results/" + quote(selected["result_id"], safe=""), follow_redirects=False)
        response.raise_for_status()
        result = response.json()
        reference = {key: result["original_patch"][key] for key in (
            "tenant_id", "project_id", "artifact_id", "version", "sha256", "bytes", "media_type")}
        intent = TransformAdapter(observer)._intent(run, attempt)
        await observer.verify_checks(intent=intent, run=run, result=result, base_files=files, authority=authority)
        async def verified(selected_run, selected_attempt, current_result, evaluated):
            if current_result != result:
                raise ValueError("Transform result changed after independent scope, dependency and check verification")
            if on_verified_case is not None:
                await on_verified_case(selected_run, selected_attempt, current_result, evaluated)
        evaluator = TransformQualificationEvaluator(TransformQualificationCollector(source, observer), client, observer,
                                                     clock=clock, on_verified=verified)
        evaluated = await evaluator.evaluate(run, attempt, selected["result_id"], selected["result_revision"], reference, authority)
        reports.append(evaluated)
        if evaluated["status"] == "evaluated" and on_retained_case is not None:
            await on_retained_case(run=run, attempt=attempt, result=result, authority=authority,
                                   observer=observer, intent=intent, files=files, client=client, sessions=sessions)
    return {"schema": "transform-release-qualification/v1", "source_revision": fixture["source_sha"],
            "status": "evaluated" if all(report["status"] == "evaluated" for report in reports) else "unresolved",
            "live_accepted": False, "runs": reports}


def evidence_path(directory, name):
    directory = Path(directory)
    if (not directory.is_absolute() or directory.is_symlink() or not directory.is_dir()
            or directory.stat().st_mode & 0o077 or directory.resolve().is_relative_to(Path.cwd().resolve())):
        raise ValueError("Transform evidence directory must be private and outside the checkout")
    return directory / name


async def run(source_sha, fixture_path, directory, *, phase, on_verified_case=None, on_selected=None,
              output_name=None, on_retained_case=None):
    from l5_services import runtime_support as resources

    fixture = load_fixture(source_sha, fixture_path)
    output = evidence_path(directory, output_name or ("qualification.json" if phase == "emit" else "observations.json"))
    configuration = {key: fixture.get("runtime", {}).get(key, os.environ.get(key)) for key in RUNTIME_KEYS}
    if any(not isinstance(value, str) or not value or "\n" in value or "\r" in value for value in configuration.values()):
        raise ValueError("Transform qualification private runtime configuration is incomplete")
    if not configuration["L5_RDS_PORT"].isdigit() or not 1 <= int(configuration["L5_RDS_PORT"]) <= 65535:
        raise ValueError("Transform qualification database port is invalid")
    gateway = configuration["L5_GATEWAY_URL"]
    parsed = urlsplit(gateway)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("Select the protected HTTPS Gateway origin")
    if phase == "preflight":
        return {"status": "configured", "live_accepted": False}
    token = configuration["L5_QUALIFICATION_API_TOKEN"]
    database_keys = {key for key in RUNTIME_KEYS if key.startswith("L5_RDS_")} | {"AWS_REGION"}
    previous = {key: os.environ.get(key) for key in database_keys}
    try:
        os.environ.update({key: configuration[key] for key in database_keys})
        sessions = resources.sessions()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    artifacts = TransformArtifacts(boto3.client("s3", region_name=configuration["AWS_REGION"],
        config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 2})),
        configuration["L5_TRANSFORM_ARTIFACT_BUCKET"])
    try:
        async with httpx.AsyncClient(base_url=gateway, headers={"Authorization": "Bearer " + token},
                                     timeout=10, follow_redirects=False) as client:
            if "bootstrap" in fixture:
                fixture = await prepare(fixture, client, output.parent, allow_submit=phase == "run", on_selected=on_selected)
            report = await asyncio.wait_for(qualify(fixture, sessions, client, artifacts,
                                                    on_verified_case=on_verified_case, on_retained_case=on_retained_case), timeout=180)
    finally:
        await sessions.kw["bind"].dispose()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(json.dumps(report, sort_keys=True, indent=2) + "\n")
    if report["status"] != "evaluated":
        raise ValueError("Transform qualification has unresolved observations")
    return report


def main(*, emitter=False):
    parser = argparse.ArgumentParser(description=__doc__)
    if not emitter:
        parser.add_argument("phase", choices=("preflight", "run"))
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments.source_sha, arguments.fixture_path, arguments.evidence_dir,
                        phase="emit" if emitter else arguments.phase))
    except BootstrapIncomplete as error:
        raise SystemExit(str(error)) from None
    except httpx.HTTPStatusError as error:
        action = {401: "Refresh the dedicated qualification session.",
                  403: "Restore current Product and source authorization for the qualification identity.",
                  404: "Verify the selected Product, project, run and installed service routes.",
                  409: "Reconcile the original request, binding and source revisions.",
                  412: "Re-read the selected configuration and preserve the existing run identity.",
                  422: "Validate the selected fixture against the installed request contract.",
                  503: "Complete executor qualification and installation before native submission."}
        raise SystemExit(f"Transform qualification HTTP {error.response.status_code}: " +
                         action.get(error.response.status_code, "Inspect the protected Gateway logs for this request.")) from None
    except Exception:
        raise SystemExit("Transform qualification failed; no native acceptance was claimed") from None
    print("Transform configuration validated; no readbacks performed" if not emitter and arguments.phase == "preflight"
          else "Transform observations evaluated; native acceptance remains separate")
