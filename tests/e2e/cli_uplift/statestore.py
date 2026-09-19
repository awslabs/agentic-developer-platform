"""Durable run state, keyed by evaluation ID, private and encrypted.

WHY THIS EXISTS

Run state lived only in the runner's `/tmp`. Every Actions job gets a fresh
runner, so the moment the `evaluate` job ended its state was gone. That made
three things impossible:

- `--mode resume` from a later run. The state it must resume no longer exists,
  so the "resume" mode could only ever resume within one job.
- `--mode cleanup` on a fresh runner. The manifest naming what to delete died
  with the runner, leaving IAM roles, stacks, secrets and Cognito users with no
  record at all — none of which any tag-and-age instance sweep can find.
- Reporting a run by ID after the fact.

WHY NOT AN ACTIONS ARTIFACT

The obvious fix is wrong. This state holds live disposable Cognito passwords,
session tokens and ExternalIds until cleanup completes. An ordinary artifact is
readable by anyone with repository read access and is retained for days, so
uploading it would publish exactly the material the report redacts. Hence S3
with mandatory server-side encryption, a bucket-owner-only ACL, and a key
namespaced by evaluation ID.

WHAT IS VALIDATED ON RESTORE

A restored document is untrusted input until proven to be ours: the embedded
evaluation ID must equal the key we asked for, the config fingerprint must match
the config we were invoked with, and the harness pin must match. Otherwise a
stale or wrong-target state could be resumed and reported as acceptance of the
revision under test. A lease guards against two runners restoring the same
evaluation and interleaving deletions.
"""

from __future__ import annotations

import json
import time

from . import ports as ports_module

# Every object lives under this prefix, so a bucket policy can scope access to
# the evaluation role without granting the whole bucket.
PREFIX = "cli-uplift-eval/state"

# Server-side encryption is not optional. `aws:kms` over SSE-S3 so access is
# auditable in CloudTrail and revocable by key policy.
ENCRYPTION = "aws:kms"

# How long a lease is honoured. Longer than max_run_minutes would strand a
# state after a lost runner; shorter would let a live run be stolen. The runner
# refreshes it, so this is a "no heartbeat for this long" threshold.
LEASE_SECONDS = 30 * 60


class StateStoreError(RuntimeError):
    """Durable state could not be read, written or claimed. Carries no secrets."""


def state_key(evaluation_id):
    return f"{PREFIX}/{evaluation_id}/state.json"


def manifest_key(evaluation_id):
    return f"{PREFIX}/{evaluation_id}/manifest.json"


def lease_key(evaluation_id):
    return f"{PREFIX}/{evaluation_id}/lease.json"


class S3StateStore:
    """Encrypted, ID-addressed state in S3.

    Deliberately not a general key-value helper: it only ever touches keys under
    `PREFIX/<evaluation_id>/`, so a bug cannot make it read or overwrite anything
    else in the bucket.
    """

    def __init__(self, aws, bucket, *, kms_key_id=None, clock=time.time):
        if not bucket:
            raise StateStoreError("No state bucket is configured")
        self.aws = aws
        self.bucket = bucket
        self.kms_key_id = kms_key_id
        self.clock = clock

    def _put(self, key, document):
        extra = {}
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        self.aws.call(
            "s3",
            "put_object",
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(document, indent=2, sort_keys=True).encode(),
            ServerSideEncryption=ENCRYPTION,
            # Defence in depth against a bucket that is misconfigured for public
            # or cross-account read: the object itself grants only the owner.
            ACL="bucket-owner-full-control",
            ContentType="application/json",
            **extra,
        )

    def _get(self, key):
        try:
            response = self.aws.call("s3", "get_object", Bucket=self.bucket, Key=key)
        except ports_module.PortError:
            return None
        body = response.get("Body")
        raw = body.read() if hasattr(body, "read") else body
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise StateStoreError(
                f"Durable state at {key} is not valid JSON; refusing to resume"
            ) from None

    def save(self, evaluation_id, document, manifest=None):
        """Persist state (and manifest) for one evaluation."""
        self._put(state_key(evaluation_id), document)
        if manifest is not None:
            self._put(manifest_key(evaluation_id), manifest)
        return True

    def load(self, evaluation_id):
        """Return (document, manifest); either may be None if absent."""
        return (
            self._get(state_key(evaluation_id)),
            self._get(manifest_key(evaluation_id)),
        )

    def claim(self, evaluation_id, holder):
        """Take the lease, unless a live one is held by someone else.

        Not a strong distributed lock, and it does not pretend to be — S3
        conditional writes would be needed for that. It is a guard against the
        realistic case (two Actions jobs touching one evaluation), and it fails
        CLOSED: an unreadable or ambiguous lease is treated as held.
        """
        existing = self._get(lease_key(evaluation_id))
        now = self.clock()
        if existing and existing.get("holder") != holder:
            age = now - float(existing.get("claimed_at") or 0)
            if age < LEASE_SECONDS:
                raise StateStoreError(
                    f"Evaluation {evaluation_id} is leased by another attempt "
                    f"({int(age)}s ago); refusing to operate on it concurrently"
                )
        self._put(
            lease_key(evaluation_id),
            {"holder": holder, "claimed_at": int(now), "evaluation_id": evaluation_id},
        )
        return True

    def release(self, evaluation_id, holder):
        existing = self._get(lease_key(evaluation_id))
        if existing and existing.get("holder") == holder:
            self._put(
                lease_key(evaluation_id),
                {"holder": None, "released_at": int(self.clock())},
            )
        return True


def check_restored(document, evaluation_id, cfg, *, fingerprint, harness_commit):
    """Prove a restored document is ours, for this target, before using it.

    Restored state is untrusted input. Resuming a state built against another
    revision would publish acceptance of the revision under test using evidence
    collected elsewhere — the same false-green class as the stage-veto bug.
    """
    if not isinstance(document, dict):
        raise StateStoreError(f"No durable state found for evaluation {evaluation_id}")
    actual = document.get("evaluation_id")
    if actual != evaluation_id:
        raise StateStoreError(
            f"Durable state under {evaluation_id} declares itself {actual!r}; refusing to use it"
        )
    if document.get("config_fingerprint") != fingerprint:
        raise StateStoreError(
            f"Durable state for {evaluation_id} was created against a different "
            "target or revision; start a new evaluation instead"
        )
    if document.get("harness_commit") != harness_commit:
        raise StateStoreError(
            f"Durable state for {evaluation_id} was created against harness "
            f"{document.get('harness_commit')!r}, not the pinned {harness_commit!r}"
        )
    return document


def default_store(cfg, aws=None, *, clock=time.time):
    """The live store, or None when no bucket is configured.

    None is honest rather than convenient: without a bucket there IS no durable
    state, and callers say so instead of appearing to have persisted something.
    """
    bucket = cfg.get("state_bucket")
    if not bucket:
        return None
    if aws is None:
        aws = ports_module.default_ports(cfg)["aws"]
    return S3StateStore(
        aws, bucket, kms_key_id=cfg.get("state_kms_key_id"), clock=clock
    )


__all__ = [
    "LEASE_SECONDS",
    "PREFIX",
    "S3StateStore",
    "StateStoreError",
    "check_restored",
    "default_store",
]
