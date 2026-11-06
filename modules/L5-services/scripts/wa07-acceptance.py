"""Validate a private WA07 ledger and print only its allowlisted public summary."""

import argparse
import json
import sys
from pathlib import Path

from l5_services.wa_acceptance import sanitized_summary


def main():
    parser = argparse.ArgumentParser(description="Report WA07 acceptance without private evidence")
    parser.add_argument("--private-ledger", required=True, type=Path)
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[3]
    if arguments.private_ledger.resolve().is_relative_to(repository):
        print("Private acceptance ledgers must be kept outside the repository", file=sys.stderr)
        return 2
    try:
        ledger = json.loads(arguments.private_ledger.read_text())
        specs = json.loads((repository / "docs/l5-services/well-architected/story-specs.json").read_text())
        summary = sanitized_summary(ledger, specs)
    except (OSError, ValueError, KeyError, TypeError):
        print("Private acceptance ledger is missing or invalid", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
