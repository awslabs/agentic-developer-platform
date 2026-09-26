"""Read-only reconciliation from the pre-launch native identity; never deletes."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
import native_image as image
import producer


def reconcile(plan, receipt):
    image.pattern(receipt["build_id"], r"superplane-native-[a-f0-9]{32}")
    if (receipt["account_id"], receipt["region"]) != (
        plan["account_id"],
        plan["region"],
    ):
        raise image.ImageRefused("reconciliation scope differs")
    if build.aws(plan, "sts", "get-caller-identity")["Account"] != plan["account_id"]:
        raise image.ImageRefused("reconciliation account differs")
    try:
        evidence = producer.observe(plan, receipt["build_id"])
    except Exception as error:
        return {
            "build_id": receipt["build_id"],
            "cleanup": "unknown",
            "error_kind": type(error).__name__,
        }
    return {
        "build_id": receipt["build_id"],
        "cleanup": "review_required",
        "inventory": evidence,
        "note": "Readonly inventory is not a deletion authorization or GPU acceptance receipt.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--native-start", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = reconcile(
        producer.read_plan(args.plan), json.loads(Path(args.native_start).read_text())
    )
    build.write(Path(args.output), result)
    if result["cleanup"] == "unknown":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
