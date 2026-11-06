"""Authenticate built release evidence independently of D2's context archive."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

from .delivery_contract import WorkflowDeliveryEvidence
from .deployment_runtime_contract import ReleaseArtifact
from .deployment_workflow_provider import MAX_ARCHIVE_BYTES, MAX_CONTEXT_BYTES, WorkflowProvider
from .review_cycle import CycleBlockedError


class ReleaseProvider(WorkflowProvider):
    async def contains(self, binding, source, actual):
        repository = (await self.request(binding, "GET", f"/repos/{binding.repo}")).json()
        if repository.get("id") != binding.provider_repository_id:
            raise CycleBlockedError("deployment_repository_identity_mismatch")
        # Reversed on purpose (#7812): `compare/{actual}...{source}` answers "is
        # `source` an ancestor of `actual`?" with `status: behind` and the merge base,
        # and carries no commits or file patches because its head *is* the merge base.
        # The forward direction returns every commit and changed file between them —
        # 1.18 MB for 66 commits on 2026-10-15 — and tripped the 1 MiB response bound,
        # holding runtime verification with `deployment_provider_response_limit`.
        compared = (await self.request(binding, "GET", f"/repos/{binding.repo}/compare/{actual}...{source}")).json()
        if compared.get("status") != "behind" or compared.get("merge_base_commit", {}).get("sha") != source:
            raise CycleBlockedError("deployment_newer_release_does_not_contain_merge")

    async def release(self, binding, run, component, *, delivery_contract=None):
        context = run.context
        evidence_type = WorkflowDeliveryEvidence if delivery_contract is not None else ReleaseArtifact
        # Generic delivery opts in through its authenticated workflow definition.
        # Existing workflows with S3 context and GitHub delivery remain compatible.
        # A missing signed receipt never falls back to a different transport.
        use_s3 = (
            getattr(run, "artifact_ref", None) is not None
            if delivery_contract is None
            else getattr(run, "delivery_evidence_transport", "github-artifact") == "s3-oidc-v1"
        )
        if use_s3:
            result = await self.s3_evidence(
                account=context.account_id,
                environment=context.inputs.get("environment") or "dev",
                region=context.region,
                repository=binding.repo,
                repository_id=binding.provider_repository_id,
                run_id=run.run_id,
                attempt=run.run_attempt,
                kind="delivery" if delivery_contract is not None else "release",
                name=delivery_contract.evidence_artifact if delivery_contract is not None else component,
                workflow_path=context.workflow_path,
                workflow_revision=context.workflow_revision,
            )
            if result is None:
                raise CycleBlockedError("deployment_release_artifact_missing_or_ambiguous")
            payload, digest, reference = result
            release = evidence_type.model_validate_json(payload)
        else:
            name = (
                f"{delivery_contract.evidence_artifact}-{run.run_attempt}"
                if delivery_contract is not None
                else f"adp-release-{component}-{run.run_attempt}"
            )
            rows = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run.run_id}/artifacts", "artifacts")
            matches = [row for row in rows if row.get("name") == name and not row.get("expired")]
            if len(matches) != 1:
                raise CycleBlockedError("deployment_release_artifact_missing_or_ambiguous")
            artifact = matches[0]
            if not 0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE_BYTES:
                raise CycleBlockedError("deployment_release_artifact_size_invalid")
            response = await self.request(binding, "GET", f"/repos/{binding.repo}/actions/artifacts/{int(artifact['id'])}/zip", follow_redirects=True)
            digest = hashlib.sha256(response.content).hexdigest()
            if artifact.get("digest") != "sha256:" + digest:
                raise CycleBlockedError("deployment_release_artifact_digest_mismatch")
            with zipfile.ZipFile(io.BytesIO(response.content)) as zipped:
                entries = zipped.infolist()
                if (
                    len(entries) != 1
                    or entries[0].filename != ("delivery.json" if delivery_contract is not None else "release.json")
                    or entries[0].file_size > MAX_CONTEXT_BYTES
                ):
                    raise CycleBlockedError("deployment_release_archive_invalid")
                release = evidence_type.model_validate_json(zipped.read(entries[0]))
            reference = f"github/actions/runs/{run.run_id}/artifacts/{int(artifact['id'])}"
        context = run.context
        fields = (
            "repository_id",
            "run_id",
            "run_attempt",
            "workflow_path",
            "workflow_revision",
            "source_revision",
            "account_id",
            "region",
            "resource_id",
        )
        if any(getattr(release, key) != getattr(context, key) for key in fields) or release.component != component:
            raise CycleBlockedError("deployment_release_artifact_identity_mismatch")
        if release.produced_at > datetime.now(UTC) + timedelta(seconds=30):
            raise CycleBlockedError("deployment_release_artifact_time_invalid")
        return release, digest, reference
