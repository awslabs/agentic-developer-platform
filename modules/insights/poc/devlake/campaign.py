"""Resume offline operating evidence without changing the frozen sample."""

import argparse
import fcntl
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

import campaign_schema
import verify_campaign


RECOVERY_CASES = frozenset(("b1", "source_outage", "revocation", "db_clients", "modes", "profile", "finalization"))


def _digest(document):
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _private(path):
    path = Path(path)
    if path.is_symlink() or (path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077):
        raise ValueError("campaign output must be a private regular file")
    if path.exists() and not path.is_file():
        raise ValueError("campaign output must be a private regular file")
    return path


def _save(path, document):
    path = _private(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".campaign-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(document, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record(frozen, batch, approved_limits, checkpoint, report, campaign_id=None, revision=None):
    """Merge one evidence batch; exact retries are safe, conflicting retries fail."""
    campaign_schema.validate(frozen, "frozen")
    campaign_schema.validate(batch, "batch")
    campaign_schema.validate(approved_limits, "limits")
    checkpoint = _private(checkpoint)
    report = _private(report)
    if checkpoint == report:
        raise ValueError("checkpoint and report must be separate private files")
    if not frozen.get("sample_revision") or not batch.get("batch_id"):
        raise ValueError("sample revision and batch ID are required")
    if not isinstance(batch.get("reads"), list):
        raise ValueError("batch reads must be a list")
    known = {update["update_id"] for update in frozen["updates"]}
    for read in batch["reads"]:
        if read.get("update_id") not in known or not read.get("evidence_ref") or not read.get("observer"):
            raise ValueError("read needs frozen ID, independent observer and private evidence reference")
    recovery_receipts = batch.get("recovery", {})
    if not isinstance(recovery_receipts, dict) or (recovery_receipts and not frozen.get("required_db_clients")):
        raise ValueError("recovery requires a frozen DB client inventory")
    for name, receipt in recovery_receipts.items():
        if (name not in RECOVERY_CASES or not isinstance(receipt, dict) or
                not all(receipt.get(field) for field in ("attempt_id", "evidence_ref", "observer")) or
                not isinstance(receipt.get("data"), dict)):
            raise ValueError("recovery receipt needs a known case, attempt and independent evidence")
        if name == "finalization" and any(
                not isinstance(attempt, dict) or not attempt.get("attempt_id") or not attempt.get("evidence_ref")
                for attempt in receipt["data"].values()):
            raise ValueError("each finalization attempt needs its own private receipt")
    upgrade_receipt = batch.get("upgrade")
    if upgrade_receipt is not None and (
            not frozen.get("upgrade") or not isinstance(upgrade_receipt, dict) or
            not all(upgrade_receipt.get(field) for field in ("attempt_id", "evidence_ref", "observer")) or
            not isinstance(upgrade_receipt.get("data"), dict)):
        raise ValueError("upgrade requires frozen revisions, attempt and private evidence")
    isolation_receipt = batch.get("isolation")
    if isolation_receipt is not None and (
            not frozen.get("isolation") or not isinstance(isolation_receipt, dict) or
            not all(isolation_receipt.get(field) for field in ("attempt_id", "evidence_ref", "observer")) or
            not isinstance(isolation_receipt.get("data"), dict)):
        raise ValueError("isolation requires frozen scopes, attempt and private evidence")
    frozen_digest = _digest(frozen)
    limits_digest = _digest(approved_limits)
    lock = checkpoint.with_name(checkpoint.name + ".lock")
    continuation = batch.get("continuation", {})
    if not isinstance(continuation, dict):
        raise ValueError("continuation receipts must be keyed by step")
    with os.fdopen(os.open(_private(lock), os.O_CREAT | os.O_RDWR, 0o600), "r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        state = (json.loads(checkpoint.read_text()) if checkpoint.exists() else
                 {"frozen_sha256": frozen_digest, "limits_sha256": limits_digest,
                  "batches": {}, "reads": [], "query_seconds": [], "jobs": [],
                  "storage_gib_per_cell": [], "cost_usd": None, "recovery": {}, "upgrade": None, "isolation": None,
                  "continuation": {}})
        state.setdefault("recovery", {})
        state.setdefault("upgrade", None)
        state.setdefault("isolation", None)
        state.setdefault("continuation", {})
        campaign_schema.validate(state, "checkpoint")
        if state["frozen_sha256"] != frozen_digest or state["limits_sha256"] != limits_digest:
            raise ValueError("sample or limits changed after campaign checkpoint")
        batch_id = batch["batch_id"]
        digest = _digest(batch)
        if batch_id in state["batches"]:
            if state["batches"][batch_id] != digest:
                raise ValueError("conflicting evidence batch retry")
        else:
            recorded = {read["update_id"] for read in state["reads"]}
            for read in batch["reads"]:
                identifier = read["update_id"]
                if identifier in recorded:
                    raise ValueError("duplicate update across evidence batches")
                recorded.add(identifier)
            state["reads"].extend(batch["reads"])
            measurements = batch.get("measurements", {})
            state["query_seconds"].extend(measurements.get("query_seconds", []))
            state["jobs"].extend(measurements.get("jobs", []))
            state["storage_gib_per_cell"].extend(measurements.get("storage_gib_per_cell", []))
            if "cost_usd" in measurements:
                state["cost_usd"] = measurements["cost_usd"]
            for name, receipt in recovery_receipts.items():
                if name in state["recovery"]:
                    raise ValueError("conflicting recovery case across batches")
                state["recovery"][name] = receipt
            if upgrade_receipt is not None:
                if state["upgrade"] is not None:
                    raise ValueError("conflicting upgrade receipt across batches")
                state["upgrade"] = upgrade_receipt
            if isolation_receipt is not None:
                if state["isolation"] is not None:
                    raise ValueError("conflicting isolation receipt across batches")
                state["isolation"] = isolation_receipt
            for step, receipt in continuation.items():
                if step in state["continuation"]:
                    raise ValueError("conflicting continuation step across batches")
                state["continuation"][step] = receipt
            state["batches"][batch_id] = digest
            verify_campaign.unattended(state["continuation"], state["batches"], frozen_digest, limits_digest,
                                       campaign_id, revision)
            verify_campaign.freshness(frozen["updates"], state["reads"], approved_limits["limits"])
            verify_campaign.operating_bounds(state, approved_limits)
            if frozen.get("required_db_clients"):
                verify_campaign.recovery({name: receipt["data"] for name, receipt in state["recovery"].items()},
                                         frozen["required_db_clients"])
            if frozen.get("upgrade"):
                verify_campaign.compatibility(state["upgrade"]["data"] if state["upgrade"] else {},
                                              frozen["upgrade"])
            if frozen.get("isolation"):
                verify_campaign.isolation(state["isolation"]["data"] if state["isolation"] else {},
                                          frozen["isolation"])
            campaign_schema.validate(state, "checkpoint")
            _save(checkpoint, state)
        freshness = verify_campaign.freshness(frozen["updates"], state["reads"], approved_limits["limits"])
        bounds = verify_campaign.operating_bounds(state, approved_limits)
        recovery = (verify_campaign.recovery(
            {name: receipt["data"] for name, receipt in state["recovery"].items()}, frozen["required_db_clients"])
            if frozen.get("required_db_clients") else {"status": "not_run", "checks": {}})
        compatibility = (verify_campaign.compatibility(
            state["upgrade"]["data"] if state["upgrade"] else {}, frozen["upgrade"])
            if frozen.get("upgrade") else {"status": "not_run", "checks": {}})
        isolation = (verify_campaign.isolation(
            state["isolation"]["data"] if state["isolation"] else {}, frozen["isolation"])
            if frozen.get("isolation") else {"status": "not_run", "checks": {}})
        unattended = verify_campaign.unattended(state["continuation"], state["batches"], frozen_digest,
                                                 limits_digest, campaign_id, revision)
        statuses = (freshness["status"], bounds["status"], recovery["status"],
                    compatibility["status"], isolation["status"], unattended["status"])
        result = {"scope": "offline-evidence-evaluation", "sample_sha256": frozen_digest,
                  "batch_count": len(state["batches"]), "freshness": freshness, "bounds": bounds,
                  "recovery": recovery, "compatibility": compatibility, "isolation": isolation,
                  "recovery_receipts": {name: {field: receipt[field] for field in ("attempt_id", "observer",
                                                                      "evidence_ref")}
                                        for name, receipt in state["recovery"].items()},
                  "upgrade_receipt": ({field: state["upgrade"][field] for field in
                                       ("attempt_id", "observer", "evidence_ref")} if state["upgrade"] else None),
                  "isolation_receipt": ({field: state["isolation"][field] for field in
                                         ("attempt_id", "observer", "evidence_ref")} if state["isolation"] else None),
                  "unattended": unattended,
                  "continuation_receipts": {step: {field: receipt[field] for field in
                      ("attempt_id", "observer", "evidence_ref")}
                      for step, receipt in state["continuation"].items()},
                  "status": ("fail" if "fail" in statuses else "incomplete" if "incomplete" in statuses
                             else "unavailable" if "unavailable" in statuses else "pass")}
        campaign_schema.validate(result, "report")
        _save(report, result)
        return result


def main():
    parser = argparse.ArgumentParser(description="Evaluate private, independently collected campaign evidence")
    for name in ("frozen", "batch", "approved-limits", "checkpoint", "report"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    result = record(json.loads(args.frozen.read_text()), json.loads(args.batch.read_text()),
                    json.loads(args.approved_limits.read_text()), args.checkpoint, args.report)
    print(json.dumps({"scope": result["scope"], "status": result["status"],
                      "batch_count": result["batch_count"]}, sort_keys=True))


if __name__ == "__main__":
    main()
