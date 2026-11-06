"""Integration fixtures for exact Product-scoped code-review sources."""

import io
import json
import stat
import zipfile
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from l5_services.adapters.security.code_review import CodeReviewAdapter
from l5_services.source_archive import verify_review_selection
from l5_services.source_manifest import SourceIntegrityError, materialize_inputs


NOW = datetime(2026, 1, 1, tzinfo=UTC)


def artifact(content):
    return {"artifact_id": "artifact", "version": "1", "sha256": sha256(content).hexdigest(),
            "media_type": "application/zip", "bytes": len(content)}


class FileArchiveFixture:
    async def put_archive_file(self, run_id, path):
        return await self.put_archive(run_id, Path(path).read_bytes())

    @asynccontextmanager
    async def get_archive_file(self, run_id, reference):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.zip"
            path.write_bytes(await self.get_archive(run_id, reference))
            yield path


@pytest.mark.parametrize("revisions", [("base-commit", "head-commit"), ("one-commit",)])
async def test_review_selection_seals_exact_archive(revisions):
    sources = [{"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
                "record_type": "repository_commit", "record_id": "repo", "record_revision": revision}
               for revision in revisions]
    snapshots = []
    contents = {}
    manifests = {}
    for index, source in enumerate(sources):
        source_file = f"revision-{index}".encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("src/", "")
            archive.writestr("src/main.py", source_file)
        content = output.getvalue()
        entry = {"source_ref": source, "path": "src/main.py", "sha256": sha256(source_file).hexdigest(),
                 "bytes": len(source_file)}
        manifest = json.dumps({"source_ref": source, "files": [entry], "exclusions": []}).encode()
        snapshot_id = f"snapshot-{index}"
        snapshots.append({"snapshot_id": snapshot_id, "source_ref": source, "content": artifact(content),
                          "file_manifest": artifact(manifest), "file_count": 1, "exclusions": [],
                          "expires_at": (NOW + timedelta(hours=1)).isoformat()})
        contents[snapshot_id] = content
        manifests[snapshot_id] = manifest

    class SourceClient:
        async def read_source(self, _, __, snapshot_id, request):
            assert request["operation_id"] == "run-one"
            return next(snapshot for snapshot in snapshots if snapshot["snapshot_id"] == snapshot_id)

        async def read_source_manifest(self, _, __, snapshot_id, request):
            return manifests[snapshot_id]

        async def stream_source(self, _, __, snapshot_id, request):
            yield contents[snapshot_id]

    staged = []

    class ArchiveStore(FileArchiveFixture):
        async def put_archive(self, run_id, content):
            assert run_id == "run-one"
            staged.append(content)
            return artifact(content)

    run = SimpleNamespace(service_id="security", tenant_id="tenant-one", product_id="product-one", project_id="project-one",
                          run_id="run-one", configuration_revision=1, binding_generation=1,
                          input={"capability": "code_review", "sources": sources,
                                 **({"pr_base": revisions[0], "pr_head": revisions[1]} if len(sources) == 2 else {})},
                          admission={"source_snapshots": snapshots, "exclusions": [], "scope": {
                              "tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"},
                                     "delegation_id": "grant", "delegation_version": 1,
                                     "configuration_hash": "f" * 64, "profile_ref": sources[0],
                                     "adapter_revision": "review-1"},
                          capture_limits={"max_files": 2, "max_bytes": 1024})
    attempt = SimpleNamespace(work_id="work-one", fence=1)
    verify_review_selection(run)
    manifest, uploaded = await materialize_inputs(run, attempt, SourceClient(),
                                                  archive_store=ArchiveStore(), now=NOW)
    assert uploaded == []
    with zipfile.ZipFile(io.BytesIO(staged[0])) as archive:
        assert {item.filename for item in archive.infolist() if not item.is_dir()} == {
            (f"{index}/" if len(sources) > 1 else "") + "src/main.py" for index in range(len(sources))}
        for index in range(len(sources)):
            name = (f"{index}/" if len(sources) > 1 else "") + "src/main.py"
            assert archive.read(name) == f"revision-{index}".encode()
    assert manifest["sources"] == sources
    assert manifest["source_snapshot_ids"] == [snapshot["snapshot_id"] for snapshot in snapshots]
    assert [entry["sha256"] for entry in manifest["files"]] == [
        sha256(f"revision-{index}".encode()).hexdigest() for index in range(len(sources))]
    assert manifest["coverage_mode"] == "full_snapshot"
    if len(sources) == 1:
        assert manifest["archive"] == snapshots[0]["content"]
    else:
        assert len(manifest["archive"]["sha256"]) == 64


def test_review_selection_does_not_substitute_repository_or_revision():
    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "repository_commit", "record_id": "repo", "record_revision": "base"}
    head = {**source, "record_revision": "head-commit"}
    run = SimpleNamespace(tenant_id="tenant-one", project_id="project-one",
                          input={"sources": [source, head], "pr_base": "base", "pr_head": "head-commit"})
    verify_review_selection(run)
    for changed in ({**head, "record_id": "other-repo"}, {**head, "record_revision": "other-commit"},
                    {**head, "tenant_id": "tenant-two"}):
        run.input["sources"] = [source, changed]
        with pytest.raises(SourceIntegrityError):
            verify_review_selection(run)


@pytest.mark.parametrize("denied,tampered,event_case,override", [
    (False, False, None, None), (True, False, None, None), ("version_read", False, None, None), (False, True, None, None),
    (False, False, "forged", None), (False, False, "valid", None),
    (False, False, None, "validationMode"), (False, False, None, "codeRemediationStrategy")],
    ids=["authorized", "denied_registration", "denied_version_read", "tampered_after_preparation", "forged_event", "valid_event",
         "endpoint_override", "remediation_override"])
@pytest.mark.parametrize("native_buckets", [[], ["fictional-archive"], ["arn:aws:s3:::fictional-archive"]])
async def test_native_job_scoped_findings_and_zero_finding_coverage(denied, tampered, event_case, override, native_buckets):
    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "repository_commit", "record_id": "repo", "record_revision": "commit-one"}
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("src/main.py", "safe source")
    content = zipped.getvalue()
    scope = {"tenant_id": "tenant-one", "project_id": "project-one", "product_id": "product-one"}
    space = {**source, "record_type": "security_space", "record_id": "space",
             "record_revision": NOW.isoformat()}
    identity = {"account_id": "000000000000", "role_arn": "role", "region": "us-east-1"}
    run = SimpleNamespace(service_id="security", tenant_id="tenant-one", product_id="product-one",
                          project_id="project-one", run_id="run-one", configuration_revision=1,
                          binding_generation=1, input={"capability": "code_review", "sources": [source]},
                          admission={"scope": scope, "native_space_ref": space, "provider_identity": identity,
                                     "connection_ref": {"source_connection_id": "conn"},
                                     "source_registration": {"bucket": "fictional-archive", "service_role": "role"},
                                     "source_snapshots": [{"snapshot_id": "snapshot-one"}]},
                          capture_limits={"max_bytes": 1024},
                          manifest={"scope": scope, "run_id": "run-one", "sources": [source],
                                    "configuration_revision": 1, "binding_generation": 1,
                                    "source_snapshot_ids": ["snapshot-one"], "coverage_mode": "full_snapshot",
                                    "files": [{"source_ref": source, "path": "src/main.py"}],
                                    "archive": artifact(content)})
    manifest_hash = sha256(json.dumps(run.manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    token = sha256(f"tenant-one:run-one:{manifest_hash}".encode()).hexdigest()
    attempt = SimpleNamespace(run_id="run-one", input_hash=manifest_hash, idempotency_token=token)
    if event_case is not None:
        run.admission.update(event_id="pr-delivery", event_kind="pull_request",
                             event_source_ref=({**source, "record_id": "unselected-repo"}
                                               if event_case == "forged" else source))

    class ArchiveStore(FileArchiveFixture):
        async def get_archive(self, run_id, archive_ref):
            assert (run_id, archive_ref) == ("run-one", run.manifest["archive"])
            return content + b"changed" if tampered else content

    class S3:
        objects = []

        def put_object(self, **kwargs):
            kwargs["Body"] = kwargs["Body"].read()
            self.objects.append(kwargs)

        def head_object(self, **kwargs):
            assert kwargs["Bucket"] == "fictional-archive"
            return {"ContentLength": len(content), "ETag": "fixture-etag", "Metadata": {"sha256": sha256(content).hexdigest()}}

        def get_object(self, **kwargs):
            if denied == "version_read":
                raise ClientError({"Error": {"Code": "AccessDenied", "Message": "private bucket details"}}, "GetObject")
            return {"ContentLength": len(content), "Body": io.BytesIO(content)}

    class Provider:
        requests = []
        findings = [{"findingId": "finding-one", "codeReviewJobId": "job-one",
                     "agentSpaceId": "space", "description": "Location evidence", "riskLevel": "HIGH",
                     "codeLocations": [{"filePath": "src/main.py", "lineStart": 2, "lineEnd": 4}]},
                    {"findingId": "finding-two", "codeReviewJobId": "job-one",
                     "agentSpaceId": "space", "name": "Other evidence", "riskLevel": "LOW",
                     "codeLocations": [{"filePath": "src/main.py", "lineStart": 5, "lineEnd": 6}]}]

        def batch_get_agent_spaces(self, **kwargs):
            return {"agentSpaces": [{"agentSpaceId": "space", "name": "existing", "createdAt": NOW,
                                     "awsResources": {"iamRoles": [], "s3Buckets": native_buckets}}]}

        def update_agent_space(self, **kwargs):
            self.requests.append(("register", kwargs))
            if denied is True:
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                                  "UpdateAgentSpace")
            return {"agentSpaceId": "space"}

        def create_code_review(self, **kwargs):
            self.requests.append(("create", kwargs))
            return {"codeReviewId": "review-one"}

        def start_code_review_job(self, **kwargs):
            self.requests.append(("start", kwargs))
            return {"codeReviewJobId": "job-one"}

        def batch_get_code_review_jobs(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space", "codeReviewJobIds": ["job-one"]}
            return {"codeReviewJobs": [{"codeReviewJobId": "job-one", "codeReviewId": "review-one",
                                        "status": "COMPLETED", "steps": [
                                            {"name": "STATIC_ANALYSIS", "status": "COMPLETED", "updatedAt": NOW}]}]}

        def list_code_review_job_tasks(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space", "codeReviewJobId": "job-one", "maxResults": 100}
            return {"codeReviewJobTaskSummaries": []}

        def list_findings(self, **kwargs):
            assert kwargs["codeReviewJobId"] == "job-one" and kwargs["agentSpaceId"] == "space"
            if not self.findings:
                return {"findingsSummaries": []}
            index = 1 if kwargs.get("nextToken") else 0
            return {"findingsSummaries": [{"findingId": self.findings[index]["findingId"],
                                           "codeReviewJobId": "job-one"}],
                    **({"nextToken": "more"} if index == 0 else {})}

        def batch_get_findings(self, **kwargs):
            assert kwargs["agentSpaceId"] == "space"
            return {"findings": [item for item in self.findings if item["findingId"] in kwargs["findingIds"]]}

    provider = Provider()
    s3 = S3()

    class Session:
        account_id = "000000000000"
        role_arn = "role"
        region = "us-east-1"

        def client(self, service):
            return provider if service == "securityagent" else s3

    adapter = CodeReviewAdapter(ArchiveStore())
    if override:
        run.input[override] = "ENABLED"
        with pytest.raises(SourceIntegrityError, match="cannot enable"):
            await adapter.submit(run, run.manifest, attempt, Session())
        assert not provider.requests and not s3.objects
        return
    if event_case == "forged":
        with pytest.raises(SourceIntegrityError, match="event does not match"):
            await adapter.submit(run, run.manifest, attempt, Session())
        assert not provider.requests and not s3.objects
        return
    if tampered:
        with pytest.raises(SourceIntegrityError):
            await adapter.submit(run, run.manifest, attempt, Session())
        assert [action for action, _ in provider.requests] == ["register"] and not s3.objects
        return
    submitted = await adapter.submit(run, run.manifest, attempt, Session())
    if denied == "version_read":
        assert submitted.classification == "definitive_failure" and submitted.native_job_ref is None
        assert [action for action, _ in provider.requests] == ["register"] and len(s3.objects) == 1
        assert submitted.observation["stage"] == "verify_source_archive"
        assert "s3:GetObjectVersion" in submitted.observation["next_action"]
        assert "private bucket" not in json.dumps(submitted.observation)
        return
    if denied:
        assert submitted.classification == "definitive_failure" and submitted.native_job_ref is None
        assert [action for action, _ in provider.requests] == ["register"] and not s3.objects
        return
    assert submitted.classification == "accepted"
    assert [action for action, _ in provider.requests] == ["register", "create", "start"]
    assert provider.requests[0][1]["awsResources"]["s3Buckets"] == (native_buckets or ["arn:aws:s3:::fictional-archive"])
    request = provider.requests[1][1]
    assert request["validationMode"] == request["codeRemediationStrategy"] == "DISABLED"
    assert not any(action in {"validate", "remediate"} for action, _ in provider.requests)
    assert request["assets"]["sourceCode"][0]["s3Location"].endswith(f"{token}.zip")
    assert s3.objects[0]["Body"] == content
    observed = await adapter.collect(run, submitted.native_job_ref, attempt, Session())
    assert observed.result["finding_count"] == 2
    assert observed.result["coverage"] == "complete"
    assert all(item["source_refs"] == [source] for item in observed.result["findings"])
    provider.findings[0]["codeLocations"][0]["filePath"] = "/tmp/opt/source_code/code_sc-00000000-0000-0000-0000-000000000000/src/main.py"
    provider.findings[0]["codeLocations"].append({"filePath": "src/main.py", "label": "supporting file"})
    recovered = await adapter.collect(run, submitted.native_job_ref, attempt, Session())
    assert recovered.result["findings"][0]["locations"][0]["filePath"] == "src/main.py"
    assert recovered.result["findings"][0]["native_locations"][0]["filePath"].startswith("/tmp/opt/")
    assert recovered.result["findings"][0]["locations"][1] == {"filePath": "src/main.py", "label": "supporting file"}
    provider.findings = []
    empty = await adapter.collect(run, submitted.native_job_ref, attempt, Session())
    assert empty.result["finding_count"] == 0 and empty.result["coverage"] == "complete"


async def _exercise_exact_source_scan(tmp_path, *, incomplete=False):
    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.findings import ResultStore
    from l5_services.models import Configuration, ConfigurationVersion, Run, Work
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.base import Base
    from l5_services.models.evidence import AssessmentResult, ProviderObservation
    from l5_services.dispatcher import dispatch_work
    from l5_services.run_access import RunStore
    from l5_services.workers.execution import ExecutionService

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'code-review.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    snapshots = {}
    archives = {}
    requests = []
    for suffix in ("a", "b"):
        source = {"tenant_id": f"tenant-{suffix}", "project_id": f"project-{suffix}",
                  "source_connection_id": f"conn-{suffix}", "record_type": "repository_commit",
                  "record_id": f"repo-{suffix}", "record_revision": f"commit-{suffix}"}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("src/main.py", f"source-{suffix}")
        content = output.getvalue()
        file_data = f"source-{suffix}".encode()
        entry = {"source_ref": source, "path": "src/main.py", "sha256": sha256(file_data).hexdigest(),
                 "bytes": len(file_data)}
        listing = json.dumps({"source_ref": source, "files": [entry], "exclusions": []}).encode()
        snapshot_id = f"snapshot-{suffix}"
        receipt = {"snapshot_id": snapshot_id, "source_ref": source, "content": artifact(content),
                   "file_manifest": artifact(listing), "file_count": 1, "exclusions": [],
                   "expires_at": (NOW + timedelta(hours=1)).isoformat()}
        snapshots[suffix] = (receipt, listing, content)
        scope = {"tenant_id": f"tenant-{suffix}", "product_id": f"product-{suffix}",
                 "project_id": f"project-{suffix}", "owner_id": f"owner-{suffix}",
                 "relationship_revision": "1", "visibility": "only_me"}
        space = {**source, "record_type": "security_space", "record_id": f"space-{suffix}",
                 "record_revision": NOW.isoformat()}
        async with sessions() as session:
            session.add(Configuration(tenant_id=scope["tenant_id"], configuration_id=f"config-{suffix}",
                                      product_id=scope["product_id"], project_id=scope["project_id"],
                                      service_id="security", relationship_revision="1", owner_id=scope["owner_id"],
                                      current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id=scope["tenant_id"], configuration_id=f"config-{suffix}",
                                             revision=1, settings={}, inherited_fields=[], gaps=[],
                                             state="ready", hash="f" * 64))
            session.add(Run(tenant_id=scope["tenant_id"], run_id=f"run-{suffix}",
                            product_id=scope["product_id"], project_id=scope["project_id"],
                            service_id="security", configuration_id=f"config-{suffix}",
                            configuration_revision=1, binding_id=f"binding-{suffix}", trigger_id="manual",
                            binding_generation=1, input={"capability": "code_review", "sources": [source]},
                            admission={"scope": scope, "source_snapshots": [receipt], "delegation_id": "grant",
                                       "delegation_version": 1, "exclusions": [], "configuration_hash": "f" * 64,
                                       "profile_ref": source, "adapter_revision": "review-1",
                                       "monitoring_policy": {"poll_interval_seconds": 600,
                                                             **({"stall_after_seconds": 1200} if incomplete else {})},
                                       "native_space_ref": space, "connection_ref": {
                                           "source_connection_id": f"conn-{suffix}"},
                                       "source_registration": {"bucket": "fictional-staging",
                                                               "service_role": "service-role"},
                                       "provider_identity": {"account_id": "000000000000", "role_arn": "role",
                                                             "region": "us-east-1"}},
                            capture_limits={"max_bytes": 1024, "max_files": 1},
                            deadline_at=NOW + timedelta(hours=1)))
            session.add(Work(tenant_id=scope["tenant_id"], work_id=f"work-{suffix}",
                             run_id=f"run-{suffix}", kind="execute", generation=1, step=0, due_at=NOW))
            await session.commit()

    class SourceClient:
        async def read_source(self, product, project, snapshot_id, request):
            suffix = snapshot_id[-1]
            assert (product, project, request["operation_id"]) == (
                f"product-{suffix}", f"project-{suffix}", f"run-{suffix}")
            return snapshots[suffix][0]

        async def read_source_manifest(self, product, project, snapshot_id, request):
            return snapshots[snapshot_id[-1]][1]

        async def stream_source(self, product, project, snapshot_id, request):
            yield snapshots[snapshot_id[-1]][2]

    class ArchiveStore(FileArchiveFixture):
        async def put_archive(self, run_id, content):
            archives[run_id] = content
            return artifact(content)

        async def get_archive(self, run_id, reference):
            return archives[run_id]

    class S3:
        stored = {}

        def put_object(self, **kwargs):
            kwargs["Body"] = kwargs["Body"].read()
            self.stored[kwargs["Key"]] = kwargs

        def head_object(self, **kwargs):
            value = self.stored[kwargs["Key"]]
            return {"ContentLength": len(value["Body"]), "ETag": "fixture-etag", "Metadata": value["Metadata"]}

        def get_object(self, **kwargs):
            value = self.stored[kwargs["Key"]]
            return {"ContentLength": len(value["Body"]), "Body": io.BytesIO(value["Body"])}

    class Provider:
        fail_next_job_read = False
        partial_next_job = False
        fail_next_task_page = False
        fail_next_findings = False
        reviews = {}
        started = []

        def batch_get_agent_spaces(self, **kwargs):
            return {"agentSpaces": [{"agentSpaceId": kwargs["agentSpaceIds"][0],
                                     "name": "existing", "createdAt": NOW}]}

        def update_agent_space(self, **kwargs):
            return {"agentSpaceId": kwargs["agentSpaceId"]}

        def create_code_review(self, **kwargs):
            requests.append(kwargs)
            review_id = kwargs["title"].replace("adp-run", "review")
            self.reviews[review_id] = {"codeReviewId": review_id, "agentSpaceId": kwargs["agentSpaceId"],
                                       **kwargs}
            return {"codeReviewId": review_id}

        def start_code_review_job(self, **kwargs):
            self.started.append(kwargs["codeReviewId"])
            if kwargs["codeReviewId"] == "review-a":
                raise ConnectionError("start response lost after provider effect")
            return {"codeReviewJobId": kwargs["codeReviewId"].replace("review", "job")}

        def list_code_reviews(self, **kwargs):
            assert kwargs["agentSpaceId"] == "space-a"
            if not kwargs.get("nextToken"):
                return {"codeReviewSummaries": [{"title": "unrelated", "codeReviewId": "other",
                                                   "agentSpaceId": "space-a"}], "nextToken": "more"}
            assert kwargs["nextToken"] == "more"
            return {"codeReviewSummaries": [{"title": "adp-run-a", "codeReviewId": "review-a",
                                               "agentSpaceId": "space-a"}]}

        def batch_get_code_reviews(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space-a", "codeReviewIds": ["review-a"]}
            return {"codeReviews": [self.reviews["review-a"]]}

        def list_code_review_jobs_for_code_review(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space-a", "codeReviewId": "review-a"}
            return {"codeReviewJobSummaries": [{"codeReviewId": "review-a", "codeReviewJobId": "job-a"}]}

        def batch_get_code_review_jobs(self, **kwargs):
            if self.fail_next_job_read:
                self.fail_next_job_read = False
                raise ConnectionError("temporary native status read failure")
            partial = self.partial_next_job
            self.partial_next_job = False
            job_id = kwargs["codeReviewJobIds"][0]
            review = self.reviews[job_id.replace("job", "review")]
            return {"codeReviewJobs": [{"codeReviewJobId": job_id, "codeReviewId":
                                         review["codeReviewId"],
                                        "status": "IN_PROGRESS" if partial else "COMPLETED",
                                        "steps": ([{"name": "STATIC_ANALYSIS", "status": "IN_PROGRESS",
                                                   "updatedAt": NOW}] if partial else []),
                                        "sourceCode": review["assets"]["sourceCode"],
                                        "codeRemediationStrategy": review["codeRemediationStrategy"]}]}

        def list_code_review_job_tasks(self, **kwargs):
            assert kwargs["codeReviewJobId"].startswith("job-") and kwargs["maxResults"] == 100
            if self.fail_next_task_page:
                self.fail_next_task_page = False
                raise ConnectionError("task page unavailable")
            return {"codeReviewJobTaskSummaries": []}

        def list_findings(self, **kwargs):
            if self.fail_next_findings:
                self.fail_next_findings = False
                return {"findingsSummaries": None}
            suffix = kwargs["codeReviewJobId"][-1]
            return {"findingsSummaries": ([{"findingId": "finding-a", "codeReviewJobId": "job-a"}]
                                          if suffix == "a" else [])}

        def batch_get_findings(self, **kwargs):
            return {"findings": [{"findingId": "finding-a", "codeReviewJobId": "job-a",
                                  "agentSpaceId": "space-a", "name": "Static finding", "riskLevel": "HIGH",
                                  "codeLocations": [{"filePath": "src/main.py", "lineStart": 1}]}]}

    s3 = S3()
    provider = Provider()

    class Session:
        account_id = "000000000000"
        role_arn = "role"
        region = "us-east-1"

        def client(self, service):
            return provider if service == "securityagent" else s3

    async def authorize(run, claim, operation):
        assert run.tenant_id == claim.tenant_id
        return Session()

    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    clock = {"now": NOW}
    service = ExecutionService(sessions, SourceClient(), CodeReviewAdapter(ArchiveStore(), clock=lambda: clock["now"]),
                               publish, authorize, archive_store=ArchiveStore(), clock=lambda: clock["now"])
    for suffix in ("a", "b"):
        clock["now"] = NOW
        assert await dispatch_work(sessions, f"tenant-{suffix}", f"work-{suffix}", publish, now=NOW)
        for step in range((7 if incomplete else 6) if suffix == "a" else 4):
            if step >= 2:
                clock["now"] = NOW + timedelta(seconds=600 * (step - 1))
            if suffix == "a" and step == 3:
                provider.fail_next_job_read = True
            if suffix == "a" and step == 4 and incomplete:
                provider.partial_next_job = True
                provider.fail_next_task_page = True
            if suffix == "a" and step == 6:
                provider.fail_next_findings = True
            envelope = messages.pop(0)
            if suffix == "a" and step == 6:
                with pytest.raises(ValueError, match="incomplete native pagination"):
                    await service.execute(envelope)
                assert not await service.execute(envelope)
                async with sessions() as session:
                    pending = await session.get(Run, ("tenant-a", "run-a"))
                    owed = (await session.scalars(select(Work).where(
                        Work.tenant_id == "tenant-a", Work.kind == "collect"))).one()
                    assert pending.state == "collecting" and owed.completed_at is None
                    assert not (await session.scalars(select(AssessmentResult).where(
                        AssessmentResult.tenant_id == "tenant-a"))).all()
                    assert len((await session.scalars(select(WorkerAttempt).where(
                        WorkerAttempt.tenant_id == "tenant-a"))).all()) == 1
                assert provider.started == ["review-a"] and len(requests) == 1
                clock["now"] += timedelta(seconds=61)
                assert await service.execute(envelope)
            else:
                assert await service.execute(envelope)
            if suffix == "a" and step == 2:
                async with sessions() as session:
                    pending = await session.get(Run, ("tenant-a", "run-a"))
                    assert pending.state == "running" and pending.native_job_ref["record_id"] == "job-a"
            if suffix == "a" and step == 3:
                async with sessions() as session:
                    pending = await session.get(Run, ("tenant-a", "run-a"))
                    assert pending.state == "running"
                    assert pending.monitoring["state"] == "degraded"
            if suffix == "a" and step == 4 and incomplete:
                async with sessions() as session:
                    pending = await session.get(Run, ("tenant-a", "run-a"))
                    assert pending.state == "running" and pending.provider_state == "running"
                    assert pending.monitoring["progress_state"] == "unknown"
                    assert pending.monitoring["latest_observation"]["progress_coverage"] == "partial"
                    assert "progress_baseline_at" not in pending.monitoring
                    assert not (await session.scalars(select(AssessmentResult).where(
                        AssessmentResult.tenant_id == "tenant-a"))).all()
                    assert provider.started == ["review-a"]
            if suffix == "a" and step == 5 and incomplete:
                async with sessions() as session:
                    pending = await session.get(Run, ("tenant-a", "run-a"))
                    assert pending.state == "collecting" and pending.provider_state == "completed"
                    assert pending.monitoring["progress_state"] == "unknown"
                    assert pending.monitoring["latest_observation"]["progress_coverage"] == "unavailable"
                    assert not (await session.scalars(select(AssessmentResult).where(
                        AssessmentResult.tenant_id == "tenant-a"))).all()
        async with sessions() as session:
            stored = await session.get(Run, (f"tenant-{suffix}", f"run-{suffix}"))
            results = (await session.scalars(select(AssessmentResult).where(
                AssessmentResult.tenant_id == f"tenant-{suffix}"))).all()
            attempts = (await session.scalars(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == f"tenant-{suffix}"))).all()
            history = (await session.scalars(select(ProviderObservation).where(
                ProviderObservation.tenant_id == f"tenant-{suffix}"))).all()
            work_history = (await session.scalars(select(Work).where(
                Work.tenant_id == f"tenant-{suffix}"))).all()
            assert stored.state == "succeeded" and len(results) == len(attempts) == 1
            assert attempts[0].reconcile_checks == (1 if suffix == "a" else 0)
            assert len(history) == (3 if suffix == "a" and incomplete else 2)
            assert len(work_history) == ((7 if incomplete else 6) if suffix == "a" else 4)
            assert all(item.payload["native_job_ref"] == stored.native_job_ref for item in history)
            assert stored.manifest["sources"] == [snapshots[suffix][0]["source_ref"]]
            assert stored.manifest["archive"]["sha256"] == sha256(s3.stored[
                next(key for key in s3.stored if key.endswith(attempts[0].idempotency_token + ".zip"))]["Body"]).hexdigest()
            store = ResultStore(session, [stored])
            projection = await store.get(results[0].result_id)
            findings = await store.findings(results[0].result_id)
            assert projection["finding_count"] == len(findings) == (1 if suffix == "a" else 0)
            assert projection["coverage"] == "complete"
            if findings:
                assert findings[0]["source_refs"] == stored.input["sources"]
            other = RunStore(session, f"tenant-{suffix}", f"product-{suffix}", f"project-{suffix}",
                             "security", f"owner-{suffix}", "1")
            with pytest.raises(HTTPException) as error:
                await other.get("run-b" if suffix == "a" else "run-a")
            assert error.value.status_code == 404
    async with sessions() as session:
        run_a = await session.get(Run, ("tenant-a", "run-a"))
        run_b = await session.get(Run, ("tenant-b", "run-b"))
        result_a = (await session.scalars(select(AssessmentResult).where(
            AssessmentResult.tenant_id == "tenant-a"))).one()
        result_b = (await session.scalars(select(AssessmentResult).where(
            AssessmentResult.tenant_id == "tenant-b"))).one()
        for store, foreign in ((ResultStore(session, [run_a]), result_b),
                               (ResultStore(session, [run_b]), result_a)):
            with pytest.raises(HTTPException) as error:
                await store.get(foreign.result_id)
            assert error.value.status_code == 404
        assert [result_a.payload["native_job_ref"]["record_id"],
                result_b.payload["native_job_ref"]["record_id"]] == ["job-a", "job-b"]
        attempt_a = (await session.scalars(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == "tenant-a"))).one()
        provider.reviews["review-a"]["assets"] = {"sourceCode": [{"s3Location": "s3://foreign/source.zip"}]}
        mismatched = await service.adapter.reconcile_submit(run_a, attempt_a, Session())
        assert mismatched.classification == "unknown" and mismatched.native_job_ref is None
    assert all(archives[f"run-{suffix}"] == snapshots[suffix][2] for suffix in ("a", "b"))
    assert len(requests) == 2 and all(request["validationMode"] == request["codeRemediationStrategy"]
                                      == "DISABLED" for request in requests)
    assert provider.started == ["review-a", "review-b"]
    assert not messages
    await engine.dispose()
    return requests


async def test_exact_source_scan(tmp_path):
    await _exercise_exact_source_scan(tmp_path)


async def test_incomplete_progress_and_collect_retry(tmp_path):
    await _exercise_exact_source_scan(tmp_path, incomplete=True)


async def test_static_only_and_legacy_compatibility(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location
    from pathlib import Path

    product_requests = await _exercise_exact_source_scan(tmp_path, incomplete=True)
    driver = Path(__file__).resolve().parents[4] / ".github/scripts/code_review_request.py"
    spec = spec_from_file_location("nightly_code_review_request", driver)
    assert spec is not None and spec.loader is not None
    legacy = module_from_spec(spec)
    spec.loader.exec_module(legacy)

    calls = []

    class LegacyClient:
        def update_agent_space(self, **kwargs):
            calls.append(("register", kwargs))
            return {"agentSpaceId": kwargs["agentSpaceId"]}

        def create_code_review(self, **kwargs):
            calls.append(("create", kwargs))
            return {"codeReviewId": "legacy-review"}

        def start_code_review_job(self, **kwargs):
            calls.append(("start", kwargs))
            return {"codeReviewJobId": "legacy-job"}

    role = "arn:aws:iam::000000000000:role/adp-fixture-securityagent-nightly"
    profile = {"agent_space": {"existing_id": "legacy-space", "existing_name": "legacy-fixture",
                                "service_role": role, "staging_bucket": "fixture-staging"},
               "code_review": {"pinned_modes": {"validationMode": "DISABLED",
                                                 "codeRemediationStrategy": "DISABLED"},
                               "title_charset": "^[A-Za-z0-9_-]{1,100}$"}}
    client = LegacyClient()
    space_id = legacy.register_agent_space(client, profile)
    title = legacy.nightly_title("2026-01-01", prefix="adp-fixture-nightly")
    source_uri = "s3://fixture-staging/security-agent/source/legacy-fixture.zip"
    review_id = legacy.create_review(client, profile, space_id, title, source_uri)
    job_id = legacy.start_job(client, space_id, review_id)

    assert [action for action, _ in calls] == ["register", "create", "start"]
    assert calls[0][1]["awsResources"] == {"iamRoles": [role],
                                             "s3Buckets": ["arn:aws:s3:::fixture-staging"]}
    legacy_request = calls[1][1]
    assert space_id == "legacy-space" and review_id == "legacy-review" and job_id == "legacy-job"
    assert legacy_request["assets"] == {"sourceCode": [{"s3Location": source_uri}]}
    assert legacy_request["serviceRole"] == role
    assert calls[2][1] == {"agentSpaceId": space_id, "codeReviewId": review_id}
    assert all(set(request) == set(legacy_request) and request["assets"].keys() == {"sourceCode"}
               and request["validationMode"] == request["codeRemediationStrategy"] == "DISABLED"
               for request in product_requests)
    assert legacy_request["validationMode"] == legacy_request["codeRemediationStrategy"] == "DISABLED"


@pytest.mark.parametrize("fault", ["changed", "unsafe", "expansion", "too_many", "device"])
async def test_untrusted_archives_rejected_before_submission(fault):
    source = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "conn",
              "record_type": "repository_commit", "record_id": "repo", "record_revision": "commit-a"}
    path = "../secret" if fault == "unsafe" else "src/main.py"
    file_data = b"x" * (16385 if fault == "expansion" else 8)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if fault == "too_many":
            for index in range(35):
                archive.writestr(f"directory-{index}/", "")
        if fault == "device":
            info = zipfile.ZipInfo(path)
            info.external_attr = (stat.S_IFCHR | 0o644) << 16
            archive.writestr(info, file_data)
        else:
            archive.writestr(path, file_data)
    archived = output.getvalue()
    listing = json.dumps({"source_ref": source, "files": [{"source_ref": source, "path": path,
                         "bytes": len(file_data), "sha256": sha256(file_data).hexdigest()}],
                          "exclusions": []}).encode()
    snapshot = {"snapshot_id": "snapshot-a", "source_ref": source,
                "content": artifact(archived), "file_manifest": artifact(listing),
                "file_count": 1, "exclusions": [], "expires_at": (NOW + timedelta(hours=1)).isoformat()}
    run = SimpleNamespace(service_id="security", tenant_id="tenant-a", product_id="product-a", project_id="project-a",
                          run_id="run-a", configuration_revision=1, binding_generation=1,
                          input={"capability": "code_review", "sources": [source]},
                          admission={"source_snapshots": [snapshot], "scope": {"tenant_id": "tenant-a"},
                                     "exclusions": [], "delegation_id": "grant", "delegation_version": 1,
                                     "configuration_hash": "f" * 64, "profile_ref": source,
                                     "adapter_revision": "review-1"},
                          capture_limits={"max_files": 1, "max_bytes": 16384})

    class SourceClient:
        async def read_source(self, *args):
            return snapshot

        async def read_source_manifest(self, *args):
            return listing

        async def stream_source(self, *args):
            yield archived + b"tampered" if fault == "changed" else archived

    class Store(FileArchiveFixture):
        async def put_archive(self, *args):
            pytest.fail("unverified bytes must not be staged")

    expected = {"changed": "source integrity mismatch", "unsafe": "unsafe source path",
                "expansion": "source file coverage mismatch", "too_many": "too many entries",
                "device": "unsupported archive entry type"}
    with pytest.raises(SourceIntegrityError, match=expected[fault]):
        await materialize_inputs(run, SimpleNamespace(work_id="work", fence=1), SourceClient(),
                                 archive_store=Store(), now=NOW)


async def _exercise_rejected_source(tmp_path, fault, *, two_tenants=False):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from l5_services.dispatcher import dispatch_work
    from l5_services.models import Configuration, ConfigurationVersion, Run, Work
    from l5_services.models.attempt import WorkerAttempt
    from l5_services.models.base import Base
    from l5_services.models.evidence import AssessmentResult
    from l5_services.workers.execution import ExecutionService

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rejection.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    source = {"tenant_id": "tenant-a", "project_id": "project-a", "source_connection_id": "conn",
              "record_type": "repository_commit", "record_id": "repo", "record_revision": "commit-a"}
    output = io.BytesIO()
    path = "../outside" if fault == "unsafe" else "src/main.py"
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(path, "pinned bytes")
    content = output.getvalue()
    file_entry = {"source_ref": source, "path": path, "sha256": sha256(b"pinned bytes").hexdigest(),
                  "bytes": len(b"pinned bytes")}
    listing = json.dumps({"source_ref": source, "files": [file_entry], "exclusions": []}).encode()
    snapshot = {"snapshot_id": "snapshot-a", "source_ref": source, "content": artifact(content),
                "file_manifest": artifact(listing), "file_count": 1, "exclusions": [],
                "expires_at": (NOW + timedelta(hours=1)).isoformat()}
    scope = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-a",
             "owner_id": "owner-a", "relationship_revision": "1", "visibility": "only_me"}
    admission = {"scope": scope, "source_snapshots": [snapshot], "delegation_id": "grant",
                 "delegation_version": 1, "exclusions": [], "configuration_hash": "f" * 64,
                 "profile_ref": source, "adapter_revision": "review-1",
                 "monitoring_policy": {"poll_interval_seconds": 600},
                 "native_space_ref": {**source, "record_type": "security_space", "record_id": "space",
                                      "record_revision": NOW.isoformat()},
                 "connection_ref": {"source_connection_id": "conn"},
                 "source_registration": {"bucket": "fictional-staging", "service_role": "role"},
                 "provider_identity": {"account_id": "000000000000", "role_arn": "role",
                                       "region": "us-east-1"}}
    if fault == "forged_event":
        admission.update(event_kind="pull_request", event_id="pr-event",
                         event_source_ref={**source, "record_id": "foreign-repo"})
    if two_tenants:
        source_b = {**source, "tenant_id": "tenant-b", "project_id": "project-b",
                    "source_connection_id": "conn-b", "record_id": "repo-b", "record_revision": "commit-b"}
        output_b = io.BytesIO()
        with zipfile.ZipFile(output_b, "w") as archive:
            archive.writestr("src/main.py", "separate tenant source")
        content_b = output_b.getvalue()
        listing_b = json.dumps({"source_ref": source_b, "files": [{"source_ref": source_b,
                               "path": "src/main.py", "sha256": sha256(b"separate tenant source").hexdigest(),
                               "bytes": len(b"separate tenant source")}], "exclusions": []}).encode()
        snapshot_b = {"snapshot_id": "snapshot-b", "source_ref": source_b, "content": artifact(content_b),
                      "file_manifest": artifact(listing_b), "file_count": 1, "exclusions": [],
                      "expires_at": (NOW + timedelta(hours=1)).isoformat()}
        scope_b = {**scope, "tenant_id": "tenant-b", "product_id": "product-b",
                   "project_id": "project-b", "owner_id": "owner-b"}
        admission_b = {key: value for key, value in admission.items()
                       if key not in {"event_kind", "event_id", "event_source_ref"}}
        admission_b.update(scope=scope_b, source_snapshots=[snapshot_b], profile_ref=source_b,
                           connection_ref={"source_connection_id": "conn-b"},
                           native_space_ref={**admission["native_space_ref"], "tenant_id": "tenant-b",
                                             "project_id": "project-b", "source_connection_id": "conn-b",
                                             "record_id": "space-b"})
    async with sessions() as session:
        session.add(Configuration(tenant_id="tenant-a", configuration_id="config-a", product_id="product-a",
                                  project_id="project-a", service_id="security", relationship_revision="1",
                                  owner_id="owner-a", current_revision=1, handoff_state="linked"))
        session.add(ConfigurationVersion(tenant_id="tenant-a", configuration_id="config-a", revision=1,
                                         settings={}, inherited_fields=[], gaps=[], state="ready", hash="f" * 64))
        session.add(Run(tenant_id="tenant-a", run_id="run-a", product_id="product-a", project_id="project-a",
                        service_id="security", configuration_id="config-a", configuration_revision=1,
                        binding_id="binding-a", trigger_id="manual", binding_generation=1,
                        input={"capability": "code_review", "sources": [source]}, admission=admission,
                        capture_limits={"max_bytes": 16384, "max_files": 1},
                        deadline_at=NOW + timedelta(hours=1)))
        session.add(Work(tenant_id="tenant-a", work_id="work-a", run_id="run-a", kind="execute",
                         generation=1, step=0, due_at=NOW))
        if two_tenants:
            session.add(Configuration(tenant_id="tenant-b", configuration_id="config-b", product_id="product-b",
                                      project_id="project-b", service_id="security", relationship_revision="1",
                                      owner_id="owner-b", current_revision=1, handoff_state="linked"))
            session.add(ConfigurationVersion(tenant_id="tenant-b", configuration_id="config-b", revision=1,
                                             settings={}, inherited_fields=[], gaps=[], state="ready",
                                             hash="f" * 64))
            session.add(Run(tenant_id="tenant-b", run_id="run-b", product_id="product-b", project_id="project-b",
                            service_id="security", configuration_id="config-b", configuration_revision=1,
                            binding_id="binding-b", trigger_id="manual", binding_generation=1,
                            input={"capability": "code_review", "sources": [source_b]}, admission=admission_b,
                            capture_limits={"max_bytes": 16384, "max_files": 1},
                            deadline_at=NOW + timedelta(hours=1)))
            session.add(Work(tenant_id="tenant-b", work_id="work-b", run_id="run-b", kind="execute",
                             generation=1, step=0, due_at=NOW))
        await session.commit()

    faulted = {"b": False}

    class SourceClient:
        async def read_source(self, product, project, snapshot_id, request):
            if snapshot_id == "snapshot-b":
                assert (product, project, request["operation_id"]) == ("product-b", "project-b", "run-b")
                if not faulted["b"]:
                    faulted["b"] = True
                    raise ConnectionError("temporary source transport failure")
                return snapshot_b
            assert (product, project, snapshot_id) == ("product-a", "project-a", "snapshot-a")
            return snapshot

        async def read_source_manifest(self, *args):
            return listing_b if args[2] == "snapshot-b" else listing

        async def stream_source(self, *args):
            if args[2] == "snapshot-b":
                yield content_b + b"modified"
                return
            yield content + b"tampered" if fault == "changed" else content

    class Store(FileArchiveFixture):
        contents = {}

        async def put_archive(self, run_id, value):
            self.contents[run_id] = value
            return artifact(value)

        async def get_archive(self, run_id, reference):
            return self.contents[run_id]

    calls = []

    class Provider:
        def batch_get_agent_spaces(self, **kwargs):
            calls.append("read")
            return {"agentSpaces": [{"agentSpaceId": "space", "name": "existing", "createdAt": NOW}]}

        def update_agent_space(self, **kwargs):
            calls.append("register")
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
                              "UpdateAgentSpace")

        def create_code_review(self, **kwargs):
            pytest.fail("untrusted input must not create a native code review")

        def start_code_review_job(self, **kwargs):
            pytest.fail("untrusted input must not start a paid job")

    class Session:
        account_id = "000000000000"
        role_arn = "role"
        region = "us-east-1"

        def client(self, service):
            assert service == "securityagent"
            return Provider()

    async def authorize(run, claim, operation):
        assert run.tenant_id == claim.tenant_id and run.run_id == claim.run_id
        return Session()

    messages = []

    async def publish(envelope, delay):
        messages.append(envelope)

    store = Store()
    clock = {"now": NOW}
    worker = ExecutionService(sessions, SourceClient(), CodeReviewAdapter(store, clock=lambda: clock["now"]),
                              publish, authorize, archive_store=store, clock=lambda: clock["now"])
    assert await dispatch_work(sessions, "tenant-a", "work-a", publish, now=NOW)
    assert await worker.execute(messages.pop(0))
    if messages:
        assert await worker.execute(messages.pop(0))
    if two_tenants:
        assert await dispatch_work(sessions, "tenant-b", "work-b", publish, now=NOW)
        envelope = messages.pop(0)
        with pytest.raises(ConnectionError, match="source transport"):
            await worker.execute(envelope)
        async with sessions() as session:
            pending = await session.get(Run, ("tenant-b", "run-b"))
            work_b = await session.get(Work, ("tenant-b", "work-b"))
            assert pending.state == "admitted" and work_b.completed_at is None
            assert not (await session.scalars(select(AssessmentResult))).all()
        clock["now"] = NOW + timedelta(seconds=61)
        assert await worker.execute(envelope)
        assert faulted["b"]
    async with sessions() as session:
        run = await session.get(Run, ("tenant-a", "run-a"))
        works = (await session.scalars(select(Work).where(Work.tenant_id == "tenant-a"))).all()
        attempts = (await session.scalars(select(WorkerAttempt).where(
            WorkerAttempt.tenant_id == "tenant-a"))).all()
        assert run.state == ("failed" if fault == "registration_denied" else "blocked")
        assert run.native_job_ref is None and not (await session.scalars(select(AssessmentResult))).all()
        assert all(work.completed_at is not None for work in works)
        assert len(attempts) == (0 if fault in {"changed", "unsafe"} else 1)
        if attempts:
            assert attempts[0].status == "definitive_failure"
        assert not messages and calls == (["read", "register"] if fault == "registration_denied" else [])
        assert bool(store.contents) == (fault in {"forged_event", "registration_denied"})
        if two_tenants:
            from fastapi import HTTPException

            from l5_services.run_access import RunStore

            run_b = await session.get(Run, ("tenant-b", "run-b"))
            work_b = await session.get(Work, ("tenant-b", "work-b"))
            attempts_b = (await session.scalars(select(WorkerAttempt).where(
                WorkerAttempt.tenant_id == "tenant-b"))).all()
            assert run_b.state == "blocked" and run_b.native_job_ref is None and run_b.manifest is None
            assert run_b.admission["source_snapshots"][0]["source_ref"] == source_b
            assert work_b.completed_at is not None and work_b.fence == 2 and not attempts_b
            for tenant, product, project, actor, foreign_run in (
                ("tenant-a", "product-a", "project-a", "owner-a", "run-b"),
                ("tenant-b", "product-b", "project-b", "owner-b", "run-a")
            ):
                scoped = RunStore(session, tenant, product, project, "security", actor, "1")
                with pytest.raises(HTTPException) as error:
                    await scoped.get(foreign_run)
                assert error.value.status_code == 404
    await engine.dispose()


@pytest.mark.parametrize("fault", ["changed", "unsafe", "forged_event", "registration_denied"])
async def test_worker_rejects_untrusted_review_before_job(tmp_path, fault):
    await _exercise_rejected_source(tmp_path, fault)


async def test_source_admission_failures(tmp_path):
    await _exercise_rejected_source(tmp_path, "forged_event", two_tenants=True)

@pytest.mark.parametrize("state,classification", [
    ("IN_PROGRESS", "running"), ("STOPPING", "running"), ("STOPPED", "stopped"),
    ("FAILED", "failed"), ("COMPLETED", "completed")])
async def test_code_review_progress_is_scoped_serializable_and_static(state, classification):
    source = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "repository_commit", "record_id": "repo", "record_revision": "commit"}
    scope = {"tenant_id": "tenant-one", "product_id": "product-one", "project_id": "project-one"}
    run = SimpleNamespace(service_id="security", tenant_id="tenant-one", product_id="product-one",
                          project_id="project-one", run_id="run-one", configuration_revision=1,
                          binding_generation=1, input={"capability": "code_review", "sources": [source]},
                          admission={"scope": scope, "native_space_ref": {
                              **source, "record_type": "security_space", "record_id": "space"},
                              "provider_identity": {"account_id": "000000000000", "role_arn": "role",
                                                    "region": "us-east-1"},
                              "source_registration": {"bucket": "fictional-archive", "service_role": "role"},
                              "connection_ref": {"source_connection_id": "conn"},
                              "source_snapshots": [{"snapshot_id": "snapshot-one"}]},
                          manifest={"scope": scope, "sources": [source], "run_id": "run-one",
                                    "configuration_revision": 1, "binding_generation": 1,
                                    "source_snapshot_ids": ["snapshot-one"], "coverage_mode": "full_snapshot"})
    native = {"tenant_id": "tenant-one", "project_id": "project-one", "source_connection_id": "conn",
              "record_type": "native_job", "record_id": "job-one", "record_revision": "review-one"}

    class Provider:
        incomplete = False
        non_static = False
        missing_progress = False

        def batch_get_code_review_jobs(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space", "codeReviewJobIds": ["job-one"]}
            return {"codeReviewJobs": [{"codeReviewJobId": "job-one", "codeReviewId": "review-one",
                                        "status": state, "createdAt": NOW, "updatedAt": NOW,
                                        "errorInformation": {"code": "CLIENT_ERROR",
                                                             "message": "private provider detail"},
                                        "logConfig": {"logGroupName": "private log"},
                                        "steps": [] if self.missing_progress else [
                                            {"name": "PENTEST" if self.non_static else "STATIC_ANALYSIS",
                                                   "status": "IN_PROGRESS", "updatedAt": NOW}]}]}

        def list_code_review_job_tasks(self, **kwargs):
            assert kwargs == {"agentSpaceId": "space", "codeReviewJobId": "job-one",
                              "maxResults": 100, **({"nextToken": "second"} if kwargs.get("nextToken") else {})}
            if self.missing_progress:
                return {"codeReviewJobTaskSummaries": []}
            if kwargs.get("nextToken"):
                if self.incomplete:
                    return {"codeReviewJobTaskSummaries": [{"taskId": "wrong", "agentSpaceId": "foreign",
                                                               "codeReviewJobId": "job-one"}]}
                return {"codeReviewJobTaskSummaries": [{"taskId": "task-two", "agentSpaceId": "space",
                                                         "codeReviewId": "review-one", "codeReviewJobId": "job-one",
                                                         "executionStatus": "COMPLETED", "updatedAt": NOW}]}
            return {"codeReviewJobTaskSummaries": [{"taskId": "task-one", "agentSpaceId": "space",
                                                     "codeReviewId": "review-one", "codeReviewJobId": "job-one",
                                                     "executionStatus": "FAILED", "updatedAt": NOW}],
                    "nextToken": "second"}

    provider = Provider()

    class Session:
        account_id = "000000000000"
        role_arn = "role"
        region = "us-east-1"

        def client(self, service):
            assert service == "securityagent"
            return provider

    adapter = CodeReviewAdapter(None, clock=lambda: NOW)
    outcome = await adapter.observe(run, native, None, Session())
    assert outcome.classification == classification
    observation = outcome.observation
    assert observation["progress_coverage"] == "complete"
    assert observation["progress_fingerprint"]
    assert observation["created_at"] == observation["updated_at"] == NOW.isoformat()
    assert observation["phases"][0]["updated_at"] == observation["tasks"][0]["updated_at"] == NOW.isoformat()
    assert [task["status"] for task in observation["tasks"]] == ["FAILED", "COMPLETED"]
    assert observation["error_information"] == {"code": "CLIENT_ERROR"}
    assert "private" not in json.dumps(observation)
    assert (await adapter.observe(run, native, None, Session())).observation["progress_fingerprint"] == (
        observation["progress_fingerprint"])

    provider.incomplete = True
    partial = await adapter.observe(run, native, None, Session())
    assert partial.observation["progress_coverage"] == "partial"
    assert partial.observation["progress_fingerprint"] is None
    assert all(task["task_id"] != "wrong" for task in partial.observation["tasks"])

    provider.non_static = True
    unexpected = await adapter.observe(run, native, None, Session())
    assert unexpected.classification == "unknown" and unexpected.observation["progress_fingerprint"] is None

    provider.non_static = False
    provider.missing_progress = True
    missing = await adapter.observe(run, native, None, Session())
    assert missing.observation["progress_coverage"] == "unavailable"
    assert missing.observation["progress_fingerprint"] is None


@pytest.mark.parametrize("path", ["/other/src/main.py", "../src/main.py", "/tmp/opt/source_code/code_sc-00000000-0000-0000-0000-000000000000/../src/main.py", "unknown.py", "/tmp/opt/source_code/untrusted/src/main.py"])
def test_native_source_locations_reject_unknown_roots_and_traversal(path):
    from l5_services.adapters.security.code_review import source_location_path
    with pytest.raises(ValueError):
        source_location_path(path, {"src/main.py"})
