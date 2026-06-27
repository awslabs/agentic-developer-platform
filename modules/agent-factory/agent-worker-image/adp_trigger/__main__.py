"""adp-trigger CLI entry point.

Usage:
  adp-trigger --persona <persona> --issue <number> [--repo <owner/repo>] [--reason <text>]

Triggers another agent persona via the POST /agent/trigger API route.
Reads lineage context (ADP_CORRELATION_ID, ADP_MESSAGE_ID, ADP_CHAIN_DEPTH)
from the pod environment and SigV4-signs the request with IRSA credentials.

Must run inside an agent pod. Exits with code 2 if required env vars are missing.

Examples:
  adp-trigger --persona reviewer --issue 42
  adp-trigger --persona operations --issue 100 --repo aws-e/adp --reason "deploy needed"
"""

from __future__ import annotations

import json
import os
import sys

from adp_trigger.client import build_body, send_trigger


def _usage() -> None:
    print(
        "Usage:\n"
        "  adp-trigger --persona PERSONA --issue NUMBER [--repo OWNER/REPO] [--reason TEXT]\n"
        "\n"
        "Triggers another agent persona via the API-based trigger path.\n"
        "Must run inside an agent pod (requires ADP_CORRELATION_ID, ADP_MESSAGE_ID,\n"
        "ADP_CHAIN_DEPTH, ADP_TRIGGER_ENDPOINT environment variables).\n"
        "\n"
        "Options:\n"
        "  --persona  Target persona to trigger (e.g. developer, reviewer, operations)\n"
        "  --issue    GitHub issue number to dispatch on\n"
        "  --repo     Target repository (default: current repo from GITHUB_REPOSITORY)\n"
        "  --reason   Optional reason for the trigger (shown in Activity)\n",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> None:
    args = sys.argv[1:]

    if not args or "--help" in args or "-h" in args:
        _usage()

    persona: str | None = None
    issue: int | None = None
    repo: str | None = None
    reason: str | None = None

    i = 0
    while i < len(args):
        if args[i] == "--persona" and i + 1 < len(args):
            persona = args[i + 1]
            i += 2
        elif args[i] == "--issue" and i + 1 < len(args):
            try:
                issue = int(args[i + 1])
            except ValueError:
                print(f"error: --issue must be a number, got: {args[i + 1]}", file=sys.stderr)
                sys.exit(2)
            i += 2
        elif args[i] == "--repo" and i + 1 < len(args):
            repo = args[i + 1]
            i += 2
        elif args[i] == "--reason" and i + 1 < len(args):
            reason = args[i + 1]
            i += 2
        else:
            print(f"error: unknown argument: {args[i]}", file=sys.stderr)
            _usage()

    # Validate required arguments
    if not persona:
        print("error: --persona is required", file=sys.stderr)
        _usage()
    if issue is None:
        print("error: --issue is required", file=sys.stderr)
        _usage()

    # Default repo from environment
    if not repo:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not repo:
            print(
                "error: --repo is required (GITHUB_REPOSITORY not set in environment)",
                file=sys.stderr,
            )
            sys.exit(2)

    # Build the request body (validates env vars, exits 2 if missing)
    body = build_body(persona=persona, issue=issue, repo=repo, reason=reason)

    # Send the SigV4-signed request
    result = send_trigger(body)

    # Output result as JSON
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
