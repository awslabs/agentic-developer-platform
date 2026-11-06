"""Product-scoped static code review against a sealed repository ZIP."""

import asyncio
import base64
import json
from datetime import UTC, datetime
from hashlib import sha256

from botocore.exceptions import ClientError

from l5_services.adapters.protocol import AdapterOutcome
from l5_services.security_registration import bucket_name
from l5_services.source_archive import verify_review_event, verify_review_selection
from l5_services.source_manifest import SourceIntegrityError, verify_archive_file


def _selection(run, manifest, provider_session):
    verify_review_selection(run)
    verify_review_event(run)
    if set(run.input) & {"validationMode", "codeRemediationStrategy", "validation_mode",
                         "code_remediation_strategy", "endpoint_testing", "remediation"}:
        raise SourceIntegrityError("code review cannot enable validation or remediation")
    admission = run.admission
    scope = admission["scope"]
    space = admission.get("native_space_ref") or {}
    identity = admission.get("provider_identity") or {}
    registration = admission.get("source_registration") or {}
    if (run.service_id != "security" or run.input.get("capability") != "code_review"
            or (run.tenant_id, run.product_id, run.project_id) != (
                scope.get("tenant_id"), scope.get("product_id"), scope.get("project_id"))
            or (space.get("tenant_id"), space.get("project_id")) != (run.tenant_id, run.project_id)
            or space.get("record_type") != "security_space" or not space.get("record_id")
            or space.get("source_connection_id") != admission["connection_ref"]["source_connection_id"]
            or any(getattr(provider_session, field) != identity.get(field)
                   for field in ("account_id", "role_arn", "region"))
            or provider_session.region != "us-east-1"
            or not registration.get("bucket") or not registration.get("service_role")
            or manifest.get("scope") != scope or manifest.get("sources") != run.input["sources"]
            or manifest.get("run_id") != run.run_id
            or manifest.get("configuration_revision") != run.configuration_revision
            or manifest.get("binding_generation") != run.binding_generation
            or manifest.get("source_snapshot_ids") != [
                snapshot["snapshot_id"] for snapshot in admission["source_snapshots"]]
            or manifest.get("coverage_mode") != "full_snapshot"):
        raise SourceIntegrityError("code review does not match pinned source or provider scope")
    return space, registration, provider_session.client("securityagent")


def _attempt_key(run, manifest, attempt):
    expected_hash = sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    expected_token = sha256(f"{run.tenant_id}:{run.run_id}:{expected_hash}".encode()).hexdigest()
    if (attempt.run_id != run.run_id or attempt.input_hash != expected_hash
            or attempt.idempotency_token != expected_token):
        raise ValueError("code review attempt does not match sealed manifest")
    return f"security-agent/source/{expected_token}.zip"


def source_location_path(path, files):
    """Map only the provider's known checkout prefix onto a sealed source file."""
    import re

    if not isinstance(path, str):
        raise ValueError("native finding lacks a source path")
    relative = re.sub(r"^/tmp/opt/source_code/code_sc-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/", "", path)
    if relative not in files or any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("native finding cites an unverified file")
    return relative


def _native_ref(run, job_id, review_id):
    return {"tenant_id": run.tenant_id, "project_id": run.project_id,
            "source_connection_id": run.admission["connection_ref"]["source_connection_id"],
            "record_type": "native_job", "record_id": job_id, "record_revision": review_id}


def _pages(fetch, key, **parameters):
    token = None
    seen = set()
    while True:
        page = fetch(**parameters, **({"nextToken": token} if token else {}))
        if not isinstance(page.get(key), list):
            raise ValueError("incomplete native pagination")
        yield from page[key]
        token = page.get("nextToken")
        if not token:
            return
        if token in seen or len(seen) >= 100:
            raise ValueError("unbounded native pagination")
        seen.add(token)


def _job(client, space_id, native_ref):
    response = client.batch_get_code_review_jobs(
        agentSpaceId=space_id, codeReviewJobIds=[native_ref["record_id"]])
    jobs = response.get("codeReviewJobs") or []
    if (len(jobs) != 1 or jobs[0].get("codeReviewJobId") != native_ref["record_id"]
            or jobs[0].get("codeReviewId") != native_ref["record_revision"]):
        return None
    return jobs[0]


def _instant(value):
    return value.isoformat() if isinstance(value, datetime) else None


def _progress(client, space_id, native_ref, job):
    steps = job.get("steps")
    phases = ([{"name": step.get("name"), "status": step.get("status"),
                "created_at": _instant(step.get("createdAt")),
                "updated_at": _instant(step.get("updatedAt"))} for step in steps]
              if isinstance(steps, list) else [])
    tasks = []
    coverage = "complete" if isinstance(steps, list) else "unavailable"
    try:
        summaries = _pages(client.list_code_review_job_tasks, "codeReviewJobTaskSummaries",
                           agentSpaceId=space_id, codeReviewJobId=native_ref["record_id"], maxResults=100)
        for task in summaries:
            if (len(tasks) >= 500 or task.get("codeReviewJobId") != native_ref["record_id"]
                    or task.get("codeReviewId") != native_ref["record_revision"]
                    or task.get("agentSpaceId") != space_id or not task.get("taskId")):
                raise ValueError("incomplete scoped code review tasks")
            tasks.append({"task_id": task["taskId"], "status": task.get("executionStatus"),
                          "created_at": _instant(task.get("createdAt")),
                          "updated_at": _instant(task.get("updatedAt"))})
        if len({task["task_id"] for task in tasks}) != len(tasks):
            raise ValueError("duplicate code review tasks")
    except (ClientError, ConnectionError, TimeoutError, ValueError):
        coverage = "partial" if phases or tasks else "unavailable"
    if any(phase["name"] not in {"PREFLIGHT", "STATIC_ANALYSIS", "FINALIZING"}
           or phase["status"] not in {"NOT_STARTED", "IN_PROGRESS", "COMPLETED", "FAILED", "STOPPED"}
           for phase in phases) or any(task["status"] not in {
               "IN_PROGRESS", "ABORTED", "COMPLETED", "INTERNAL_ERROR", "FAILED"} for task in tasks):
        coverage = "partial"
    if not phases and not tasks:
        coverage = "unavailable"
    fingerprint = None
    if coverage == "complete":
        fingerprint = sha256(json.dumps({"status": job.get("status"),
                                        "phases": sorted((phase["name"], phase["status"])
                                                         for phase in phases),
                                        "tasks": sorted((task["task_id"], task["status"])
                                                        for task in tasks)}, sort_keys=True).encode()).hexdigest()
    return phases, tasks, coverage, fingerprint


def _stage_archive(s3, bucket, key, path, reference):
    """Create one immutable native input; retry collisions require byte verification."""
    with open(path, "rb") as content:
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=content, ContentLength=reference["bytes"],
                          IfNoneMatch="*", Metadata={"sha256": reference["sha256"]},
                          ChecksumSHA256=base64.b64encode(bytes.fromhex(reference["sha256"])).decode())
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
                raise
    head = s3.head_object(Bucket=bucket, Key=key)
    if (head.get("ContentLength") != reference["bytes"]
            or (head.get("Metadata") or {}).get("sha256") != reference["sha256"]):
        raise SourceIntegrityError("staged archive integrity unverified")
    version = head.get("VersionId")
    selection = {"VersionId": version} if version and version != "null" else {}
    if not selection:
        if not head.get("ETag"):
            raise SourceIntegrityError("staged archive identity unverified")
        selection = {"IfMatch": head["ETag"]}
    stored = s3.get_object(Bucket=bucket, Key=key, **selection)
    body = stored["Body"]
    try:
        if (stored.get("ContentLength") != reference["bytes"]
                or ("VersionId" in selection and stored.get("VersionId") != version)):
            raise SourceIntegrityError("staged archive identity changed")
        digest, size = sha256(), 0
        while chunk := body.read(64 * 1024):
            size += len(chunk)
            if size > reference["bytes"]:
                raise SourceIntegrityError("staged archive exceeds receipt")
            digest.update(chunk)
        if size != reference["bytes"] or digest.hexdigest() != reference["sha256"]:
            raise SourceIntegrityError("staged archive content changed")
    finally:
        body.close()


class CodeReviewAdapter:
    def __init__(self, archive_store, *, clock=None):
        self.archive_store = archive_store
        self.clock = clock or (lambda: datetime.now(UTC))

    async def submit(self, run, manifest, attempt, provider_session):
        space, registration, client = _selection(run, manifest, provider_session)
        key = _attempt_key(run, manifest, attempt)
        space_id = space["record_id"]
        current = await asyncio.to_thread(client.batch_get_agent_spaces, agentSpaceIds=[space_id])
        spaces = current.get("agentSpaces") or []
        if (len(spaces) != 1 or spaces[0].get("agentSpaceId") != space_id
                or not spaces[0].get("name") or not spaces[0].get("createdAt")
                or spaces[0]["createdAt"].isoformat() != space["record_revision"]):
            raise ValueError("source registration space identity unverified")
        resources = spaces[0].get("awsResources") or {}
        roles = list(resources.get("iamRoles") or [])
        buckets = list(resources.get("s3Buckets") or [])
        role = registration["service_role"]
        bucket = registration["bucket"]
        if role not in roles:
            roles.append(role)
        bucket_arn = f"arn:aws:s3:::{bucket}"
        if not any(bucket_name(value) == bucket for value in buckets):
            buckets.append(bucket_arn)
        try:
            updated = await asyncio.to_thread(
                client.update_agent_space, agentSpaceId=space_id, name=spaces[0]["name"],
                awsResources={**resources, "iamRoles": roles, "s3Buckets": buckets},
                codeReviewSettings=spaces[0].get("codeReviewSettings") or {
                    "controlsScanning": True, "generalPurposeScanning": True})
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {
                "AccessDenied", "AccessDeniedException", "ValidationException", "ResourceNotFoundException"
            }:
                return AdapterOutcome("definitive_failure")
            return AdapterOutcome("unknown")
        if not isinstance(updated, dict) or updated.get("agentSpaceId") != space_id:
            return AdapterOutcome("unknown")
        reference = manifest["archive"]
        async with self.archive_store.get_archive_file(run.run_id, reference) as archived:
            await asyncio.to_thread(verify_archive_file, archived, reference,
                                    run.capture_limits["max_bytes"] + 64 * 1024 * 1024)
            with open(archived, "rb") as content:
                if content.read(2) != b"PK":
                    raise SourceIntegrityError("source archive is not a ZIP")
            try:
                await asyncio.to_thread(_stage_archive, provider_session.client("s3"),
                                        bucket, key, archived, reference)
            except ClientError as error:
                code = error.response.get("Error", {}).get("Code")
                if code not in {"AccessDenied", "AccessDeniedException", "NoSuchBucket", "NoSuchKey", "NoSuchVersion"}:
                    raise
                # The immutable upload is preparatory: CreateCodeReview and
                # StartCodeReviewJob have not been called. Do not reconcile a
                # nonexistent assessment indefinitely after a known rejection.
                return AdapterOutcome("definitive_failure", observation={
                    "code": "source_archive_unavailable", "stage": "verify_source_archive", "cause": code,
                    "next_action": "Verify the Product source bucket and grant the connection role "
                                   "s3:PutObject, s3:GetObject and s3:GetObjectVersion on its source prefix; "
                                   "then start a new run. No assessment was started.",
                })
        review = await asyncio.to_thread(
            client.create_code_review, title=f"adp-{run.run_id}", agentSpaceId=space_id,
            assets={"sourceCode": [{"s3Location": f"s3://{bucket}/{key}"}]},
            serviceRole=role, validationMode="DISABLED", codeRemediationStrategy="DISABLED")
        review_id = review.get("codeReviewId")
        if not review_id:
            return AdapterOutcome("unknown")
        response = await asyncio.to_thread(client.start_code_review_job,
                                           agentSpaceId=space_id, codeReviewId=review_id)
        job_id = response.get("codeReviewJobId")
        if not job_id:
            return AdapterOutcome("unknown")
        native = _native_ref(run, job_id, review_id)
        job = await asyncio.to_thread(_job, client, space_id, native)
        if job is None:
            return AdapterOutcome("unknown")
        return AdapterOutcome("accepted", native_job_ref=native)

    async def reconcile_submit(self, run, attempt, provider_session):
        space, registration, client = _selection(run, run.manifest, provider_session)
        key = _attempt_key(run, run.manifest, attempt)
        source_location = f"s3://{registration['bucket']}/{key}"
        space_id = space["record_id"]

        def lookup():
            reviews = list(_pages(client.list_code_reviews, "codeReviewSummaries", agentSpaceId=space_id))
            matches = [review for review in reviews if review.get("title") == f"adp-{run.run_id}"
                       and review.get("agentSpaceId") == space_id and review.get("codeReviewId")]
            if len(matches) != 1:
                return None
            review_id = matches[0]["codeReviewId"]
            readback = client.batch_get_code_reviews(agentSpaceId=space_id, codeReviewIds=[review_id])
            records = readback.get("codeReviews") or []
            if len(records) != 1:
                return None
            review = records[0]
            assets = review.get("assets") or {}
            if (review.get("codeReviewId") != review_id or review.get("agentSpaceId") != space_id
                    or review.get("title") != f"adp-{run.run_id}"
                    or assets.get("sourceCode") != [{"s3Location": source_location}]
                    or any(assets.get(field) for field in (
                        "endpoints", "actors", "documents", "integratedRepositories", "trustedCaCertificates"))
                    or review.get("serviceRole") != registration["service_role"]
                    or review.get("validationMode") != "DISABLED"
                    or review.get("codeRemediationStrategy") != "DISABLED"):
                return None
            jobs = list(_pages(client.list_code_review_jobs_for_code_review, "codeReviewJobSummaries",
                               agentSpaceId=space_id, codeReviewId=review_id))
            if (len(jobs) != 1 or not jobs[0].get("codeReviewJobId")
                    or jobs[0].get("codeReviewId") != review_id):
                return None
            native = _native_ref(run, jobs[0]["codeReviewJobId"], review_id)
            job = _job(client, space_id, native)
            if (job is None or job.get("sourceCode") not in (None, [{"s3Location": source_location}])
                    or job.get("codeRemediationStrategy") not in (None, "DISABLED")):
                return None
            return native

        native = await asyncio.to_thread(lookup)
        return AdapterOutcome("accepted", native_job_ref=native) if native else AdapterOutcome("unknown")

    async def observe(self, run, native_job_ref, attempt, provider_session):
        space, _, client = _selection(run, run.manifest, provider_session)
        if native_job_ref != _native_ref(run, native_job_ref["record_id"], native_job_ref["record_revision"]):
            raise ValueError("job does not match pinned Product scope")
        job = await asyncio.to_thread(_job, client, space["record_id"], native_job_ref)
        if job is None:
            return AdapterOutcome("unknown")
        state = job.get("status")
        classification = ("completed" if state == "COMPLETED" else "failed" if state == "FAILED"
                          else "stopped" if state == "STOPPED"
                          else "running" if state in {"IN_PROGRESS", "STOPPING"} else "unknown")
        phases, tasks, coverage, fingerprint = await asyncio.to_thread(
            _progress, client, space["record_id"], native_job_ref, job)
        if any(phase["name"] in {"VALIDATION", "PENTEST"} for phase in phases):
            classification = "unknown"
        error_code = (job.get("errorInformation") or {}).get("code")
        return AdapterOutcome(classification, observation={"native_job_ref": native_job_ref,
                              "observed_at": self.clock().isoformat(), "outcome": classification,
                              "progress_coverage": coverage, "progress_fingerprint": fingerprint,
                              "status": state, "created_at": _instant(job.get("createdAt")),
                              "updated_at": _instant(job.get("updatedAt")),
                              "phases": phases, "tasks": tasks,
                              "error_information": {"code": error_code} if error_code in {
                                  "CLIENT_ERROR", "INTERNAL_ERROR", "STOPPED_BY_USER"} else None})

    async def collect(self, run, native_job_ref, attempt, provider_session):
        observed = await self.observe(run, native_job_ref, attempt, provider_session)
        if observed.classification != "completed":
            return observed
        space_id = run.admission["native_space_ref"]["record_id"]
        client = provider_session.client("securityagent")

        def read_findings():
            summaries = list(_pages(client.list_findings, "findingsSummaries",
                                    agentSpaceId=space_id, codeReviewJobId=native_job_ref["record_id"]))
            ids = [summary.get("findingId") for summary in summaries]
            if any(not finding_id or summary.get("codeReviewJobId") != native_job_ref["record_id"]
                   for finding_id, summary in zip(ids, summaries, strict=True)) or len(set(ids)) != len(ids):
                raise ValueError("finding summary does not match selected job")
            findings = []
            for start in range(0, len(ids), 50):
                page = client.batch_get_findings(agentSpaceId=space_id, findingIds=ids[start:start + 50])
                findings.extend(page.get("findings") or [])
            if {item.get("findingId") for item in findings} != set(ids) or len(findings) != len(ids):
                raise ValueError("native finding readback incomplete")
            return findings

        native_findings = await asyncio.to_thread(read_findings)
        sources = run.input["sources"]
        files = {
            (f"{sources.index(entry['source_ref'])}/{entry['path']}" if len(sources) > 1 else entry["path"]):
            entry["source_ref"] for entry in run.manifest["files"]
        }
        converted = []
        for item in native_findings:
            if (item.get("codeReviewJobId") != native_job_ref["record_id"]
                    or item.get("agentSpaceId") != space_id or not item.get("codeLocations")):
                raise ValueError("native finding lacks scoped source locations")
            locations = [{**location, "filePath": source_location_path(location.get("filePath"), files)}
                         for location in item["codeLocations"]]
            if any(("lineStart" in location and (type(location["lineStart"]) is not int or location["lineStart"] < 1))
                   or ("lineEnd" in location and (type(location["lineEnd"]) is not int
                       or "lineStart" not in location or location["lineEnd"] < location["lineStart"]))
                   for location in locations):
                raise ValueError("native finding cites an unverified file")
            refs = list({json.dumps(files[location["filePath"]], sort_keys=True)
                         for location in locations})
            converted.append({"native_id": item["findingId"], "kind": "static_finding",
                              "claim": item.get("description") or item.get("name") or "Static finding",
                              "provider_severity": item.get("riskLevel") or "unknown",
                              "source_refs": [json.loads(value) for value in refs],
                              "locations": locations, "native_locations": item["codeLocations"], "evidence": []})
        result = {"native_job_ref": native_job_ref, "findings": converted,
                  "finding_count": len(converted), "coverage": "complete",
                  "coverage_mode": run.manifest["coverage_mode"]}
        return AdapterOutcome("completed", observation=observed.observation, result=result)

    async def cancel(self, run, native_job_ref, attempt, provider_session):
        space, _, client = _selection(run, run.manifest, provider_session)
        await asyncio.to_thread(client.stop_code_review_job, agentSpaceId=space["record_id"],
                                codeReviewJobId=native_job_ref["record_id"])
        return AdapterOutcome("stopped")
