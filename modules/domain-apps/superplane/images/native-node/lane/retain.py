"""Retain compact reconciliation evidence independently of expiring build logs."""

import argparse
from pathlib import Path
import transport

# Fixed application-generated evidence only, never arbitrary uploaded files or logs.
EVIDENCE = (
    "approved-plan.json",
    "source-provenance.json",
    "state.json",
    "base-provenance.json",
    "cleanup-inventory.json",
    "root-evidence.download.json",
    "root-evidence.json",
    "descriptor.json",
    "result.json",
)


def retain(work, bucket, component, region, outcome):
    transport.image.pattern(component, r"[A-Za-z0-9-]+")
    result = work / "result"
    prefix = "receipts/" + component + "/"
    retained = {}
    for name in EVIDENCE:
        source = result / name
        if source.is_file():
            retained[name] = transport.upload(region, bucket, prefix + name, source)
    receipt = {
        "version": 1,
        "process_exit_code": outcome,
        "evidence": retained,
        "cleanup": "review_required"
        if "result.json" in retained and outcome == 0
        else "unknown",
        "note": "Retained receipt does not authorize deletion; missing finalization means unknown.",
    }
    path = work / "retention-receipt.json"
    transport.build.write(path, receipt)
    transport.upload(region, bucket, prefix + "retention-receipt.json", path)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("work", "bucket", "component", "region"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--outcome", required=True, type=int)
    args = parser.parse_args()
    retain(Path(args.work), args.bucket, args.component, args.region, args.outcome)


if __name__ == "__main__":
    main()
