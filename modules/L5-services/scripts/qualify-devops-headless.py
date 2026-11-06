#!/usr/bin/env python3
"""Read selected existing Product/native results without submitting work or opening a browser."""

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/live/devops"))

from installed_journeys import InstalledJourneyReader, validate_installed_journeys, validate_runs
from journey_driver import HEADLESS_ENTRIES, private_path, validate_driver
from qualification import load_manifest, timestamp

PENDING = ["installed-release-identity", "fresh-api-cli-mcp-execution", "adp-ui-journey", "native-ui-sso-arrival",
           "recovery", "context", "portability", "full-acceptance", "cleanup"]


def preflight(source_sha, fixture_path, evidence_dir, *, now=None):
    manifest = load_manifest(fixture_path)
    now = now or datetime.now(UTC)
    authorization = manifest.get("authorization", {})
    expires = timestamp(authorization.get("expires_at"))
    if (not re.fullmatch(r"[a-f0-9]{40}", source_sha)
            or authorization.get("source_sha") != source_sha
            or manifest["release"]["source_sha"] != source_sha
            or not isinstance(authorization.get("scenarios"), list)
            or "headless" not in authorization["scenarios"]
            or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", authorization.get("run_id", ""))
            or not now < expires <= now + timedelta(hours=24)):
        raise ValueError("Headless progress needs current source-bound owner authorization")
    private_path(evidence_dir, directory=True)
    driver = validate_driver(manifest, entries=HEADLESS_ENTRIES)
    validate_runs(manifest.get("installed_journeys", {}).get("runs"), entries=HEADLESS_ENTRIES)
    validate_installed_journeys(manifest, entries=HEADLESS_ENTRIES)
    if timestamp(manifest["installed_journeys"]["started_at"]) > now:
        raise ValueError("Headless authorization has not started")
    for entry in HEADLESS_ENTRIES:
        limit = manifest.get("limits", {}).get(driver["inputs"][entry]["capability"], {})
        try:
            amount = Decimal(str(limit.get("max_spend_usd")))
        except InvalidOperation:
            raise ValueError("Headless readbacks need a finite spend bound") from None
        if (not amount.is_finite() or amount <= 0
                or type(limit.get("max_requests")) is not int or not 1 <= limit["max_requests"] <= 50
                or type(limit.get("deadline_seconds")) is not int
                or not 30 <= limit["deadline_seconds"] <= 900):
            raise ValueError("Headless readbacks need bounded spend, requests and time")
    return manifest


def write_private(path, document):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as sink:
        json.dump(document, sink, sort_keys=True, indent=2)


def run(source_sha, fixture_path, evidence_dir):
    manifest = preflight(source_sha, fixture_path, evidence_dir)
    directory = Path(evidence_dir) / (manifest["authorization"]["run_id"] + "-headless")
    # An interrupted attempt cannot be replayed into its original journal.
    directory.mkdir(mode=0o700)
    write_private(directory / "manifest.json", manifest)
    report = {"schema": "l5-headless-progress/v1", "service": "devops", "qualified": False,
              "source_sha": source_sha, "run_id": manifest["authorization"]["run_id"],
              "completed": [], "pending": PENDING, "browser_started": False}
    capabilities = {manifest["journey_driver"]["inputs"][entry]["capability"] for entry in HEADLESS_ENTRIES}
    seconds = min(sum(manifest["limits"][name]["deadline_seconds"] for name in capabilities),
                  (timestamp(manifest["authorization"]["expires_at"]) - datetime.now(UTC)).total_seconds())
    deadline = time.monotonic() + seconds
    stage = "independent-product-and-provider-readback"
    try:
        reader = InstalledJourneyReader(manifest, deadline=deadline,
            max_requests=sum(manifest["limits"][name]["max_requests"] for name in capabilities),
            entries=HEADLESS_ENTRIES)
        try:
            observed = reader.read(manifest["installed_journeys"]["runs"])
        finally:
            reader.close()
        write_private(directory / "readbacks.json", observed)
        report["completed"] = ["existing-product-and-native-results"]
        report["read_entries"] = list(HEADLESS_ENTRIES)
        report["provider_work_submitted"] = False
        report["status"] = "partial"
    except Exception as error:
        # Provider responses may contain sessions or native identifiers. Keep public
        # diagnostics fixed and use the protected receipt journal for reconciliation.
        report.update(status="failed", failed_stage=stage, error_type=type(error).__name__,
                      next_action="Check the selected run IDs, tenant, binding, provider connection and native task state. Keep existing run IDs; do not resubmit work to repair a readback.")
        write_private(directory / "progress.json", report)
        return 1
    write_private(directory / "progress.json", report)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run"))
    parser.add_argument("source_sha")
    parser.add_argument("fixture_path")
    parser.add_argument("evidence_dir")
    args = parser.parse_args(argv)
    if args.action == "preflight":
        preflight(args.source_sha, args.fixture_path, args.evidence_dir)
        return 0
    status = run(args.source_sha, args.fixture_path, args.evidence_dir)
    print("DevOps headless progress recorded; full qualification and native UI SSO remain pending.")
    return status


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        raise SystemExit("DevOps headless preflight failed: check source authorization, dedicated clients, ready binding, bounds and private evidence directory. No browser is used.") from None
