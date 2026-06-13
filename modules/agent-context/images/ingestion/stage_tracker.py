"""Stage tracker — verify-after-write contract for indexing stages.

Issue #1423: Wraps each producer stage with the discipline:
    attempt -> write row -> produce -> read-back verify -> only-then record verified

This module is the integration layer between the ingest-repo pipeline and the
per-stage Postgres tracking in db.py. It handles:
- Creating the index run header
- Starting/verifying/failing each stage
- Read-back verification of artifacts (S3 head_object, vector count, etc.)
- Skip logic keyed off 'verified' status (not SHA alone)

Usage in ingest-repo.py:
    tracker = StageTracker(conn, repo, repo_id, commit_sha)
    with tracker.stage("clone") as stage:
        # ... do clone work ...
        stage.set_artifact(clone_path)
        stage.verify(lambda: os.path.exists(clone_path))
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Callable

import db as stage_db

log = logging.getLogger("stage_tracker")


@dataclass
class StageResult:
    """Result of a tracked stage execution."""

    stage: str
    status: str  # verified | failed | skipped
    artifact_ref: str | None = None
    error: str | None = None


@dataclass
class StageContext:
    """Context object passed into a stage block. Collects artifact and verification."""

    _stage_name: str
    _artifact_ref: str | None = None
    _verified: bool = False
    _error: str | None = None

    def set_artifact(self, ref: str) -> None:
        """Record the artifact reference (S3 key, path, index name, etc.)."""
        self._artifact_ref = ref

    def verify(self, check_fn: Callable[[], bool]) -> bool:
        """Run the read-back verification. Returns True if artifact confirmed.

        The check_fn should perform the actual read-back: head_object, count query, etc.
        """
        try:
            self._verified = check_fn()
            if not self._verified:
                self._error = "read-back verification failed: artifact not found"
        except Exception as e:
            self._verified = False
            self._error = f"read-back verification error: {e}"
        return self._verified

    def fail(self, error: str) -> None:
        """Explicitly mark this stage as failed (e.g., producer threw)."""
        self._verified = False
        self._error = error


class StageTracker:
    """Manages per-stage tracking for one indexing run.

    Creates the index_runs header and provides a context manager for each stage
    that implements the verify-after-write contract.
    """

    def __init__(
        self,
        conn,
        repo: str,
        repo_id: str,
        commit_sha: str | None = None,
    ):
        self._conn = conn
        self._repo = repo
        self._repo_id = repo_id
        self._commit_sha = commit_sha
        self._run_id = stage_db.create_index_run(conn, repo_id, repo, commit_sha)
        self._results: list[StageResult] = []

    @property
    def run_id(self) -> str:
        """The canonical run_id for this indexing run."""
        return self._run_id

    @property
    def results(self) -> list[StageResult]:
        """All stage results collected so far."""
        return list(self._results)

    def should_skip(self, stage: str) -> bool:
        """Check if this stage is already verified at the current SHA."""
        return stage_db.should_skip_stage(self._conn, self._repo, stage, self._commit_sha)

    @contextmanager
    def stage(self, stage_name: str):
        """Context manager implementing the stage contract.

        Usage:
            with tracker.stage("deepwiki") as ctx:
                wiki = generate_wiki(repo)
                s3_key = upload_to_s3(wiki)
                ctx.set_artifact(s3_key)
                ctx.verify(lambda: s3_head_object(s3_key))

        If an exception is raised inside the block, the stage is marked failed.
        If verify() is never called or returns False, the stage is marked failed.
        """
        ctx = StageContext(_stage_name=stage_name)
        stage_id = stage_db.start_stage(self._conn, self._run_id, self._repo, stage_name)

        try:
            yield ctx
        except Exception as e:
            ctx.fail(str(e))

        # Determine final state
        if ctx._verified and ctx._artifact_ref:
            stage_db.verify_stage(self._conn, stage_id, ctx._artifact_ref)
            self._results.append(StageResult(
                stage=stage_name,
                status="verified",
                artifact_ref=ctx._artifact_ref,
            ))
        else:
            error = ctx._error or "stage completed without verification"
            stage_db.fail_stage(self._conn, stage_id, error)
            self._results.append(StageResult(
                stage=stage_name,
                status="failed",
                error=error,
            ))

    def mark_skipped(self, stage_name: str, reason: str = "disabled") -> None:
        """Record a stage as skipped (feature disabled, not applicable, etc.)."""
        stage_db.skip_stage(self._conn, self._run_id, self._repo, stage_name, reason)
        self._results.append(StageResult(
            stage=stage_name,
            status="skipped",
        ))

    def finalize(self) -> None:
        """Complete the index run header (derives overall status from stages)."""
        stage_db.complete_index_run(self._conn, self._run_id)
