"""Read immutable provider identities and verify bounded machine artifacts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import yaml

from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

from .deployment_workflow_provider import WorkflowProvider
from .merge_evidence import GitHubEvidenceSource
from .repository_evaluation_contract import predicate_passes
from .review_cycle import CycleBlockedError

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024


def require(condition, reason):
    if not condition:
        raise CycleBlockedError("repository_evaluation_" + reason)


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp_unverifiable")
    return parsed


def parse_document(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("nonfinite JSON value")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant)


def verified_archive(artifact, content):
    require(0 < len(content) <= MAX_ARCHIVE, "artifact_size")
    digest = hashlib.sha256(content).hexdigest()
    require(artifact.get("digest") == "sha256:" + digest, "artifact_digest")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = archive.infolist()
        require(
            1 <= len(entries) <= 256
            and len({entry.filename for entry in entries}) == len(entries)
            and sum(entry.file_size for entry in entries) <= MAX_EXPANDED,
            "artifact_archive",
        )
        for entry in entries:
            require(
                not entry.is_dir()
                and 0 < entry.file_size <= MAX_FILE
                and not entry.filename.startswith("/")
                and "\\" not in entry.filename
                and all(part not in {"", ".", ".."} for part in entry.filename.split("/")),
                "artifact_archive",
            )
        return digest, {entry.filename: archive.read(entry) for entry in entries}


class RepositoryEvidenceProvider(WorkflowProvider):
    """No provider mutations or worker credentials; one repository-scoped reader."""

    def __init__(self, *, client=None, clock=lambda: datetime.now(UTC), reviews=None):
        super().__init__(client=client, clock=clock)
        self.reviews = reviews or GitHubEvidenceSource()
        self._tokens = {}

    async def token(self, binding, *, write=False):
        require(not write, "read_only")
        key = (binding.org_id, binding.installation_id, binding.repo, binding.provider_repository_id)
        cached = self._tokens.get(key)
        if cached and cached[1] > self.clock() + timedelta(seconds=30):
            return cached[0]
        app, private_key = await resolve_tenant_app_credentials(binding.org_id)
        token, expires = await mint_installation_token_with_expiry(
            app,
            private_key,
            binding.installation_id,
            repositories=[binding.repo.split("/", 1)[1]],
            permissions={"actions": "read", "contents": "read", "pull_requests": "read", "issues": "read", "checks": "read", "metadata": "read"},
        )
        deadline = timestamp(expires)
        require(deadline > self.clock() + timedelta(seconds=30), "credential_expired")
        self._tokens[key] = token, deadline
        return token

    async def pull_request(self, binding, source):
        prefix = f"/repos/{binding.repo}"
        pr = (await self.request(binding, "GET", f"{prefix}/pulls/{source['pr_number']}")).json()
        require(
            pr.get("number") == source["pr_number"]
            and pr.get("base", {}).get("repo", {}).get("id") == binding.provider_repository_id
            and pr.get("head", {}).get("repo", {}).get("id") == binding.provider_repository_id
            and pr.get("head", {}).get("sha") == source["head_sha"]
            and pr.get("merged") is True
            and pr.get("merge_commit_sha") == source["merge_sha"]
            and isinstance(pr.get("node_id"), str),
            "pull_request_identity_or_merge_changed",
        )
        if source.get("provider_pr_node_id"):
            require(pr["node_id"] == source["provider_pr_node_id"], "pull_request_identity_changed")
        review = await self.reviews.bound_pull_request(
            org_id=binding.org_id,
            installation_id=binding.installation_id,
            repo=binding.repo,
            pr_number=source["pr_number"],
            read_token=await self.token(binding),
        )
        require(review is not None and review.review_approved and review.head_sha == source["head_sha"], "review_missing_or_stale")
        checks = await self.pages(binding, f"{prefix}/commits/{source['head_sha']}/check-runs", "check_runs", filter="latest")
        receipts = []
        for expected in source["required_checks"]:
            matches = [check for check in checks if check.get("name") == expected["name"] and check.get("app", {}).get("id") == expected["app_id"]]
            require(len(matches) == 1, "required_check_missing_or_ambiguous")
            check = matches[0]
            require(
                check.get("head_sha") == source["head_sha"]
                and check.get("status") == "completed"
                and check.get("conclusion") == "success"
                and type(check.get("id")) is int,
                "required_check_not_successful",
            )
            receipts.append(dict(name=expected["name"], app_id=expected["app_id"], check_run_id=check["id"], head_sha=source["head_sha"]))
        return {
            **{key: value for key, value in source.items() if key != "required_checks"},
            "provider_pr_node_id": pr["node_id"],
            "merged_at": pr["merged_at"],
            "checks": receipts,
        }

    async def definition_blob(self, binding, path, revision):
        record = (await self.request(binding, "GET", f"/repos/{binding.repo}/contents/{path}", params={"ref": revision})).json()
        require(
            record.get("type") == "file" and record.get("encoding") == "base64" and 0 < record.get("size", 0) <= 256 * 1024, "workflow_definition"
        )
        content = base64.b64decode(record["content"], validate=False)
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
        require(blob == record.get("sha"), "workflow_blob_digest")
        return blob, content

    async def latest_workflow_run(self, binding, workflow_path, source):
        path = quote(workflow_path.rsplit("/", 1)[1], safe="")
        response = await self.request(
            binding,
            "GET",
            f"/repos/{binding.repo}/actions/workflows/{path}/runs",
            params={"event": "workflow_dispatch", "head_sha": source, "per_page": 20},
        )
        candidates = response.json().get("workflow_runs")
        require(isinstance(candidates, list) and len(candidates) <= 20, "workflow_runs")
        # Never skip a newer failed/pending attempt to select an older green run.
        candidates = [run for run in candidates if run.get("head_sha") == source and run.get("event") == "workflow_dispatch"]
        require(bool(candidates), "workflow_run_missing")
        return max(candidates, key=lambda run: (run.get("run_number", 0), run.get("id", 0)))

    async def workflow(self, binding, workflow, *, revisions, max_age_seconds, bound_run=None, producer=None):
        source = workflow.source.revision or revisions[workflow.source.predecessor]
        definition = workflow.definition.revision or revisions[workflow.definition.predecessor]
        blob, content = await self.definition_blob(binding, workflow.path, definition)
        source_blob, _ = await self.definition_blob(binding, workflow.path, source)
        require(blob == source_blob, "workflow_definition_changed")
        document = yaml.safe_load(content)
        require(isinstance(document, dict), "workflow_events")
        events = document.get("on", document.get(True))
        names = set(events) if isinstance(events, dict | list) else {events} if isinstance(events, str) else set()
        require("workflow_dispatch" in names and (not workflow.dispatch_only or names == {"workflow_dispatch"}), "workflow_not_dispatch_only")
        chosen = {"id": bound_run.run_id} if bound_run is not None else await self.latest_workflow_run(binding, workflow.path, source)
        expected_head = bound_run.context.workflow_revision if bound_run is not None else source
        run = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{int(chosen['id'])}")).json()
        require(
            run.get("repository", {}).get("id") == binding.provider_repository_id
            and run.get("head_repository", {}).get("id") == binding.provider_repository_id
            and run.get("head_sha") == expected_head
            and run.get("path", "").split("@", 1)[0] == workflow.path
            and run.get("event") == "workflow_dispatch"
            and run.get("id") == chosen["id"]
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and type(run.get("run_attempt")) is int
            and run["run_attempt"] > 0,
            "workflow_run_not_successful_or_changed",
        )
        age = (self.clock() - timestamp(run["updated_at"])).total_seconds()
        require(0 <= age <= max_age_seconds, "workflow_run_stale")
        run_id, attempt = run["id"], run["run_attempt"]
        if bound_run is not None:
            require(bound_run.run_attempt == attempt and bound_run.context.source_revision == source, "producer_run_changed")
        jobs = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
        job_receipts = []
        for name in workflow.required_jobs:
            matches = [job for job in jobs if job.get("name") == name]
            require(len(matches) == 1, "required_job_missing_or_ambiguous")
            job = matches[0]
            require(
                job.get("status") == "completed" and job.get("conclusion") == "success" and job.get("run_id") == run_id, "required_job_not_successful"
            )
            job_receipts.append(dict(name=name, job_id=job["id"]))
        artifacts = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run_id}/artifacts", "artifacts")
        artifact_receipts, outcomes = [], []
        for expected in workflow.artifacts:
            # An attempt suffix prevents a rerun reusing a prior attempt's archive.
            name = expected.name.replace("{run_id}", str(run_id)).replace("{run_attempt}", str(attempt))
            matches = [artifact for artifact in artifacts if artifact.get("name") == name and artifact.get("expired") is False]
            require(len(matches) == 1, "artifact_missing_or_ambiguous")
            artifact = matches[0]
            require(0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE, "artifact_size")
            require(timestamp(artifact["created_at"]) >= timestamp(run["run_started_at"]), "artifact_prior_attempt")
            response = await self.request(
                binding, "GET", f"/repos/{binding.repo}/actions/artifacts/{int(artifact['id'])}/zip", max_bytes=MAX_ARCHIVE, follow_redirects=True
            )
            digest, files = verified_archive(artifact, response.content)
            require(expected.path in files, "artifact_file_missing")
            data = files[expected.path]
            payload = parse_document(data)
            if producer is not None and (expected.name, expected.path) == (producer.receipt_artifact, producer.receipt_path):
                from .repository_producer_contract import RepositoryScanReceipt

                scan = RepositoryScanReceipt.model_validate(payload)
                require(
                    bound_run is not None
                    and scan.source_revision == source
                    and scan.correlation == bound_run.context.correlation
                    and scan.target == producer.target
                    and scan.images == producer.images
                    and scan.coverage_complete
                    and scan.cleanup_complete,
                    "scan_scope_coverage_images_or_cleanup_changed",
                )
            artifact_receipts.append(
                dict(artifact_id=artifact["id"], name=name, digest=digest, path=expected.path, sha256=hashlib.sha256(data).hexdigest())
            )
            outcomes.extend(dict(criterion_id=item.criterion_id, passed=predicate_passes(item, payload)) for item in expected.predicates)
        # Rerun/replacement while downloading invalidates this entire observation.
        current = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run_id}")).json()
        require(
            all(current.get(key) == run.get(key) for key in ("id", "run_attempt", "head_sha", "status", "conclusion", "updated_at")),
            "workflow_run_changed_during_observation",
        )
        # The selected run can remain green while a newer run starts or fails.
        # Re-read selection too; rechecking only this run would accept stale success.
        if bound_run is None:
            latest = await self.latest_workflow_run(binding, workflow.path, source)
            require(latest.get("id") == run_id and latest.get("run_attempt") == attempt, "workflow_latest_run_changed")
        return dict(
            criterion_id=workflow.criterion_id,
            workflow_path=workflow.path,
            source_revision=source,
            definition_revision=definition,
            workflow_blob_sha=blob,
            run_id=run_id,
            run_attempt=attempt,
            event="workflow_dispatch",
            jobs=job_receipts,
            artifacts=artifact_receipts,
            criteria=outcomes,
        )

    async def observe(self, binding, spec, sources):
        owned = self.client is None
        if owned:
            self.client = httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False)
        try:
            return await self._observe(binding, spec, sources)
        finally:
            if owned:
                await self.client.aclose()
                self.client = None

    async def verify_sources(self, binding, spec, sources):
        repository = (await self.request(binding, "GET", f"/repos/{binding.repo}")).json()
        require(repository.get("id") == spec.runner.repository_id == binding.provider_repository_id, "repository_changed")
        semaphore = asyncio.Semaphore(4)

        async def bounded_reads(operation, values):
            async def read(value):
                async with semaphore:
                    return await operation(value)

            try:
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(read(value)) for value in values]
            except ExceptionGroup as errors:
                if all(isinstance(error, CycleBlockedError) for error in errors.exceptions):
                    raise errors.exceptions[0] from None
                raise
            return [task.result() for task in tasks]

        async def pull(source):
            return await self.pull_request(binding, source)

        pulls = await bounded_reads(pull, sources)
        revisions = {source["address"]: source["merge_sha"] for source in sources if source.get("address")}
        comparisons = sorted(
            {
                (source["merge_sha"], workflow.source.revision or revisions[workflow.source.predecessor])
                for workflow in spec.workflows
                for source in sources
                if source["merge_sha"] != (workflow.source.revision or revisions[workflow.source.predecessor])
            }
        )

        async def ancestry(pair):
            merge, revision = pair
            comparison = (
                await self.request(binding, "GET", f"/repos/{binding.repo}/compare/{merge}...{revision}", params={"per_page": 1}, max_bytes=MAX_FILE)
            ).json()
            require(
                comparison.get("status") in {"ahead", "identical"}
                and comparison.get("base_commit", {}).get("sha") == merge
                and comparison.get("merge_base_commit", {}).get("sha") == merge,
                "workflow_missing_delivered_revision",
            )

        await bounded_reads(ancestry, comparisons)
        return pulls, revisions

    async def _observe(self, binding, spec, sources):
        require(spec.producer is None, "producer_requires_durable_execution")
        pulls, revisions = await self.verify_sources(binding, spec, sources)
        workflows = [await self.workflow(binding, item, revisions=revisions, max_age_seconds=spec.max_age_seconds) for item in spec.workflows]
        return dict(pull_requests=pulls, workflows=workflows, mandatory_passed=all(c["passed"] for row in workflows for c in row["criteria"]))
