"""Claim the exact reviewed failed checkpoint once; no runtime probes here."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import boto3
from botocore.config import Config
from journal import Journal
from recovery_contract import (
    ORIGIN_CONFIG_SHA,
    ORIGIN_ETAG,
    ORIGIN_VERSION,
    ORIGIN_JOURNAL_SHA,
    ORIGIN_RUN,
    ORIGIN_WORKFLOW_SHA,
    ORIGIN_FAILURE,
    ORIGIN_CHECKS,
    ROLE_ARN,
    validate_failed_checkpoint,
    validate_continuation,
)


def claim(path, s3, cfg, run_id, workflow_sha, caller_arn):
    if not (re.fullmatch(r"[0-9]{1,20}", run_id) and run_id != ORIGIN_RUN):
        raise AssertionError()
    if not (re.fullmatch(r"[a-f0-9]{40}", workflow_sha)):
        raise AssertionError()
    if not (
        caller_arn.startswith(
            "arn:aws:sts::879318057152:assumed-role/adp-dev-agent-runner-role/"
        )
    ):
        raise AssertionError()
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(str(path) + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not (not path.exists()):
            raise AssertionError(
                "Local receipt exists; reconcile instead of claiming again"
            )
        obj = s3.get_object(Bucket=cfg["source_bucket"], Key=cfg["receipt_key"])
        try:
            body = obj["Body"].read(131073)
        finally:
            obj["Body"].close()
        original = validate_failed_checkpoint(body, obj["ETag"], obj.get("VersionId"))
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        journal = Journal(
            path,
            s3,
            cfg["source_bucket"],
            cfg["receipt_key"],
            {
                "config_sha256": ORIGIN_CONFIG_SHA,
                "role_arn": ROLE_ARN,
                "workflow_run_id": ORIGIN_RUN,
                "workflow_sha": ORIGIN_WORKFLOW_SHA,
            },
        )
        if not (journal.etag == ORIGIN_ETAG):
            raise AssertionError()
        journal.result["continuation"] = {
            "reason": "Validate and deduplicate identical ECR manifests returned for multiple tags",
            "origin_journal_sha256": ORIGIN_JOURNAL_SHA,
            "origin_journal_etag": ORIGIN_ETAG,
            "origin_journal_version": ORIGIN_VERSION,
            "origin_caller_arn": original["caller_arn"],
            "reconciled_origin_failure": ORIGIN_FAILURE,
            "preserved_checks": ORIGIN_CHECKS,
            "workflow_run_id": run_id,
            "workflow_sha": workflow_sha,
            "caller_arns": [caller_arn],
        }
        validate_continuation(journal.result, run_id, workflow_sha)
        # Sole mutation: append continuation. All prior evidence remains intact.
        if not (
            {k: v for k, v in journal.result.items() if k != "continuation"} == original
        ):
            raise AssertionError()
        journal.save()  # Conditional IfMatch; an uncertain result is never retried.
        path.with_name("summary.json").write_text(
            json.dumps(journal.summary(), indent=2) + "\n"
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    body = Path(args.config).read_bytes()
    if not (hashlib.sha256(body).hexdigest() == ORIGIN_CONFIG_SHA):
        raise AssertionError()
    if not args.execute:
        print(
            json.dumps({"validated": True, "executed": False, "origin_run": ORIGIN_RUN})
        )
        return
    if not (os.environ["GITHUB_REF"] == "refs/heads/main"):
        raise AssertionError()
    if not (os.environ["GITHUB_RUN_ATTEMPT"] == "1"):
        raise AssertionError()
    cfg = json.loads(body)
    session = boto3.Session(region_name=cfg["region"])
    if not (session.get_credentials().method == "assume-role-with-web-identity"):
        raise AssertionError()
    client_config = Config(
        connect_timeout=10, read_timeout=30, retries={"total_max_attempts": 1}
    )
    identity = session.client("sts", config=client_config).get_caller_identity()
    if not (identity["Account"] == cfg["account"]):
        raise AssertionError()
    claim(
        args.receipt,
        session.client("s3", config=client_config),
        cfg,
        os.environ["GITHUB_RUN_ID"],
        os.environ["GITHUB_SHA"],
        identity["Arn"],
    )
    print(
        json.dumps(
            {"claimed": True, "origin_run": ORIGIN_RUN, "runtime_executed": False}
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            json.dumps(
                {
                    "claimed": False,
                    "exception": type(exc).__name__,
                    "reconciliation_required": True,
                }
            )
        )
        sys.exit(1)
