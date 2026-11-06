"""Bounded, fail-closed continuation for a privately admitted 48-hour sample."""

import argparse
import json
import os
import re
import stat
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import campaign
from verify_campaign import _instant


WINDOWS = 12
WINDOW_HOURS = 4
MINIMUM_AUTHORITY_HOURS = 56
BATCH_GRACE_MINUTES = 20


def _clock():
    return datetime.now(timezone.utc)


def _read_private(path, kind):
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("private campaign input is missing or not mode 0600")
    document = json.loads(path.read_text())
    if kind == "checkpoint" and isinstance(document, dict):
        document.setdefault("recovery", {})
        document.setdefault("upgrade", None)
        document.setdefault("isolation", None)
        document.setdefault("continuation", {})
    campaign.campaign_schema.validate(document, kind)
    if kind == "checkpoint":
        _verify_checkpoint_sources(path.parent, document)
    return document


def _verify_checkpoint_sources(directory, state):
    batch_ids = [f"window-{window:02}" for window in range(len(state["batches"]))]
    if len(batch_ids) > WINDOWS or set(state["batches"]) != set(batch_ids):
        raise ValueError("checkpoint window inventory is inconsistent with original observations")
    aggregate = {"reads": [], "query_seconds": [], "jobs": [], "storage_gib_per_cell": [],
                 "cost_usd": None, "recovery": {}, "upgrade": None, "isolation": None,
                 "continuation": {}}
    for batch_id in batch_ids:
        batch = _read_private(directory / f"{batch_id}.json", "batch")
        if batch["batch_id"] != batch_id or campaign._digest(batch) != state["batches"][batch_id]:
            raise ValueError("checkpoint batch is inconsistent with original observation source")
        aggregate["reads"].extend(batch["reads"])
        measurements = batch.get("measurements", {})
        for field in ("query_seconds", "jobs", "storage_gib_per_cell"):
            aggregate[field].extend(measurements.get(field, []))
        if "cost_usd" in measurements:
            aggregate["cost_usd"] = measurements["cost_usd"]
        aggregate["recovery"].update(batch.get("recovery", {}))
        if batch.get("upgrade") is not None:
            aggregate["upgrade"] = batch["upgrade"]
        if batch.get("isolation") is not None:
            aggregate["isolation"] = batch["isolation"]
        aggregate["continuation"].update(batch.get("continuation", {}))
    if any(state[field] != value for field, value in aggregate.items()):
        raise ValueError("checkpoint evidence differs from original observation sources; checkpoint inconsistent")


def admitted(root, campaign_id, revision, now, allow_expired=False):
    """Reject absent launch bindings, changed pins and short authority windows."""
    root = Path(root)
    if (not root.is_absolute() or root.is_symlink() or not root.is_dir() or
            root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) & 0o077 or
            not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", campaign_id)):
        raise ValueError("private owned campaign root and safe campaign ID required")
    directory = root / campaign_id
    if directory.is_symlink() or not directory.is_dir() or stat.S_IMODE(directory.stat().st_mode) & 0o077:
        raise ValueError("private campaign directory required")
    launch = _read_private(directory / "launch.json", "launch")
    frozen = _read_private(directory / "frozen.json", "frozen")
    limits = _read_private(directory / "approved-limits.json", "limits")
    start = _instant(launch["planned_start_at"])
    minimum = start + timedelta(hours=MINIMUM_AUTHORITY_HOURS)
    if (launch.get("campaign_id") != campaign_id or launch.get("revision") != revision or
            launch.get("frozen_sha256") != campaign._digest(frozen) or
            launch.get("limits_sha256") != campaign._digest(limits) or
            limits.get("approved") is not True or
            any(not launch.get(key) for key in ("source_consent_ref", "runner_capacity_ref",
                                                   "authority_ref", "engine_qualification_ref")) or
            any(_instant(launch[key]) < minimum for key in
                ("engine_until", "workflow_until", "authority_until", "expires_at")) or
            (not allow_expired and now > _instant(launch["expires_at"]))):
        raise ValueError("pilot launch pins, approvals or >=56h authority window are unavailable")
    return directory, frozen, limits, start


def campaign_from_manifest(root, manifest_path, revision, clock=_clock):
    """Bind an authored manifest to one private, already-admitted campaign."""
    root = Path(root)
    manifest_path = Path(manifest_path)
    if (not root.is_absolute() or not manifest_path.is_absolute() or
            manifest_path.name != "manifest.json" or manifest_path.parent.parent != root or
            manifest_path.parent.is_symlink() or manifest_path.is_symlink()):
        raise ValueError("launch manifest must be inside the protected campaign root")
    campaign_id = manifest_path.parent.name
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", campaign_id):
        raise ValueError("invalid campaign ID in launch manifest path")
    if not manifest_path.is_file() or stat.S_IMODE(manifest_path.stat().st_mode) & 0o077:
        raise ValueError("private launch manifest is missing or not mode 0600")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("private launch manifest is invalid") from error
    if (not isinstance(manifest, dict) or manifest.get("campaign_id") != campaign_id or
            manifest.get("source_revision") != revision):
        raise ValueError("launch manifest campaign or source revision differs")
    admitted(root, campaign_id, revision, clock())
    return campaign_id


def run_window(root, campaign_id, revision, window, clock=_clock, sleeper=time.sleep):
    """Wait for one real observation window, then checkpoint only its frozen updates."""
    if window not in range(WINDOWS):
        raise ValueError("only twelve ordered four-hour windows are supported")
    directory, frozen, limits, start = admitted(root, campaign_id, revision, clock())
    end = start + timedelta(hours=(window + 1) * WINDOW_HOURS)
    if clock() < end - timedelta(hours=WINDOW_HOURS, minutes=5):
        raise ValueError("measurement window has not started")
    checkpoint = directory / "checkpoint.json"
    history = _read_private(checkpoint, "checkpoint") if checkpoint.exists() else {"batches": {}}
    if window and f"window-{window - 1:02}" not in history["batches"]:
        raise ValueError("predecessor window is not checkpointed")
    batch_id = f"window-{window:02}"
    deadline = end + timedelta(minutes=BATCH_GRACE_MINUTES)
    while True:
        remaining = (end - clock()).total_seconds()
        if remaining <= 0:
            break
        sleeper(min(60, remaining))
    batch_path = directory / f"window-{window:02}.json"
    while not batch_path.exists():
        remaining = (deadline - clock()).total_seconds()
        if remaining <= 0:
            break
        sleeper(min(60, remaining))
    if batch_path.exists() and clock() > deadline and batch_id not in history["batches"]:
        raise ValueError("observation source missed the approved checkpoint window")
    batch = _read_private(batch_path, "batch")
    if batch.get("batch_id") != f"window-{window:02}":
        raise ValueError("observation batch differs from scheduled window")
    observed = {update["update_id"]: _instant(update["source_at"]) for update in frozen["updates"]}
    lower = start + timedelta(hours=window * WINDOW_HOURS)
    for read in batch["reads"]:
        source_time = observed.get(read["update_id"])
        if (source_time is None or source_time < lower or
                (source_time > end if window == WINDOWS - 1 else source_time >= end)):
            raise ValueError("update belongs to a different frozen observation window")
    for receipt in batch.get("continuation", {}).values():
        data = receipt.get("data", {})
        observed_at = _instant(data.get("observed_at"))
        if (observed_at < start or observed_at > clock() or
                any(_instant(data[key]) > clock() for key in ("queued_at", "started_at") if key in data)):
            raise ValueError("unattended observation is outside elapsed time")
    return campaign.record(frozen, batch, limits, checkpoint, directory / "report.json",
                           campaign_id=campaign_id, revision=revision)


def qualify(root, campaign_id, revision, clock=_clock):
    directory, frozen, limits, start = admitted(root, campaign_id, revision, clock())
    if clock() < start + timedelta(hours=WINDOWS * WINDOW_HOURS):
        raise ValueError("48-hour observation window has not elapsed")
    state = _read_private(directory / "checkpoint.json", "checkpoint")
    report = _read_private(directory / "report.json", "report")
    if (set(state["batches"]) != {f"window-{index:02}" for index in range(WINDOWS)} or
            state["frozen_sha256"] != campaign._digest(frozen) or
            state["limits_sha256"] != campaign._digest(limits) or
            report["sample_sha256"] != state["frozen_sha256"] or
            report["batch_count"] != len(state["batches"])):
        raise ValueError("required windows, sample or operating proof are incomplete")
    freshness = campaign.verify_campaign.freshness(frozen["updates"], state["reads"], limits["limits"])
    bounds = campaign.verify_campaign.operating_bounds(state, limits)
    if (freshness["status"] != "pass" or bounds["status"] != "pass" or
            report["freshness"] != freshness or report["bounds"] != bounds):
        raise ValueError("freshness or operating readback is incomplete, failed or inconsistent")
    return {"scope": "offline-evidence-evaluation", "freshness": "pass", "bounds": "pass",
            "windows": WINDOWS}


def qualify_recovery(root, campaign_id, revision, clock=_clock):
    """Read back all checkpointed recovery cases, including off-state finalization."""
    directory, frozen, limits, _ = admitted(root, campaign_id, revision, clock())
    if not frozen.get("required_db_clients"):
        raise ValueError("frozen DB client inventory is required for recovery")
    state = _read_private(directory / "checkpoint.json", "checkpoint")
    report = _read_private(directory / "report.json", "report")
    receipts = state.get("recovery", {})
    if (state.get("frozen_sha256") != campaign._digest(frozen) or
            state.get("limits_sha256") != campaign._digest(limits) or
            report.get("sample_sha256") != state["frozen_sha256"] or
            not state.get("batches") or report.get("batch_count") != len(state["batches"]) or
            not isinstance(receipts, dict) or set(receipts) != campaign.RECOVERY_CASES):
        raise ValueError("recovery pins, windows or cases are incomplete")
    for name, receipt in receipts.items():
        if (not isinstance(receipt, dict) or
                not all(receipt.get(field) for field in ("attempt_id", "observer", "evidence_ref")) or
                not isinstance(receipt.get("data"), dict)):
            raise ValueError("recovery case lacks independent attempt evidence")
        if name == "finalization" and (set(receipt["data"]) != {"failed", "aborted", "expired"} or
                any(not isinstance(attempt, dict) or
                    not all(attempt.get(field) for field in ("attempt_id", "evidence_ref"))
                    for attempt in receipt["data"].values())):
            raise ValueError("finalization attempts are incomplete")
    result = campaign.verify_campaign.recovery(
        {name: receipt["data"] for name, receipt in receipts.items()}, frozen["required_db_clients"])
    metadata = {name: {field: receipt[field] for field in ("attempt_id", "observer", "evidence_ref")}
                for name, receipt in receipts.items()}
    if (result["status"] != "pass" or report.get("recovery") != result or
            report.get("recovery_receipts") != metadata):
        raise ValueError("recovery readback is missing, failed or inconsistent")
    return {"scope": "offline-evidence-evaluation", "status": "pass",
            "batch_count": len(state["batches"]), "scenario": "SCN-02"}


def qualify_compatibility(root, campaign_id, revision, clock=_clock):
    """Read back the pinned upgrade, migration and restored-image receipt."""
    directory, frozen, limits, _ = admitted(root, campaign_id, revision, clock())
    if not frozen.get("upgrade"):
        raise ValueError("frozen upgrade pins are required for compatibility")
    state = _read_private(directory / "checkpoint.json", "checkpoint")
    report = _read_private(directory / "report.json", "report")
    if (state.get("frozen_sha256") != campaign._digest(frozen) or
            state.get("limits_sha256") != campaign._digest(limits) or
            report.get("sample_sha256") != state["frozen_sha256"] or
            not state.get("batches") or report.get("batch_count") != len(state["batches"])):
        raise ValueError("compatibility pins or windows are incomplete")
    receipt = state.get("upgrade")
    if (not isinstance(receipt, dict) or
            not all(receipt.get(field) for field in ("attempt_id", "observer", "evidence_ref")) or
            not isinstance(receipt.get("data"), dict)):
        raise ValueError("upgrade attempt and independent evidence are required")
    result = campaign.verify_campaign.compatibility(receipt["data"], frozen["upgrade"])
    metadata = {field: receipt[field] for field in ("attempt_id", "observer", "evidence_ref")}
    if (result["status"] != "pass" or report.get("compatibility") != result or
            report.get("upgrade_receipt") != metadata):
        raise ValueError("compatibility readback is missing, failed or inconsistent")
    return {"scope": "offline-evidence-evaluation", "status": "pass",
            "batch_count": len(state["batches"]), "scenario": "SCN-03"}


def qualify_isolation(root, campaign_id, revision, clock=_clock):
    """Read back routed cell, policy, storage and shared-output observations."""
    directory, frozen, limits, _ = admitted(root, campaign_id, revision, clock())
    if not frozen.get("isolation"):
        raise ValueError("frozen isolation scopes are required")
    state = _read_private(directory / "checkpoint.json", "checkpoint")
    report = _read_private(directory / "report.json", "report")
    if (state.get("frozen_sha256") != campaign._digest(frozen) or
            state.get("limits_sha256") != campaign._digest(limits) or
            report.get("sample_sha256") != state["frozen_sha256"] or
            not state.get("batches") or report.get("batch_count") != len(state["batches"])):
        raise ValueError("isolation pins or windows are incomplete")
    receipt = state.get("isolation")
    if (not isinstance(receipt, dict) or
            not all(receipt.get(field) for field in ("attempt_id", "observer", "evidence_ref")) or
            not isinstance(receipt.get("data"), dict)):
        raise ValueError("isolation attempt and independent evidence are required")
    result = campaign.verify_campaign.isolation(receipt["data"], frozen["isolation"])
    metadata = {field: receipt[field] for field in ("attempt_id", "observer", "evidence_ref")}
    if (result["status"] != "pass" or report.get("isolation") != result or
            report.get("isolation_receipt") != metadata):
        raise ValueError("isolation readback is missing, failed or inconsistent")
    return {"scope": "offline-evidence-evaluation", "status": "pass",
            "batch_count": len(state["batches"]), "scenario": "SCN-04"}


def qualify_unattended(root, campaign_id, revision, clock=_clock):
    """Read back restart, workflow, repair and successor receipts without claiming live verification."""
    directory, frozen, limits, _ = admitted(root, campaign_id, revision, clock())
    state = _read_private(directory / "checkpoint.json", "checkpoint")
    report = _read_private(directory / "report.json", "report")
    if (state["frozen_sha256"] != campaign._digest(frozen) or
            state["limits_sha256"] != campaign._digest(limits) or
            report["sample_sha256"] != state["frozen_sha256"] or
            report["batch_count"] != len(state["batches"])):
        raise ValueError("unattended continuation pins or batches changed")
    result = campaign.verify_campaign.unattended(state["continuation"], state["batches"],
                                                 state["frozen_sha256"], state["limits_sha256"],
                                                 campaign_id, revision)
    metadata = {step: {field: receipt[field] for field in ("attempt_id", "observer", "evidence_ref")}
                for step, receipt in state["continuation"].items()}
    if result["status"] != "pass" or report.get("unattended") != result or report.get("continuation_receipts") != metadata:
        raise ValueError("unattended continuation is missing or inconsistent")
    return {"scope": "offline-evidence-evaluation", "status": "pass", "scenario": "AC-02-c3"}


def main(clock=_clock):
    parser = argparse.ArgumentParser(description="Consume only privately bound, checkpointed four-hour evidence")
    parser.add_argument("--root", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--campaign-id")
    source.add_argument("--launch-manifest", type=Path)
    parser.add_argument("--revision", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--window", type=int)
    mode.add_argument("--qualify", action="store_true")
    mode.add_argument("--recovery", action="store_true")
    mode.add_argument("--compatibility", action="store_true")
    mode.add_argument("--isolation", action="store_true")
    mode.add_argument("--unattended", action="store_true")
    arguments = parser.parse_args()
    if arguments.launch_manifest is not None:
        arguments.campaign_id = campaign_from_manifest(arguments.root, arguments.launch_manifest, arguments.revision, clock)
    if arguments.qualify:
        result = qualify(arguments.root, arguments.campaign_id, arguments.revision, clock=clock)
    elif arguments.recovery:
        result = qualify_recovery(arguments.root, arguments.campaign_id, arguments.revision, clock=clock)
    elif arguments.compatibility:
        result = qualify_compatibility(arguments.root, arguments.campaign_id, arguments.revision, clock=clock)
    elif arguments.isolation:
        result = qualify_isolation(arguments.root, arguments.campaign_id, arguments.revision, clock=clock)
    elif arguments.unattended:
        result = qualify_unattended(arguments.root, arguments.campaign_id, arguments.revision, clock=clock)
    else:
        result = run_window(arguments.root, arguments.campaign_id, arguments.revision, arguments.window, clock=clock)
    print(json.dumps({"scope": "offline-evidence-evaluation",
                      "status": result.get("status", "pass"),
                      "window_count": result.get("batch_count", result.get("windows"))}, sort_keys=True))


if __name__ == "__main__":
    main()
