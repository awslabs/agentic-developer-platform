#!/usr/bin/env python3
"""CLI for human-session verification against the fixture (issue #3968, blocker 7).

Thin wrapper over lib/edge_sessions.py. The logic lives in the module so it is
unit-tested without a cluster; this only resolves tokens from the environment,
chooses the transport, and reports.

Exit codes:
  0  three distinct authenticated sessions
  1  sessions could not be verified (recorded in the artifact, not smoothed over)
  4  a fixture measurement was requested but the pod did not verify

4 is distinct from 1 on purpose: "the pod is not this run's fixture" and "the
tokens do not authenticate" need different operator responses, and collapsing them
would hide a transport substitution behind a credentials error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

import edge_sessions as es  # noqa: E402

# Budget responses authenticate the caller but do not expose identity fields.
# The auth endpoint returns the gateway-validated user and organization IDs.
ENDPOINT = "/auth/me"


def real_runner(argv: Sequence[str], stdin: str = "") -> es.CommandResult:
    """Run a command, passing `stdin` as input so tokens stay out of argv."""
    try:
        proc = subprocess.run(
            list(argv), input=stdin, capture_output=True, text=True, timeout=120
        )
    except Exception as exc:  # pragma: no cover - defensive
        return es.CommandResult(1, "", f"{type(exc).__name__}: {exc}")
    return es.CommandResult(proc.returncode, proc.stdout, proc.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--gateway-url", default="")
    parser.add_argument("--fixture-pod")
    parser.add_argument("--fixture-namespace", default="adp-gateway")
    parser.add_argument("--fixture-port", type=int, default=8080)
    parser.add_argument("--owner-token-env", required=True)
    parser.add_argument("--nonowner-token-env", required=True)
    parser.add_argument("--other-tenant-token-env", required=True)
    args = parser.parse_args(argv)

    if not args.fixture_pod and not args.gateway_url:
        print("FAIL: one of --fixture-pod or --gateway-url is required", file=sys.stderr)
        return 2

    transport = es.choose_transport(
        real_runner,
        run_id=args.run_id,
        fixture_pod=args.fixture_pod,
        fixture_namespace=args.fixture_namespace,
        gateway_url=args.gateway_url,
        fixture_port=args.fixture_port,
    )

    # A requested fixture measurement that did not verify stops here, BEFORE any
    # session is run. Continuing would exercise something other than the fixture
    # and file the result under this run id -- the substitution this module exists
    # to prevent. Nothing is written, so no artifact can be mistaken for evidence.
    if args.fixture_pod and not transport.fixture_verified:
        evidence = transport.evidence or {}
        print("FAIL: a fixture-scoped verification was requested, but the pod did not verify.",
              file=sys.stderr)
        for key in ("reason", "error", "observed_fixture_label", "phase"):
            if evidence.get(key):
                print(f"  {key}: {evidence[key]}", file=sys.stderr)
        print("\n  No sessions were run and no artifact was written. Confirm the fixture exists:\n"
              f"    kubectl get pods -n {args.fixture_namespace} "
              f"-l adp.io/w2-fixture={args.run_id}", file=sys.stderr)
        return 4

    roles = {
        "owner": args.owner_token_env,
        "nonowner": args.nonowner_token_env,
        "other_tenant": args.other_tenant_token_env,
    }
    observed = {
        role: es.run_session(
            real_runner, transport, role=role, env_var=var,
            token=os.environ.get(var) or "", path=ENDPOINT,
        )
        for role, var in roles.items()
    }

    problems = es.assess_sessions(observed)
    report = es.build_report(transport=transport, observed=observed, problems=problems)
    es.write_atomic(args.out, report)

    print(f"ok   wrote {args.out}")
    print(f"     transport: {transport.description}")
    print(f"     fixture_scoped: {report['fixture_scoped']}")
    for role, record in sorted(observed.items()):
        print(f"     {role:13s} status={record.get('status')} user={record.get('user_id')} "
              f"tenant={record.get('org_id') or record.get('tenant_id')}")

    if problems:
        print("\nPROBLEMS (recorded, not worked around):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
