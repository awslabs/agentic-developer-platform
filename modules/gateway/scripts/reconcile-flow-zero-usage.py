#!/usr/bin/env python3
"""Preview provider-evidenced zero-usage settlement; apply with its exact snapshot."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import boto3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.orchestration.zero_usage_recovery import reconcile_zero_usage
from src.shared.database import get_session_factory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("account", "org", "flow", "reason", "region"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--request", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-snapshot")
    args = parser.parse_args()
    if not args.reason.strip() or len(args.request) > 20 or (args.apply and not args.expected_snapshot):
        parser.error("Provide a reason, at most 20 requests, and an expected snapshot when applying")
    result = asyncio.run(reconcile_zero_usage(args, session_factory=get_session_factory(), aws=boto3.Session(region_name=args.region)))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # SDK/SQL exceptions can contain connection details or source contents.
        result = {"error": type(error).__name__}
        if type(error) is ValueError:
            result["detail"] = str(error)
        print(json.dumps(result), file=sys.stderr)
        raise SystemExit(1) from None
