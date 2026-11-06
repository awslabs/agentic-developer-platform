#!/usr/bin/env python3
"""Retain bounded, independently polled GitLab pending-job observations."""
import argparse
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import verify


def collect(fixtures, job_id, gitlab, *, clock=time.monotonic, wait=time.sleep,
            now=lambda: datetime.now(timezone.utc), save=lambda row: None):
    verify.require(type(job_id) is int and job_id > 0, "pending_job_id_invalid")
    project = gitlab(f"projects/{fixtures['project_b']}")
    verify.project_scope(fixtures, "b", project, gitlab, "pending_project_group_wrong")
    started = clock()
    observations = []
    pipeline_id = None
    while True:
        job = gitlab(f"projects/{fixtures['project_b']}/jobs/{job_id}")
        verify.require(isinstance(job, dict), "pending_job_scope_wrong")
        current_pipeline = job.get("pipeline", {}).get("id")
        row = {"observed_at": now().isoformat(), "job_id": job.get("id"),
               "source_revision": job.get("commit", {}).get("id"),
               "pipeline_id": current_pipeline, "status": job.get("status"),
               "runner_id": (job.get("runner") or {}).get("id"), "tag_list": job.get("tag_list")}
        observations.append(row)
        save(row)
        verify.require(row["job_id"] == job_id and row["source_revision"] == fixtures["source_revision"]
                       and row["tag_list"] == [f"adp-group-{fixtures['group_a']}"]
                       and type(current_pipeline) is int and current_pipeline > 0,
                       "pending_job_scope_wrong")
        if pipeline_id is None:
            variables = gitlab(f"projects/{fixtures['project_b']}/pipelines/{current_pipeline}/variables")
            verify.require(isinstance(variables, list) and [item.get("value") for item in variables
                           if item.get("key") == "ADP_QUALIFICATION_RUN_ID"] == [fixtures["run_id"]],
                           "pending_pipeline_run_wrong")
            pipeline_id = current_pipeline
        verify.require(current_pipeline == pipeline_id, "pending_pipeline_changed")
        verify.require(row["status"] == "pending" and row["runner_id"] is None, "group_b_executed")
        elapsed = clock() - started
        verify.require(elapsed <= 330, "pending_poll_deadline_exceeded")
        if elapsed >= 300:
            break
        wait(min(30, 300 - elapsed))
    timestamps = [verify.timestamp(row["observed_at"]) for row in observations]
    verify.require(len(observations) >= 11
                   and (timestamps[-1] - timestamps[0]).total_seconds() >= 300
                   and all(0 < (right - left).total_seconds() <= 60
                           for left, right in zip(timestamps, timestamps[1:])),
                   "group_b_pending_interval_unproven")
    return observations


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--aws-profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-role", required=True)
    parser.add_argument("--gitlab-url", required=True)
    parser.add_argument("--admin-secret-arn", required=True)
    parser.add_argument("--ca-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        fixtures = verify.private_json(args.fixtures)
        from jsonschema import Draft202012Validator
        schema = json.loads(Path(verify.__file__).with_name("fixtures.schema.json").read_text())
        verify.require(Draft202012Validator(schema).is_valid(fixtures), "fixture_schema_invalid")
        _, gitlab, _ = verify.live_clients(args, fixtures)
        directory = args.output.parent.lstat()
        verify.require(stat.S_ISDIR(directory.st_mode) and directory.st_uid == os.getuid()
                       and stat.S_IMODE(directory.st_mode) == 0o700, "private_output_directory_required")
        descriptor = os.open(args.output, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            def save(row):
                output.write(json.dumps(row, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())

            observations = collect(fixtures, args.job_id, gitlab, save=save)
        print(json.dumps({"status": "pending_interval_observed", "observation_count": len(observations)}))
        return 0
    except Exception:
        print(json.dumps({"status": "incomplete", "reason": "private_pending_observation_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
