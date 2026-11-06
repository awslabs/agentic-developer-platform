"""Persist minimal live attempt metadata to an operator-owned private file."""

import json
import os
import stat
from pathlib import Path


FIELDS = ("attempt_id", "run_id", "operation", "status", "work_id", "observed_at", "native_job_id", "effect_id", "idempotency_key")


class AttemptJournal:
    def __init__(self, path):
        self.path = Path(path)

    def append(self, attempt):
        required = ("attempt_id", "operation", "status", "work_id", "observed_at")
        if any(not isinstance(attempt.get(field), str) or not attempt[field] for field in required):
            raise ValueError("attempt lacks its durable identity, work or observation time")
        entry = {field: attempt.get(field) for field in FIELDS}
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_mode & 0o077:
                raise ValueError("attempt journal must be a private regular file")
            with os.fdopen(descriptor, "ab", closefd=False) as output:
                output.write(json.dumps(entry, sort_keys=True, separators=(",", ":")).encode() + b"\n")
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return entry

    def read(self):
        descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode) or os.fstat(descriptor).st_mode & 0o077:
                raise ValueError("attempt journal must be a private regular file")
            with os.fdopen(descriptor, "r", closefd=False) as source:
                entries = []
                for line in source:
                    if not line.endswith("\n"):
                        raise ValueError("truncated attempt journal")
                    entries.append(json.loads(line))
        finally:
            os.close(descriptor)
        if any(set(entry) != set(FIELDS) for entry in entries):
            raise ValueError("incomplete or invalid attempt journal")
        return entries


def verify_context_reuse(run, result, publication, receipt, agent_read, visibility,
                         denied_reads, repeated_receipts, event_audit):
    """Check a later agent's independently observed contribution citation.

    ``agent_read`` is an observation of a real later consumer operation, not a
    constructed answer or the producer's original publication response. Its
    contribution count must come from independently authorized Context readback.
    """
    payload = publication["payload"]
    result_ref = payload["result_ref"]
    scope = payload["scope"]
    if (run["run_id"] != result["run_id"] or result["scope"] != scope
            or publication["state"] != "published"
            or publication["result_id"] != result["result_id"]
            or publication["result_version"] != result["revision"]
            or result_ref["record_id"] != result["result_id"]
            or result_ref["record_revision"] != str(result["revision"])
            or payload["run_id"] != run["run_id"]
            or payload["native_job_ref"] != result["native_job_ref"]):
        raise AssertionError("Context publication does not match the terminal Security result")
    sources = run["input"]["sources"]
    snapshots = run["admission"]["source_snapshots"]
    if (not sources or payload["source_refs"] != sources or len(sources) != len(snapshots)
            or any(source["record_revision"].lower() in {"head", "latest", "null"} for source in sources)
            or any(snapshot["source_ref"] != source for source, snapshot in zip(sources, snapshots, strict=True))):
        raise AssertionError("Context contribution cites unpinned source revisions")
    if (receipt != publication["receipt"] or receipt["contribution_id"] != publication["publication_id"]
            or receipt["admitted_schema"] != publication["schema_version"]
            or result_ref not in receipt["record_refs"] or not receipt["consumer_revision"]):
        raise AssertionError("consumer receipt does not match this contribution and result revision")
    if (len(repeated_receipts) < 2 or any(repeat != receipt for repeat in repeated_receipts)
            or agent_read["consumer_count"] != 1):
        raise AssertionError("repeated publication produced another Context contribution")
    if (agent_read["agent_run_id"] == run["run_id"] or not agent_read["agent_run_id"]
            or agent_read["contribution_id"] != publication["publication_id"]
            or agent_read["queried_product_id"] != scope["product_id"]
            or any(agent_read["scope"].get(field) != scope[field]
                   for field in ("tenant_id", "product_id", "project_id"))
            or agent_read["source"] != "context" or not agent_read["observed_at"]):
        raise AssertionError("a later authorized agent did not read the selected Product contribution")
    if (any(visibility["scope"].get(field) != scope[field]
            for field in ("tenant_id", "product_id", "project_id"))
            or agent_read["current_visibility"] != visibility["visibility"]
            or agent_read["visibility_revision"] != visibility["revision"]):
        raise AssertionError("later read lacks current Product visibility evidence")
    citations = agent_read["citations"]
    if not citations or not any(
            citation["result_ref"] == result_ref and citation["manifest_ref"] == payload["manifest_ref"]
            and citation["source_ref"] in sources
            and citation["source_revision"] == citation["source_ref"]["record_revision"]
            and citation["validation"] == payload["validation"]
            for citation in citations):
        raise AssertionError("later agent omitted exact source/evidence revision or uncertainty")
    if (len(denied_reads) != 2 or {item["case"] for item in denied_reads} != {
            "foreign_product", "revoked_access"} or any(
            item["status"] not in {403, 404} or item["disclosed_resources"] for item in denied_reads)):
        raise AssertionError("denied-scope Context counterexample disclosed evidence")
    if (event_audit["complete"] is not True or any(
            event.get("causal_origin") == run["run_id"] for event in event_audit["items"])):
        raise AssertionError("publication caused a feedback assessment loop")
    return {"result_id": result["result_id"], "result_revision": result["revision"],
            "publication_id": publication["publication_id"], "receipt_id": receipt["receipt_id"],
            "agent_run_id": agent_read["agent_run_id"], "source_revisions": [
                source["record_revision"] for source in sources]}
