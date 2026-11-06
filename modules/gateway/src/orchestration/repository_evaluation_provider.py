"""Read immutable provider identities and verify bounded machine artifacts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import yaml

from src.knowledge.github_app_service import mint_installation_token_with_expiry, resolve_tenant_app_credentials

from .deployment_workflow_provider import WorkflowProvider
from .merge_evidence import GitHubEvidenceSource
from .repository_evaluation_contract import canonical, predicate_passes
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


def artifact_predicate_outcomes(expected, payload, source, definition, run_id, attempt, *, s3_foundation=False):
    if expected.bind_workflow_provenance:
        require(
            isinstance(payload, dict)
            and payload.get("source_revision") == source
            and payload.get("definition_revision") == definition
            and (
                isinstance(payload.get("gateway_revision"), str) and re.fullmatch(r"[0-9a-f]{40}", payload["gateway_revision"])
                if s3_foundation
                else payload.get("gateway_revision") == source
            )
            and payload.get("run_id") == str(run_id)
            and payload.get("run_attempt") == str(attempt),
            "artifact_workflow_provenance_mismatch",
        )
    return [dict(criterion_id=item.criterion_id, passed=predicate_passes(item, payload)) for item in expected.predicates]


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

    def __init__(self, *, client=None, clock=lambda: datetime.now(UTC), reviews=None, evidence_store=None):
        super().__init__(client=client, clock=clock, evidence_store=evidence_store)
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
        require(review is not None and review.head_sha == source["head_sha"], "review_missing_or_stale")
        # Only sources_for's verified, same-owner ledger decision can supply
        # these fields; the external-PR contract cannot assert owner acceptance.
        # It settles historical review objections, never later reviewer input.
        owner_accepted = False
        if source.get("accepted_merge_decision_id") and source.get("accepted_merge_at"):
            accepted_at = timestamp(source["accepted_merge_at"])
            reviewed_at = getattr(review, "review_updated_at", None)
            owner_accepted = (
                reviewed_at is not None
                and getattr(review, "review_state", None) in {"changes_requested", "stale"}
                and timestamp(pr["merged_at"]) <= accepted_at <= self.clock()
                and timestamp(reviewed_at) <= accepted_at
            )
        # The reviewer App records the merge controller's CI snapshot as a later
        # "changes requested" from its own identity, and GitHub refuses to dismiss
        # a review once the PR is merged. The same rule the merge controller applies
        # (merge_controller.app_snapshot_withdrawn, #7639): a merged head that the
        # App itself formally approved, with no change request from anyone else, is
        # approved; the required checks on that exact head are re-verified below.
        own_snapshot_withdrawn = (
            bool(getattr(review, "merged", False))
            and getattr(review, "review_state", None) == "changes_requested"
            and getattr(review, "foreign_changes_requested", None) is False
            and getattr(review, "own_approval_on_head", None) is True
        )
        require(review.review_approved or owner_accepted or own_snapshot_withdrawn, "review_missing_or_stale")
        receipts = await self.required_checks(binding, source)
        return {
            **{key: value for key, value in source.items() if key != "required_checks"},
            "provider_pr_node_id": pr["node_id"],
            "merged_at": pr["merged_at"],
            "checks": receipts,
            "provider_review_state": getattr(review, "review_state", None),
            "review_acceptance": "provider" if review.review_approved else "owner_acceptance" if owner_accepted else "own_snapshot_withdrawn",
        }

    async def required_checks(self, binding, source):
        """Every declared check must be the single latest run on the source revision, completed and green."""
        if not source["required_checks"]:
            return []
        checks = await self.pages(binding, f"/repos/{binding.repo}/commits/{source['head_sha']}/check-runs", "check_runs", filter="latest")
        receipts, suites, attempt_jobs, selected_suites = [], {}, {}, set()
        for expected in source["required_checks"]:
            matches = [check for check in checks if check.get("name") == expected["name"] and check.get("app", {}).get("id") == expected["app_id"]]
            workflow = None
            if expected.get("workflow") is not None:
                check, workflow = await self.qualified_check(binding, source, expected, matches, suites, attempt_jobs)
                selected_suites.add(workflow["check_suite_id"])
                matches = [check]
            require(len(matches) == 1, "required_check_missing_or_ambiguous")
            check = matches[0]
            require(
                check.get("head_sha") == source["head_sha"]
                and check.get("status") == "completed"
                and check.get("conclusion") == "success"
                and type(check.get("id")) is int,
                "required_check_not_successful",
            )
            receipt = dict(name=expected["name"], app_id=expected["app_id"], check_run_id=check["id"], head_sha=source["head_sha"])
            if workflow is not None:
                receipt["workflow"] = workflow
            receipts.append(receipt)
        # Recheck every selected workflow after all checks have been read. This
        # fences a rerun beginning while later checks are being collected, with
        # one fresh read per workflow rather than one per check.
        for suite in sorted(selected_suites):
            run = suites[suite]
            fresh = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run['id']}")).json()
            require(self.check_workflow_run(binding, source, suite, fresh) == run, "required_check_workflow_changed")
        return receipts

    async def verified_multi_source(self, binding, source, declared):
        """Read one source through its selected repository token and provider identity."""
        require(
            binding.provider_repository_id == declared.repository_id == source.get("repository_id"),
            "source_repository_identity",
        )
        if declared.evidence_kind == "merge":
            require(bool(source.get("provider_pr_node_id")), "source_pull_identity_missing")
        bare = {**source, "required_checks": []}
        if declared.evidence_kind == "merge":
            observed = await self.pull_request(binding, bare)
        else:
            observed = await self.deployed_revision(binding, bare)
        checks = (
            await self.pages(
                binding,
                f"/repos/{binding.repo}/commits/{source['head_sha']}/check-runs",
                "check_runs",
                filter="latest",
            )
            if declared.required_checks
            else []
        )
        evidence = []
        receipts = []
        suites = {}
        attempt_jobs = {}
        selected_suites = set()
        definitions = {}
        for required in declared.required_checks:
            matches = [check for check in checks if check.get("name") == required.name and check.get("app", {}).get("id") == required.app_id]
            workflow = None
            if required.workflow_path is not None:
                candidates = []
                for check in matches:
                    suite = (check.get("check_suite") or {}).get("id")
                    require(type(suite) is int and suite > 0, "required_check_workflow_identity")
                    if suite not in suites:
                        runs = await self.pages(
                            binding,
                            f"/repos/{binding.repo}/actions/runs",
                            "workflow_runs",
                            check_suite_id=suite,
                            head_sha=source["head_sha"],
                        )
                        require(len(runs) == 1, "required_check_workflow_missing_or_ambiguous")
                        suites[suite] = self.check_workflow_run(binding, source, suite, runs[0])
                    if suites[suite]["path"] == required.workflow_path:
                        candidates.append((check, suites[suite]))
                require(len(candidates) == 1, "required_check_missing_or_ambiguous")
                check, run = candidates[0]
                check, workflow = await self.qualified_check(
                    binding,
                    source,
                    {"name": required.name, "workflow": {"workflow_id": run["workflow_id"], "workflow_path": required.workflow_path}},
                    [check],
                    suites,
                    attempt_jobs,
                )
                selected_suites.add(workflow["check_suite_id"])
                if required.workflow_source_revision is not None:
                    for revision in (source["head_sha"], required.workflow_source_revision, required.workflow_definition_revision):
                        key = (required.workflow_path, revision)
                        if key not in definitions:
                            definitions[key] = (await self.definition_blob(binding, *key))[0]
                    actual_blob = definitions[(required.workflow_path, source["head_sha"])]
                    require(
                        actual_blob
                        == definitions[(required.workflow_path, required.workflow_source_revision)]
                        == definitions[(required.workflow_path, required.workflow_definition_revision)],
                        "required_check_workflow_run_definition_changed",
                    )
                    workflow["workflow_blob_sha"] = actual_blob
                matches = [check]
            require(len(matches) == 1, "required_check_missing_or_ambiguous")
            check = matches[0]
            require(
                type(check.get("id")) is int
                and check["id"] > 0
                and check.get("head_sha") == source["head_sha"]
                and check.get("status") == "completed"
                and check.get("conclusion") == "success",
                "required_check_not_successful",
            )
            receipts.append(
                dict(
                    name=required.name,
                    app_id=required.app_id,
                    head_sha=source["head_sha"],
                    workflow_path=required.workflow_path,
                    workflow_source_revision=required.workflow_source_revision,
                    workflow_definition_revision=required.workflow_definition_revision,
                    conclusion="success",
                )
            )
            evidence.append(
                dict(
                    repository_id=binding.provider_repository_id,
                    pr_node_id=observed.get("provider_pr_node_id"),
                    check_run_id=check["id"],
                    workflow=workflow,
                )
            )
        for suite in sorted(selected_suites):
            run = suites[suite]
            fresh = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run['id']}")).json()
            require(self.check_workflow_run(binding, source, suite, fresh) == run, "required_check_workflow_changed")
        return receipts, evidence

    @staticmethod
    def check_workflow_run(binding, source, suite, run):
        require(
            isinstance(run, dict)
            and all(type(run.get(key)) is int and run[key] > 0 for key in ("id", "workflow_id", "check_suite_id", "run_attempt"))
            and run["check_suite_id"] == suite
            and run.get("head_sha") == source["head_sha"]
            and isinstance(run.get("repository"), dict)
            and run["repository"].get("id") == binding.provider_repository_id
            and isinstance(run.get("head_repository"), dict)
            and run["head_repository"].get("id") == binding.provider_repository_id
            and isinstance(run.get("path"), str),
            "required_check_workflow_identity",
        )
        return {key: run.get(key) for key in ("id", "workflow_id", "check_suite_id", "run_attempt", "head_sha", "path", "status", "conclusion")}

    async def qualified_check(self, binding, source, expected, matches, suites, attempt_jobs):
        """Resolve workflow identity from provider APIs, never a check's display URL.

        GitHub documents check_suite_id on the repository workflow-runs endpoint.
        Attempt jobs bind the selected check back to the current run attempt.
        Caches belong to this source observation; required_checks fences every
        selected run again after collecting all checks.
        """
        selected = []
        prefix = f"/repos/{binding.repo}"
        selector = expected["workflow"]
        for check in matches:
            suite = (check.get("check_suite") or {}).get("id")
            require(
                type(suite) is int and suite > 0 and type(check.get("id")) is int and check["id"] > 0 and check.get("head_sha") == source["head_sha"],
                "required_check_workflow_identity",
            )
            if suite not in suites:
                runs = await self.pages(binding, f"{prefix}/actions/runs", "workflow_runs", check_suite_id=suite, head_sha=source["head_sha"])
                require(len(runs) == 1, "required_check_workflow_missing_or_ambiguous")
                suites[suite] = self.check_workflow_run(binding, source, suite, runs[0])
            run = suites[suite]
            if (run["workflow_id"], run["path"]) == (selector["workflow_id"], selector["workflow_path"]):
                selected.append((check, run))
        require(len(selected) == 1, "required_check_missing_or_ambiguous")
        check, run = selected[0]
        require(
            check.get("status") == run["status"] == "completed" and check.get("conclusion") == "success",
            "required_check_not_successful",
        )
        attempt = run["id"], run["run_attempt"]
        if attempt not in attempt_jobs:
            attempt_jobs[attempt] = await self.pages(binding, f"{prefix}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs")
        jobs = attempt_jobs[attempt]
        matching_jobs = [job for job in jobs if job.get("check_run_url") == f"https://api.github.com{prefix}/check-runs/{check['id']}"]
        require(len(matching_jobs) == 1, "required_check_job_missing_or_ambiguous")
        job = matching_jobs[0]
        require(
            type(job.get("id")) is int
            and job["id"] > 0
            and type(job.get("run_id")) is int
            and job["run_id"] == run["id"]
            and type(job.get("run_attempt")) is int
            and job["run_attempt"] == run["run_attempt"]
            and job.get("head_sha") == source["head_sha"]
            and job.get("name") == expected["name"]
            and job.get("status") == "completed"
            and job.get("conclusion") == "success",
            "required_check_job_identity_or_result",
        )
        return check, dict(
            workflow_id=run["workflow_id"],
            workflow_path=run["path"],
            check_suite_id=run["check_suite_id"],
            run_id=run["id"],
            run_attempt=run["run_attempt"],
            job_id=job["id"],
        )

    async def deployed_revision(self, binding, source):
        """An operations predecessor's source: the deployed revision must exist in the
        evaluated repository, and any declared checks must be green on it (#7880)."""
        commit = (await self.request(binding, "GET", f"/repos/{binding.repo}/commits/{source['head_sha']}", max_bytes=MAX_FILE)).json()
        require(commit.get("sha") == source["head_sha"] == source["merge_sha"], "deployed_revision_missing")
        return {**{key: value for key, value in source.items() if key != "required_checks"}, "checks": await self.required_checks(binding, source)}

    async def multi_probe_receipt(self, binding, run, probe):
        """Read one run/attempt-specific probe artifact through its scoped repository."""
        name = f"{probe.artifact_name}-{run.run_id}-{run.run_attempt}"
        rows = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run.run_id}/artifacts", "artifacts")
        matches = [row for row in rows if row.get("name") == name and not row.get("expired")]
        require(len(matches) == 1, "probe_artifact_missing_or_ambiguous")
        artifact = matches[0]
        require(
            type(artifact.get("id")) is int
            and artifact["id"] > 0
            and (artifact.get("workflow_run") or {}).get("id") == run.run_id
            and isinstance(run.started_at, str)
            and isinstance(artifact.get("created_at"), str)
            and timestamp(artifact["created_at"]) >= timestamp(run.started_at)
            and 0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE,
            "probe_artifact_size_or_identity",
        )
        archive = await self.request(
            binding, "GET", f"/repos/{binding.repo}/actions/artifacts/{artifact['id']}/zip", follow_redirects=True, max_bytes=MAX_ARCHIVE
        )
        _, files = verified_archive(artifact, archive.content)
        require(set(files) == {probe.artifact_path}, "probe_artifact_contents_mismatch")
        try:
            return parse_document(files[probe.artifact_path])
        except (ValueError, UnicodeDecodeError):
            raise CycleBlockedError("repository_evaluation_probe_receipt_invalid") from None

    async def definition_blob(self, binding, path, revision):
        record = (await self.request(binding, "GET", f"/repos/{binding.repo}/contents/{path}", params={"ref": revision})).json()
        require(
            record.get("type") == "file" and record.get("encoding") == "base64" and 0 < record.get("size", 0) <= 256 * 1024, "workflow_definition"
        )
        content = base64.b64decode(record["content"], validate=False)
        # Git's blob object ID, not a security digest — see deployment_workflow_provider.definition.
        blob = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content, usedforsecurity=False).hexdigest()
        require(blob == record.get("sha"), "workflow_blob_digest")
        return blob, content

    async def latest_workflow_run(self, binding, workflow_path, source, *, events=frozenset({"workflow_dispatch"})):
        path = quote(workflow_path.rsplit("/", 1)[1], safe="")
        response = await self.request(
            binding,
            "GET",
            f"/repos/{binding.repo}/actions/workflows/{path}/runs",
            params={**({"event": "workflow_dispatch"} if events == {"workflow_dispatch"} else {}), "head_sha": source, "per_page": 20},
        )
        candidates = response.json().get("workflow_runs")
        require(isinstance(candidates, list) and len(candidates) <= 20, "workflow_runs")
        # Never skip a newer failed/pending attempt to select an older green run.
        candidates = [run for run in candidates if run.get("head_sha") == source and run.get("event") in events]
        require(bool(candidates), "workflow_run_missing")
        return max(candidates, key=lambda run: (run.get("run_number", 0), run.get("id", 0)))

    async def workflow(
        self, binding, workflow, *, revisions, max_age_seconds, bound_run=None, producer=None, qualification=None, operation_source=None
    ):
        source = workflow.source.revision or revisions[workflow.source.predecessor]
        definition = workflow.definition.revision or revisions[workflow.definition.predecessor]
        operation_run = None
        if operation_source is not None:
            # These keys come from the accepted predecessor's scoped runtime
            # receipt. Preserve its run and attempt instead of searching by the
            # deployed SHA: workflow_dispatch executes at the current branch head.
            match = re.fullmatch(r"operations-runtime:([1-9][0-9]*):([1-9][0-9]*)", operation_source.get("deployment_receipt_key", ""))
            require(
                match is not None
                and bound_run is None
                and producer is None
                and operation_source.get("head_sha") == operation_source.get("merge_sha") == source
                and operation_source.get("workflow_operation_keys") == [f"operations-workflow:{match[1]}:{match[2]}"],
                "operation_workflow_binding_changed",
            )
            operation_run = int(match[1]), int(match[2])
        blob, content = await self.definition_blob(binding, workflow.path, definition)
        source_blob, source_content = await self.definition_blob(binding, workflow.path, source)
        require((blob, content) == (source_blob, source_content), "workflow_definition_changed")
        document = yaml.safe_load(content)
        require(isinstance(document, dict), "workflow_events")
        events = document.get("on", document.get(True))
        names = set(events) if isinstance(events, dict | list) else {events} if isinstance(events, str) else set()
        from .workflow_evaluation_contract import WorkflowProducer

        if isinstance(producer, WorkflowProducer) and bound_run is not None:
            # Admission permits this shared workflow's offline PR/call entrypoints.
            # The authenticated run below must still be a workflow_dispatch.
            require(
                workflow.path == ".github/workflows/eval-cli-uplift.yml" and names == {"workflow_dispatch", "workflow_call", "pull_request"},
                "qualification_workflow_events_changed",
            )
        else:
            require("workflow_dispatch" in names and (not workflow.dispatch_only or names == {"workflow_dispatch"}), "workflow_not_dispatch_only")
        if qualification is not None:
            from .cli_live_contract import NIGHTLY_SCHEDULE

            require(
                isinstance(events, dict) and names == {"workflow_dispatch", "schedule", "pull_request"} and events["schedule"] == NIGHTLY_SCHEDULE,
                "cli_nightly_schedule_changed",
            )
        allowed_events = {"workflow_dispatch", "schedule"} if qualification is not None and bound_run is None else {"workflow_dispatch"}
        chosen = (
            {"id": operation_run[0]}
            if operation_run is not None
            else {"id": bound_run.run_id}
            if bound_run is not None
            else await self.latest_workflow_run(binding, workflow.path, source, events=allowed_events)
        )
        expected_head = bound_run.context.workflow_revision if bound_run is not None else source
        run = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{int(chosen['id'])}")).json()
        if operation_run is not None:
            expected_head = run.get("head_sha")
            require(
                isinstance(expected_head, str) and re.fullmatch(r"[0-9a-f]{40}", expected_head) and run.get("run_attempt") == operation_run[1],
                "operation_workflow_run_changed",
            )
        require(
            run.get("repository", {}).get("id") == binding.provider_repository_id
            and run.get("head_repository", {}).get("id") == binding.provider_repository_id
            and run.get("head_sha") == expected_head
            and run.get("path", "").split("@", 1)[0] == workflow.path
            and run.get("event") in allowed_events
            and run.get("id") == chosen["id"]
            and run.get("status") == "completed"
            and run.get("conclusion") == "success"
            and type(run.get("run_attempt")) is int
            and run["run_attempt"] > 0,
            "workflow_run_not_successful_or_changed",
        )
        if operation_run is not None:
            # A moving branch is allowed only when both accepted revisions are
            # ancestors and the workflow bytes still match the approved definition.
            actual_blob, actual_content = await self.definition_blob(binding, workflow.path, expected_head)
            require((actual_blob, actual_content) == (blob, content), "workflow_definition_changed")
            for pin in sorted({source, definition} - {expected_head}):
                comparison = (
                    await self.request(binding, "GET", f"/repos/{binding.repo}/compare/{pin}...{expected_head}", params={"per_page": 1})
                ).json()
                require(
                    comparison.get("status") in {"ahead", "identical"}
                    and comparison.get("base_commit", {}).get("sha") == pin
                    and comparison.get("merge_base_commit", {}).get("sha") == pin,
                    "workflow_missing_delivered_revision",
                )
            # Artifacts name the exact executed definition, not its identical
            # ancestor. Keep source_revision bound to the deployed release.
            definition = expected_head
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
        transport = document.get("env", {}).get("ADP_REPOSITORY_EVIDENCE_STORE", "github-artifact")
        require(transport in {"github-artifact", "s3-oidc-v1"}, "artifact_transport_unknown")
        s3_artifacts = transport == "s3-oidc-v1"
        if s3_artifacts:
            require(
                workflow.path == ".github/workflows/security-scan.yml" and producer is not None and bound_run is not None,
                "s3_artifacts_require_bound_scan_target",
            )
        proof_transport = document.get("env", {}).get("ADP_IDENTITY_PROOF_EVIDENCE_STORE", "github-artifact")
        require(proof_transport in {"github-artifact", "s3-oidc-v1"}, "artifact_transport_unknown")
        foundation_env = document.get("jobs", {}).get("adapter-foundation", {}).get("env", {})
        foundation_transport = foundation_env.get("ADP_FOUNDATION_EVIDENCE_STORE", "github-artifact")
        require(foundation_transport in {"github-artifact", "s3-oidc-v1"}, "artifact_transport_unknown")
        artifacts = None
        artifact_receipts, outcomes, archives = [], [], {}
        for expected in workflow.artifacts:
            # An attempt suffix prevents a rerun reusing a prior attempt's archive.
            name = expected.name.replace("{run_id}", str(run_id)).replace("{run_attempt}", str(attempt))
            artifact_ref = None
            s3_proof = proof_transport == "s3-oidc-v1" and expected.name == "atlassian-stage-{run_id}-{run_attempt}"
            s3_foundation = foundation_transport == "s3-oidc-v1" and expected.name == "tools-live-evaluation-{run_id}-{run_attempt}"
            if s3_artifacts or s3_proof or s3_foundation:
                from deployment_evidence.store import EvidenceStore

                if s3_foundation:
                    from .repository_evaluation_contract import IdentityProofArtifactTarget

                    require(
                        workflow.path == ".github/workflows/tools-platform-deploy.yml"
                        and operation_run is not None
                        and attempt == 1
                        and expected.path == "atlassian-stage-summary.json"
                        and expected.bind_workflow_provenance
                        and "adapter-foundation" in workflow.required_jobs,
                        "s3_foundation_requires_bound_operation",
                    )
                    try:
                        target = IdentityProofArtifactTarget.model_validate(operation_source.get("foundation_artifact_target"))
                    except ValueError:
                        raise CycleBlockedError("foundation_artifact_target_changed") from None
                    account, environment, region = target.account_id, target.environment, target.region
                    archive_name = "atlassian-foundation"
                elif s3_proof:
                    from .repository_evaluation_contract import IdentityProofArtifactTarget

                    require(
                        workflow.path == ".github/workflows/gateway-deploy.yml"
                        and operation_run is not None
                        and expected.path == "atlassian-stage-summary.json"
                        and expected.bind_workflow_provenance
                        and {"Build and Deploy Backend", "identity-proof"}.issubset(workflow.required_jobs),
                        "s3_identity_proof_requires_bound_operation",
                    )
                    try:
                        target = IdentityProofArtifactTarget.model_validate(operation_source.get("identity_proof_artifact_target"))
                    except ValueError:
                        raise CycleBlockedError("identity_proof_artifact_target_changed") from None
                    account, environment, region = target.account_id, target.environment, target.region
                    archive_name = "identity-proof"
                else:
                    names = {
                        f"adp-repository-scan-{attempt}": "scan",
                        f"adp-repository-scan-cleanup-{attempt}": "cleanup",
                        f"security-reconciliation-{attempt}": "reconciliation",
                    }
                    require(name in names, "s3_artifact_name_unknown")
                    account, environment, region = producer.target.account_id, producer.inputs.get("environment", "dev"), producer.target.region
                    archive_name = names[name]
                try:
                    result = await asyncio.to_thread(
                        (self.evidence_store or EvidenceStore()).read_archive,
                        account=account,
                        environment=environment,
                        region=region,
                        repository=binding.repo,
                        repository_id=binding.provider_repository_id,
                        run_id=run_id,
                        attempt=attempt,
                        kind="repository-artifact",
                        name=archive_name,
                        workflow_path=workflow.path,
                        workflow_revision=expected_head,
                        source_revision=source,
                    )
                except Exception:
                    raise CycleBlockedError("repository_evaluation_s3_archive_unverifiable") from None
                require(result is not None, "artifact_missing_or_ambiguous")
                content, signed_digest, artifact_ref = result
                artifact = {"id": None, "digest": "sha256:" + signed_digest}
            else:
                if artifacts is None:
                    artifacts = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run_id}/artifacts", "artifacts")
                matches = [artifact for artifact in artifacts if artifact.get("name") == name and artifact.get("expired") is False]
                require(len(matches) == 1, "artifact_missing_or_ambiguous")
                artifact = matches[0]
                require(0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE, "artifact_size")
                require(timestamp(artifact["created_at"]) >= timestamp(run["run_started_at"]), "artifact_prior_attempt")
                response = await self.request(
                    binding, "GET", f"/repos/{binding.repo}/actions/artifacts/{int(artifact['id'])}/zip", max_bytes=MAX_ARCHIVE, follow_redirects=True
                )
                content = response.content
            digest, files = verified_archive(artifact, content)
            require(expected.path in files, "artifact_file_missing")
            data = files[expected.path]
            payload = parse_document(data)
            predicate_outcomes = artifact_predicate_outcomes(expected, payload, source, definition, run_id, attempt, s3_foundation=s3_foundation)
            if qualification is not None:
                archives[(expected.name, expected.path)] = payload, files
            if (
                producer is not None
                and qualification is None
                and hasattr(producer, "images")
                and (expected.name, expected.path) == (producer.receipt_artifact, producer.receipt_path)
            ):
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
                dict(
                    artifact_id=artifact["id"],
                    name=name,
                    digest=digest,
                    path=expected.path,
                    sha256=hashlib.sha256(data).hexdigest(),
                    **({"artifact_ref": artifact_ref} if artifact_ref else {}),
                )
            )
            outcomes.extend(predicate_outcomes)
        qualified = None
        if qualification is not None:
            from .cli_live_evidence import validate_qualification

            qualified, criteria = validate_qualification(
                qualification,
                run=run,
                jobs=jobs,
                archives=archives,
                correlation=bound_run.context.correlation if bound_run is not None else None,
                now=self.clock(),
            )
            outcomes.extend(criteria)
        # Rerun/replacement while downloading invalidates this entire observation.
        current = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{run_id}")).json()
        require(
            all(current.get(key) == run.get(key) for key in ("id", "run_attempt", "head_sha", "status", "conclusion", "updated_at")),
            "workflow_run_changed_during_observation",
        )
        # The selected run can remain green while a newer run starts or fails.
        # Re-read selection too; rechecking only this run would accept stale success.
        if bound_run is None and operation_run is None:
            latest = await self.latest_workflow_run(binding, workflow.path, source, events=allowed_events)
            require(latest.get("id") == run_id and latest.get("run_attempt") == attempt, "workflow_latest_run_changed")
        return dict(
            criterion_id=workflow.criterion_id,
            workflow_path=workflow.path,
            source_revision=source,
            definition_revision=definition,
            workflow_blob_sha=blob,
            run_id=run_id,
            run_attempt=attempt,
            event=run["event"],
            jobs=job_receipts,
            artifacts=artifact_receipts,
            criteria=outcomes,
            **({"qualification": qualified} if qualification is not None else {}),
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
            if source.get("deployment_receipt_key") and source.get("pr_number") is None:
                return await self.deployed_revision(binding, source)
            return await self.pull_request(binding, source)

        qualification = getattr(spec, "qualification", None)
        if qualification is not None:
            required_issues = {int(item.criterion_id.split("/", 1)[0]) for item in qualification.criteria}
            require(required_issues <= {source.get("issue_number") for source in sources}, "cli_prerequisite_delivery_sources_missing")
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
        qualification = getattr(spec, "qualification", None)
        if qualification is not None:
            workflow = spec.workflows[0]
            source = workflow.source.revision or revisions[workflow.source.predecessor]
            _, content = await self.definition_blob(binding, spec.runner.qualification_config_path, source)
            from .cli_live_contract import Qualification

            require(
                canonical(Qualification.model_validate(parse_document(content)).model_dump(mode="json"))
                == canonical(qualification.model_dump(mode="json")),
                "cli_accepted_configuration_missing_or_changed",
            )
            _, requirements = await self.definition_blob(binding, spec.runner.requirements_path, source)
            require(hashlib.sha256(requirements).hexdigest() == qualification.requirements_sha256, "cli_requirements_snapshot_changed")
            requirements = parse_document(requirements)
            require(
                isinstance(requirements, dict)
                and set(requirements) == {"evidence_schema", "criteria"}
                and requirements["evidence_schema"] == "cli-live-requirements/v1"
                and isinstance(requirements["criteria"], list),
                "cli_requirements_snapshot_invalid",
            )
            require(
                all(
                    isinstance(item, dict)
                    and set(item) == {"criterion_id", "source_text"}
                    and isinstance(item["criterion_id"], str)
                    and isinstance(item["source_text"], str)
                    for item in requirements["criteria"]
                ),
                "cli_requirements_snapshot_invalid",
            )
            statements = {item["criterion_id"]: item["source_text"] for item in requirements["criteria"]}
            require(
                len(statements) == len(requirements["criteria"]) and set(statements) == {item.criterion_id for item in qualification.criteria},
                "cli_requirements_coverage_changed",
            )
            bodies = {(item.criterion_id.split("/", 1)[0], item.source_body_sha256) for item in qualification.criteria}

            async def requirements_body(item):
                issue, expected = item
                document = (await self.request(binding, "GET", f"/repos/{binding.repo}/issues/{issue}", max_bytes=MAX_FILE)).json()
                require(
                    document.get("number") == int(issue)
                    and isinstance(document.get("body"), str)
                    and hashlib.sha256(document["body"].encode()).hexdigest() == expected,
                    "cli_requirement_body_changed",
                )
                for criterion in qualification.criteria:
                    if criterion.criterion_id.split("/", 1)[0] == issue:
                        statement = statements[criterion.criterion_id]
                        require(
                            isinstance(statement, str)
                            and bool(statement)
                            and statement in document["body"]
                            and hashlib.sha256(statement.encode()).hexdigest() == criterion.requirement_sha256,
                            "cli_requirement_statement_changed",
                        )

            await bounded_reads(requirements_body, sorted(bodies))
            comparisons = sorted(
                set(comparisons)
                | {
                    (source["merge_sha"], qualification.deployment.gateway_revision)
                    for source in sources
                    if source["merge_sha"] != qualification.deployment.gateway_revision
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
        operations = {
            source["address"]: source
            for source in sources
            if source.get("address") and source.get("deployment_receipt_key", "").startswith("operations-runtime:")
        }
        workflows = [
            await self.workflow(
                binding,
                item,
                revisions=revisions,
                max_age_seconds=spec.max_age_seconds,
                **({"operation_source": operations[item.source.predecessor]} if item.source.predecessor in operations else {}),
                **({"qualification": spec.qualification} if getattr(spec, "qualification", None) is not None else {}),
            )
            for item in spec.workflows
        ]
        deployments = [item for item in pulls if item.get("deployment_receipt_key") and item.get("pr_number") is None]
        pulls = [item for item in pulls if item not in deployments]
        return dict(
            pull_requests=pulls,
            workflows=workflows,
            mandatory_passed=all(c["passed"] for row in workflows for c in row["criteria"]),
            **({"deployments": deployments} if deployments else {}),
        )
