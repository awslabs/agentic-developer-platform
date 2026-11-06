"""Check protected operator receipts before a tools stage can be journaled."""

import argparse
import json
import re
from pathlib import Path

if __package__:
    from .release_scope import verify_shared_receipt
else:
    from release_scope import verify_shared_receipt


def verify_evaluation_receipt(path, *, source, environment, account):
    receipt = json.loads(Path(path).read_text())
    rows = [row for row in receipt.get("scenarios", []) if row.get("scenario_id") == "SCN-8068-04"]
    if not (receipt.get("live_attestation") is True
            and receipt.get("release_source_revision") == source
            and receipt.get("environment") == environment
            and receipt.get("account") == account
            and type(receipt.get("github_run_id")) is int and receipt["github_run_id"] > 0
            and type(receipt.get("github_run_attempt")) is int and receipt["github_run_attempt"] > 0
            and rows and all(row.get("status") == "pass"
                             and row.get("evidence_scope") == "deployed-target"
                             and row.get("observations", {}).get("evidence_type") == "independent-runtime"
                             and row["observations"].get("account") == account
                             and row["observations"].get("components")
                             and row["observations"].get("receipts") for row in rows)):
        raise ValueError("Coordinator must supply passing protected SCN-8068-04 live evaluation evidence")
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("component", choices=("shared", "evaluation"))
    parser.add_argument("path", type=Path)
    parser.add_argument("source")
    parser.add_argument("environment")
    parser.add_argument("account")
    arguments = parser.parse_args()
    if not re.fullmatch(r"[a-f0-9]{40}", arguments.source):
        parser.error("reviewed source required")
    if arguments.component == "shared":
        verify_shared_receipt(arguments.path, source=arguments.source,
                              environment=arguments.environment, account=arguments.account)
    else:
        verify_evaluation_receipt(arguments.path, source=arguments.source,
                                  environment=arguments.environment, account=arguments.account)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, TypeError) as exc:
        raise SystemExit(str(exc)) from None
