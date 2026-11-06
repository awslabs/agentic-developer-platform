"""Synthetic API polling records each attempt without assuming group isolation."""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("pending_collector", ROOT / "poll_pending.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def poll(*, change=None):
    fixtures = {"project_b": 202, "group_a": 10, "group_b": 20, "project_namespace_b": 21,
                "source_revision": "a" * 40, "run_id": "synthetic-run"}
    elapsed = [0]
    attempts = []
    job = {"id": 314, "status": "pending", "runner": None, "commit": {"id": fixtures["source_revision"]},
           "pipeline": {"id": 350}, "tag_list": ["adp-group-10"]}

    def gitlab(path):
        if path == "projects/202":
            return {"id": 202, "namespace": {"id": 21, "kind": "group"}}
        if path == "groups/21":
            return {"id": 21, "parent_id": 20}
        if path == "projects/202/pipelines/350/variables":
            return [{"key": "ADP_QUALIFICATION_RUN_ID", "value": fixtures["run_id"]}]
        assert path == "projects/202/jobs/314"
        copy = {**job}
        if change and elapsed[0] >= 120:
            change(copy)
        return copy

    def wait(seconds):
        elapsed[0] += seconds

    def now():
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=elapsed[0])

    def collect():
        return collector.collect(fixtures, 314, gitlab, clock=lambda: elapsed[0], wait=wait,
                                 now=now, save=attempts.append)

    return collect, attempts


def test_bounded_poll_retains_independent_pending_observations():
    collect, attempts = poll()
    result = collect()
    assert result == attempts
    assert len(result) == 11
    assert result[-1]["observed_at"] == "2026-01-01T00:05:00+00:00"
    assert all(row["runner_id"] is None and row["job_id"] == 314 for row in result)


@pytest.mark.parametrize("change,reason", [
    (lambda job: job.update(status="running"), "group_b_executed"),
    (lambda job: job.update(runner={"id": 42}), "group_b_executed"),
    (lambda job: job.update(commit={"id": "b" * 40}), "pending_job_scope_wrong"),
    (lambda job: job.update(tag_list=["adp-group-20"]), "pending_job_scope_wrong"),
    (lambda job: job.update(pipeline={"id": 351}), "pending_pipeline_changed"),
])
def test_unsafe_or_reassigned_job_is_retained_and_refused(change, reason):
    collect, attempts = poll(change=change)
    with pytest.raises(collector.verify.IncompleteEvidence, match=reason):
        collect()
    assert len(attempts) >= 4 or reason == "pending_job_scope_wrong"
    assert all(row["observed_at"].startswith("2026-01-01T") for row in attempts)


def test_readback_gaps_cannot_establish_five_minutes():
    fixtures = {"project_b": 202, "group_a": 10, "group_b": 20, "project_namespace_b": 20,
                "source_revision": "a" * 40, "run_id": "synthetic-run"}
    ticks = [0]
    def gitlab(path):
        if path == "projects/202":
            return {"id": 202, "namespace": {"id": 20, "kind": "group"}}
        if path.endswith("/variables"):
            return [{"key": "ADP_QUALIFICATION_RUN_ID", "value": fixtures["run_id"]}]
        return {"id": 314, "status": "pending", "runner": None, "commit": {"id": "a" * 40},
                "pipeline": {"id": 350}, "tag_list": ["adp-group-10"]}
    with pytest.raises(collector.verify.IncompleteEvidence, match="pending_interval_unproven"):
        collector.collect(fixtures, 314, gitlab, clock=lambda: ticks[0],
                          wait=lambda seconds: ticks.__setitem__(0, ticks[0] + 61),
                          now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=ticks[0]))
