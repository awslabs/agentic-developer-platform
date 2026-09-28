"""Pull only the pinned qualification workflow's authenticated evaluation archive."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import UTC, datetime

import httpx

from .deployment_workflow_provider import WorkflowProvider
from .evaluation_contract import specification
from .evaluation_evidence import EvaluationEvidenceError, EvidenceRefusal, ProviderEvidence, require, validate_evaluation_evidence
from .review_cycle import CycleBlockedError

MAX_ARCHIVE = 16 * 1024 * 1024
MAX_EXPANDED = 32 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024
MAX_RECEIPT = 256 * 1024


class EvaluationProvider(WorkflowProvider):
    async def find(self, binding, expected):
        try:
            return await self._find(binding, expected)
        except EvaluationEvidenceError:
            raise
        except (httpx.HTTPError, CycleBlockedError):
            raise EvaluationEvidenceError(EvidenceRefusal.PROVIDER_UNAVAILABLE) from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise EvaluationEvidenceError(EvidenceRefusal.SCHEMA_INVALID) from None

    async def _find(self, binding, expected):
        """Bounded pull of recent pinned harness runs; unrelated scopes grant nothing."""
        spec = specification(expected.specification)
        require(spec.runner is not None, EvidenceRefusal.SPECIFICATION_CHANGED)
        response = await self.request(
            binding, "GET", f"/repos/{binding.repo}/actions/workflows/orchestration-live-tests.yml/runs?event=workflow_dispatch&per_page=20"
        )
        runs = response.json().get("workflow_runs", [])
        require(isinstance(runs, list) and len(runs) <= 20, EvidenceRefusal.SCHEMA_INVALID)
        candidates = [
            run
            for run in runs
            if isinstance(run, dict)
            and run.get("display_title") == "ADP evaluation " + expected.execution_id
            and run.get("head_sha") == spec.runner.harness_revision
            and run.get("status") == "completed"
        ]
        for run in candidates[:4]:
            try:
                validated = await self.observe(binding, expected, run_id=run.get("id"))
            except EvaluationEvidenceError as error:
                if error.reason in {
                    EvidenceRefusal.SCOPE_CHANGED,
                    EvidenceRefusal.SPECIFICATION_CHANGED,
                    EvidenceRefusal.DEPLOYMENT_CHANGED,
                    EvidenceRefusal.EXPIRED,
                }:
                    continue
                raise
            if validated is not None:
                return validated
        return None

    async def observe(self, binding, expected, *, run_id):
        try:
            return await self._observe(binding, expected, run_id=run_id)
        except EvaluationEvidenceError:
            raise
        except (httpx.HTTPError, CycleBlockedError):
            raise EvaluationEvidenceError(EvidenceRefusal.PROVIDER_UNAVAILABLE) from None
        except (ValueError, KeyError, TypeError):
            raise EvaluationEvidenceError(EvidenceRefusal.SCHEMA_INVALID) from None

    async def _observe(self, binding, expected, *, run_id):
        require(type(run_id) is int and run_id > 0, EvidenceRefusal.PRODUCER_MISMATCH)
        spec = specification(expected.specification)
        require(
            spec.runner is not None
            and binding.org_id == expected.identity.org_id
            and binding.repo == spec.runner.repository
            and binding.provider_repository_id == spec.runner.repository_id,
            EvidenceRefusal.PRODUCER_MISMATCH,
        )
        repository = (await self.request(binding, "GET", f"/repos/{binding.repo}")).json()
        run = (await self.request(binding, "GET", f"/repos/{binding.repo}/actions/runs/{int(run_id)}")).json()
        require(
            repository.get("id") == spec.runner.repository_id
            and run.get("repository", {}).get("id") == spec.runner.repository_id
            and run.get("head_repository", {}).get("id") == spec.runner.repository_id
            and run.get("path", "").split("@", 1)[0] == spec.runner.workflow_path
            and run.get("head_sha") == spec.runner.harness_revision
            and run.get("event") == "workflow_dispatch"
            and run.get("id") == run_id,
            EvidenceRefusal.PRODUCER_MISMATCH,
        )
        if run.get("status") != "completed":
            return None
        # Failure is valid *evidence* when the trusted runner reports failed
        # criteria. Cancelled/unknown runs cannot attest a completed evaluation.
        require(run.get("conclusion") in {"success", "failure"}, EvidenceRefusal.PRODUCER_MISMATCH)
        attempt = run.get("run_attempt")
        require(type(attempt) is int and attempt > 0, EvidenceRefusal.PRODUCER_MISMATCH)
        rows = await self.pages(binding, f"/repos/{binding.repo}/actions/runs/{run_id}/artifacts", "artifacts")
        selected = [row for row in rows if row.get("name") == f"orchestration-evaluation-{run_id}-{attempt}" and not row.get("expired")]
        require(len(selected) == 1, EvidenceRefusal.ARTIFACT_INTEGRITY)
        artifact = selected[0]
        require(0 < artifact.get("size_in_bytes", 0) <= MAX_ARCHIVE, EvidenceRefusal.ARTIFACT_INTEGRITY)
        response = await self.request(
            binding, "GET", f"/repos/{binding.repo}/actions/artifacts/{int(artifact['id'])}/zip", max_bytes=MAX_ARCHIVE, follow_redirects=True
        )
        digest = hashlib.sha256(response.content).hexdigest()
        require(artifact.get("digest") == "sha256:" + digest, EvidenceRefusal.ARTIFACT_INTEGRITY)
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                entries = archive.infolist()
                require(
                    1 <= len(entries) <= 257
                    and len({e.filename for e in entries}) == len(entries)
                    and sum(e.file_size for e in entries) <= MAX_EXPANDED,
                    EvidenceRefusal.ARTIFACT_INTEGRITY,
                )
                for entry in entries:
                    require(
                        not entry.is_dir()
                        and 0 < entry.file_size <= (MAX_RECEIPT if entry.filename == "evaluation-receipt.json" else MAX_FILE)
                        and not entry.filename.startswith("/")
                        and "\\" not in entry.filename
                        and all(part not in {"", ".", ".."} for part in entry.filename.split("/")),
                        EvidenceRefusal.ARTIFACT_INTEGRITY,
                    )
                data = {entry.filename: archive.read(entry) for entry in entries}
                receipt = data.pop("evaluation-receipt.json")
        except (ValueError, KeyError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
            raise EvaluationEvidenceError(EvidenceRefusal.ARTIFACT_INTEGRITY) from None
        evidence = ProviderEvidence(
            repository_id=spec.runner.repository_id,
            workflow_path=spec.runner.workflow_path,
            harness_revision=spec.runner.harness_revision,
            run_id=run_id,
            run_attempt=attempt,
            producer_id=f"github-actions:{spec.runner.repository_id}:{run_id}:{attempt}",
            artifact_ref=f"github/actions/runs/{run_id}/artifacts/{artifact['id']}",
            artifact_hash=digest,
            receipt=receipt,
            artifacts=data,
            observed_at=datetime.now(UTC),
        )
        return validate_evaluation_evidence(expected, evidence)
