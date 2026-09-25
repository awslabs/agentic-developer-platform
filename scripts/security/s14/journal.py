"""Conditional S3 journal for one acceptance attempt; contains no secret values.

The key is fixed in reviewed configuration. A fresh runner may not replace an
existing journal. Local/remote disagreement or an uncertain write requires
operator reconciliation, never automatic resume or a new acceptance key.
"""

import hashlib
import json
import os
from pathlib import Path
from recovery_contract import active_failure


class ReconciliationRequired(RuntimeError):
    pass


class Journal:
    def __init__(self, path, s3, bucket, key, initial):
        self.path = Path(path)
        self.s3, self.bucket, self.key = s3, bucket, key
        self.etag = None
        self.poisoned = False
        self.last_committed = None
        if self.path.exists():
            self.result = json.loads(self.path.read_text())
            remote = s3.get_object(Bucket=bucket, Key=key)
            try:
                # Receipts are small; reject an oversized/unexpected object.
                body = remote["Body"].read(131073)
                if len(body) > 131072 or json.loads(body) != self.result:
                    raise ReconciliationRequired("Local/remote journal mismatch")
            finally:
                remote["Body"].close()
            self.etag = remote["ETag"]
            self.last_committed = json.loads(body)
            if any(
                self.result.get(k) != v for k, v in initial.items() if k != "checks"
            ):
                raise ReconciliationRequired("Acceptance context changed")
        else:
            self.result = initial
            self.save()  # IfNoneMatch prevents replacement, including a lost pod.

    def save(self):
        if self.poisoned:
            raise ReconciliationRequired("Prior journal write outcome uncertain")
        body = (json.dumps(self.result, sort_keys=True, indent=2) + "\n").encode()
        if len(body) > 131072:
            raise ReconciliationRequired("Receipt exceeds limit")
        # Persist local intent before remote I/O. Do not repeat a side effect if
        # either side of this commit becomes uncertain.
        temporary = self.path.with_suffix(".pending")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        directory_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        condition = {"IfMatch": self.etag} if self.etag else {"IfNoneMatch": "*"}
        try:
            response = self.s3.put_object(
                Bucket=self.bucket,
                Key=self.key,
                Body=body,
                ContentType="application/json",
                **condition,
            )
            self.etag = response["ETag"]
            self.last_committed = json.loads(body)
        except Exception as exc:
            self.poisoned = True
            raise ReconciliationRequired(
                "Journal write requires reconciliation"
            ) from exc

    def summary(self):
        """Only this compact result is uploaded to Actions, not the full journal."""
        committed = self.last_committed or {}
        reconciled = not self.poisoned and not active_failure(self.result)
        return {
            "reconciliation_required": not reconciled,
            "receipt_sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
            "caller_arn": self.result.get("continuation", {}).get(
                "caller_arns", [self.result.get("caller_arn")]
            )[-1],
            "origin_run_id": self.result.get("workflow_run_id"),
            "continuation_run_id": self.result.get("continuation", {}).get(
                "workflow_run_id"
            ),
            "reconciled_origin_failure": self.result.get("failure")
            if "continuation" in self.result
            else None,
            "source_sha": self.result.get("source_sha"),
            "runtime_complete": self.result.get("runtime_complete", False),
            "build_id": self.result.get("build_id"),
            "build_status": self.result.get("build_status"),
            "checks": self.result.get("checks", {}),
            "failure": active_failure(self.result),
            "complete": bool(
                reconciled
                and committed.get("runtime_complete")
                and committed.get("checks", {}).get("codebuild_pr")
            ),
            "cleanup": "Operator retains source archive, journal and log marker until review; no automatic delete.",
        }
