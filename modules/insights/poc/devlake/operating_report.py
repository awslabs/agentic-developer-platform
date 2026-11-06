"""Produce a private evidence index without mistaking offline receipts for live acceptance."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import campaign
import resume_campaign


LIVE_PROOF = {
    "AC-01": "48-hour source-clock ledger, independent reads, query and billing observations",
    "AC-02": "owned fault barriers, native/API/DB readbacks and off-state finalization",
    "AC-03": "pinned upgrade, migration-role probes and restored-image rollback",
    "AC-04": "concurrent scope probes, real DB/S3/KMS denials and revocation barrier",
}


def _status(*values):
    if "fail" in values:
        return "fail"
    if "incomplete" in values:
        return "incomplete"
    if "unavailable" in values:
        return "unavailable"
    if "not_run" in values:
        return "not_run"
    return "pass"


def _receipt_index(receipt):
    if receipt is None:
        return [], []
    return [receipt["attempt_id"]], [receipt["evidence_ref"]]


def build(root, campaign_id, revision, clock=None):
    """Re-evaluate a pinned checkpoint; the result is always incomplete for live acceptance."""
    now = clock() if clock else datetime.now(timezone.utc)
    directory, frozen, limits, _ = resume_campaign.admitted(
        root, campaign_id, revision, now, allow_expired=True)
    state = resume_campaign._read_private(directory / "checkpoint.json", "checkpoint")
    report = resume_campaign._read_private(directory / "report.json", "report")
    if (state["frozen_sha256"] != campaign._digest(frozen) or
            state["limits_sha256"] != campaign._digest(limits) or
            report["sample_sha256"] != state["frozen_sha256"] or
            report["batch_count"] != len(state["batches"])):
        raise ValueError("operating report does not match pinned checkpoint")
    evaluated = {
        "freshness": campaign.verify_campaign.freshness(frozen["updates"], state["reads"], limits["limits"]),
        "bounds": campaign.verify_campaign.operating_bounds(state, limits),
        "recovery": (campaign.verify_campaign.recovery(
            {name: receipt["data"] for name, receipt in state["recovery"].items()}, frozen["required_db_clients"])
            if frozen.get("required_db_clients") else {"status": "not_run", "checks": {}}),
        "compatibility": (campaign.verify_campaign.compatibility(
            state["upgrade"]["data"] if state["upgrade"] else {}, frozen["upgrade"])
            if frozen.get("upgrade") else {"status": "not_run", "checks": {}}),
        "isolation": (campaign.verify_campaign.isolation(
            state["isolation"]["data"] if state["isolation"] else {}, frozen["isolation"])
            if frozen.get("isolation") else {"status": "not_run", "checks": {}}),
        "unattended": campaign.verify_campaign.unattended(
            state["continuation"], state["batches"], state["frozen_sha256"], state["limits_sha256"],
            campaign_id, revision),
    }
    recovery_index = {name: {field: receipt[field] for field in ("attempt_id", "observer", "evidence_ref")}
                      for name, receipt in state["recovery"].items()}
    continuation_index = {step: {field: receipt[field] for field in ("attempt_id", "observer", "evidence_ref")}
                          for step, receipt in state["continuation"].items()}
    upgrade_attempts, upgrade_refs = _receipt_index(state["upgrade"])
    isolation_attempts, isolation_refs = _receipt_index(state["isolation"])
    statuses = [result["status"] for result in evaluated.values()]
    overall_status = ("fail" if "fail" in statuses else "incomplete" if "incomplete" in statuses
                      else "unavailable" if "unavailable" in statuses else "pass")
    if report["status"] != overall_status:
        raise ValueError("operating report verdict differs from checkpoint")
    if (any(report[name] != result for name, result in evaluated.items()
            if name != "unattended" or state["continuation"] or "unattended" in report) or
            report["recovery_receipts"] != recovery_index or
            (state["continuation"] or "continuation_receipts" in report) and
            report.get("continuation_receipts") != continuation_index or
            report["upgrade_receipt"] != ({field: state["upgrade"][field] for field in
                                            ("attempt_id", "observer", "evidence_ref")}
                                           if state["upgrade"] else None) or
            report["isolation_receipt"] != ({field: state["isolation"][field] for field in
                                              ("attempt_id", "observer", "evidence_ref")}
                                             if state["isolation"] else None)):
        raise ValueError("operating report evidence differs from checkpoint")
    recovery_attempts = [item["attempt_id"] for item in recovery_index.values()]
    recovery_refs = [item["evidence_ref"] for item in recovery_index.values()]
    terminal = state["recovery"].get("finalization", {}).get("data", {})
    if isinstance(terminal, dict):
        for attempt in terminal.values():
            if isinstance(attempt, dict):
                if attempt.get("attempt_id"):
                    recovery_attempts.append(attempt["attempt_id"])
                if attempt.get("evidence_ref"):
                    recovery_refs.append(attempt["evidence_ref"])
    attempts = {
        "AC-01": sorted(state["batches"]), "AC-02": recovery_attempts +
        [item["attempt_id"] for item in continuation_index.values()],
        "AC-03": upgrade_attempts, "AC-04": isolation_attempts,
    }
    references = {
        "AC-01": [read["evidence_ref"] for read in state["reads"]],
        "AC-02": recovery_refs + [item["evidence_ref"] for item in continuation_index.values()],
        "AC-03": upgrade_refs, "AC-04": isolation_refs,
    }
    offline = {
        "AC-01": _status(evaluated["freshness"]["status"], evaluated["bounds"]["status"]),
        "AC-02": (_status(evaluated["recovery"]["status"], evaluated["unattended"]["status"])
                  if state["continuation"] else evaluated["recovery"]["status"]),
        "AC-03": evaluated["compatibility"]["status"],
        "AC-04": evaluated["isolation"]["status"],
    }
    acceptance = {
        criterion: {"scenario": f"SCN-{index:02}", "revision": revision, "run_id": campaign_id,
                    "attempt_ids": sorted(set(attempts[criterion])),
                    "private_evidence_refs": sorted(set(references[criterion])),
                    "offline_status": offline[criterion], "live_status": "incomplete",
                    "missing_live_proof": LIVE_PROOF[criterion], "cleanup_status": "unverified"}
        for index, criterion in enumerate(LIVE_PROOF, start=1)
    }
    document = {"scope": "private-offline-evidence-index", "verdict": "incomplete",
                "campaign_id": campaign_id, "revision": revision,
                "sample_sha256": state["frozen_sha256"], "limits_sha256": state["limits_sha256"],
                "acceptance": acceptance, "measurements": {
                    "freshness": evaluated["freshness"], "bounds": evaluated["bounds"]}}
    campaign._save(directory / "operating-report.json", document)
    return document


def main():
    parser = argparse.ArgumentParser(description="Index private pilot receipts without asserting live acceptance")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--revision", required=True)
    arguments = parser.parse_args()
    result = build(arguments.root, arguments.campaign_id, arguments.revision)
    print(json.dumps({"scope": result["scope"], "verdict": result["verdict"],
                      "acceptance_ids": sorted(result["acceptance"])}, sort_keys=True))


if __name__ == "__main__":
    main()
