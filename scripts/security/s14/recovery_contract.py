"""One reviewed continuation of the duplicate-tag ECR fixture failure only."""

import hashlib
import json
import re

ORIGIN_RUN = "36105148993"
ORIGIN_WORKFLOW_SHA = "cb92e376b2dc89954d06de4ddfcb78f4dc126ff4"
ORIGIN_CONFIG_SHA = "c0c5980303b49d0256694e439ac4e1710dc7430388e8a2c3add73a7d2b83899f"
ORIGIN_JOURNAL_SHA = "d247c8c74f26b9d5618ff90320abc5b0f50c19b16d97276255063c9d16e64c82"
ORIGIN_ETAG = '"9fcb111845437e4ea71018107c4dfa26"'
ORIGIN_VERSION = "R37qwbtsmAyKq05wgZUd96Jk1I6IVqRD"
ORIGIN_FAILURE = {"exception": "AssertionError", "stage": "ecr"}
ORIGIN_CHECKS = {
    "ssm": True,
    "secret_kms": {"count": 6, "passed": True},
    "negative_resources": True,
}
ROLE_ARN = "arn:aws:iam::879318057152:role/adp-dev-agent-runner-role"
NO_PRIOR_EFFECT_FIELDS = (
    "log_stream",
    "log_event_started",
    "model_invocation_started",
    "build_invocation_started",
    "build_request",
    "build_id",
    "source_upload",
    "source_key",
    "runtime_complete",
    "continuation",
)


def require_origin(record):
    if not (record["workflow_run_id"] == ORIGIN_RUN):
        raise AssertionError()
    if not (record["workflow_sha"] == ORIGIN_WORKFLOW_SHA):
        raise AssertionError()
    if not (record["config_sha256"] == ORIGIN_CONFIG_SHA):
        raise AssertionError()
    if not (record["role_arn"] == ROLE_ARN):
        raise AssertionError()
    if not (
        record["caller_arn"].startswith(
            "arn:aws:sts::879318057152:assumed-role/adp-dev-agent-runner-role/"
        )
    ):
        raise AssertionError()
    if not (record["failure"] == ORIGIN_FAILURE):
        raise AssertionError()
    if not (all(record["checks"].get(k) == v for k, v in ORIGIN_CHECKS.items())):
        raise AssertionError()


def validate_failed_checkpoint(body, etag, version):
    if not (hashlib.sha256(body).hexdigest() == ORIGIN_JOURNAL_SHA):
        raise AssertionError()
    if not (etag == ORIGIN_ETAG and version == ORIGIN_VERSION):
        raise AssertionError()
    record = json.loads(body)
    require_origin(record)
    if not (record["checks"] == ORIGIN_CHECKS):
        raise AssertionError()
    if not (all(field not in record for field in NO_PRIOR_EFFECT_FIELDS)):
        raise AssertionError()
    return record


def validate_continuation(record, run_id, workflow_sha):
    require_origin(record)
    c = record["continuation"]
    if not (c["origin_journal_sha256"] == ORIGIN_JOURNAL_SHA):
        raise AssertionError()
    if not (c["origin_journal_etag"] == ORIGIN_ETAG):
        raise AssertionError()
    if not (c["origin_journal_version"] == ORIGIN_VERSION):
        raise AssertionError()
    if not (c["reconciled_origin_failure"] == ORIGIN_FAILURE):
        raise AssertionError()
    if not (c["preserved_checks"] == ORIGIN_CHECKS):
        raise AssertionError()
    if not (c["origin_caller_arn"] == record["caller_arn"]):
        raise AssertionError()
    if not (c["workflow_run_id"] == run_id != ORIGIN_RUN):
        raise AssertionError()
    if not (c["workflow_sha"] == workflow_sha):
        raise AssertionError()
    if not (re.fullmatch(r"[0-9]{1,20}", run_id)):
        raise AssertionError()
    if not (re.fullmatch(r"[a-f0-9]{40}", workflow_sha)):
        raise AssertionError()
    return c


def active_failure(record):
    """Keep original failure immutable; only a validated claim reconciles it."""
    if "continuation" not in record:
        return record.get("failure")
    c = record["continuation"]
    try:
        validate_continuation(record, c["workflow_run_id"], c["workflow_sha"])
    except (AssertionError, KeyError, TypeError):
        return {"stage": "continuation", "exception": "InvalidContinuation"}
    return c.get("failure")
