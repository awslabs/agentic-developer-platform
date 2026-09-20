"""Authenticate built release evidence independently of D2's context archive."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime, timedelta

from .deployment_runtime_contract import ReleaseArtifact
from .deployment_workflow_provider import MAX_ARCHIVE_BYTES, MAX_CONTEXT_BYTES, WorkflowProvider
from .review_cycle import CycleBlockedError


class ReleaseProvider(WorkflowProvider):
    async def contains(self, binding, source, actual):
        repository = (await self.request(binding, "GET", f"/repos/{binding.repo}")).json()
        if repository.get("id") != binding.provider_repository_id:
            raise CycleBlockedError("deployment_repository_identity_mismatch")
        compared = (await self.request(binding, "GET", f"/repos/{binding.repo}/compare/{source}...{actual}")).json()
        if compared.get("status") != "ahead" or compared.get("merge_base_commit", {}).get("sha") != source:
            raise CycleBlockedError("deployment_newer_release_does_not_contain_merge")

    async def release(self, binding, run, component):
        name = f"adp-release-{component}-{run.run_attempt}"
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
            if len(entries) != 1 or entries[0].filename != "release.json" or entries[0].file_size > MAX_CONTEXT_BYTES:
                raise CycleBlockedError("deployment_release_archive_invalid")
            release = ReleaseArtifact.model_validate_json(zipped.read(entries[0]))
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
        return release, digest, f"github/actions/runs/{run.run_id}/artifacts/{int(artifact['id'])}"
