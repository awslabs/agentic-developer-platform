#!/usr/bin/env python3
"""Run the existing #5173 EC2 hierarchy harness with mapping writes via this CLI.

The harness owns fixtures, EC2 execution, destination evidence and cleanup. This
adapter only replaces its mapping PUT boundary with the actual installed CLI.
Use an explicit checkout of PR #5173 until that harness lands on main.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def resume_matrix(start, state, suites):
    """Keep omitted acceptance gates visible when resuming only some suites."""
    previous = state.data.get("matrix", [])[:]
    start(state, suites)
    state.data["matrix"] = list(dict.fromkeys([*previous, *state.data["matrix"]]))
    state.save()


def cli_command(fixtures, cli_dir, path, body, who):
    """Only fixture-owned org/team/user mappings may be exercised by this adapter."""
    from urllib.parse import unquote

    scope = [unquote(value) for value in path.rsplit("/", 1)[-1].split(":")]
    resources = fixtures.s.data["resources"]
    org = resources["org"]
    if body["destination_id"] != resources["destination_id"]:
        raise RuntimeError("Unexpected destination outside the fixture")
    args = ["admin", "bedrock", "connect", "--destination", resources["destination_id"], "--org", org, "--yes", "--json"]
    if scope == ["org", org]:
        pass
    elif scope == ["team", org, resources["team"]]:
        args += ["--team", resources["team"]]
    elif scope[0] == "user" and len(scope) == 2 and scope[1] in {row.get("adp_id") for row in fixtures.s.data["users"].values()}:
        args += ["--user", scope[1]]
    else:
        raise RuntimeError("Unexpected mapping outside the fixture")
    with tempfile.TemporaryDirectory(prefix="adp-cli-regression-") as temporary:
        directory = Path(temporary)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "ANTHROPIC_", "OPENAI_", "ADP_GATEWAY_"))}
        # Isolated child home only; never touch the operator's session/config.
        env["HOME"] = str(directory)
        installed = directory / "bin"
        result = subprocess.run(
            ["sh", str(cli_dir / "install.sh"), "--prefix", str(installed), "--gateway-url", fixtures.c["gateway_url"], "--no-path-edit"],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode:
            raise RuntimeError("Candidate CLI installation failed")
        auth = fixtures.s.data["users"][who]["tokens"]
        config = directory / ".bedrock-gateway"
        tokens = {
            "access_token": auth["AccessToken"],
            "id_token": auth.get("IdToken", ""),
            "refresh_token": auth.get("RefreshToken", ""),
            "expires_at": int(time.time()) + 600,
        }
        fd = os.open(config / "tokens.json", os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(tokens, output)
        result = subprocess.run([str(installed / "adp"), *args], env=env, capture_output=True, text=True, timeout=180)
        # Do not echo raw process output: failures may include provider data.
        if result.returncode:
            raise RuntimeError("Candidate CLI mapping command failed; inspect the named fixture's routing state")
        outcome = json.loads(result.stdout)
        detail = outcome["detail"]
        if outcome["status"] != "verified" or not detail.get("assigned") or detail["account_id"] != fixtures.c["bedrock_account"]:
            raise RuntimeError("Candidate CLI did not verify and assign the expected destination")
        fixtures.s.check(
            "cli:" + ":".join(scope), True, {"scope": ":".join(scope), "destination_id": detail["destination_id"], "account_id": detail["account_id"]}
        )
        return detail["mapping"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-root", required=True, type=Path)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state-dir", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--cleanup-only", action="store_true")
    mode.add_argument("--resume", action="store_true", help="Rerun against retained fixtures, preserving earlier failed/not-run acceptance checks")
    parser.add_argument("--hosted", action="store_true", help="Also exercise real cloud-agent dispatch at each hierarchy step")
    parser.add_argument("--maintenance-kubeconfig")
    parser.add_argument(
        "--provision", choices=("direct", "handoff"), help="Provision a fresh role with the CLI on EC2, then run Claude/Codex evidence checks"
    )
    args = parser.parse_args()
    if args.hosted and not args.cleanup_only:
        try:
            import websockets  # noqa: F401
        except ImportError:
            parser.error("--hosted requires websockets in the harness Python environment; install it before creating fixtures")
    harness_root = args.harness_root.resolve()
    if not (harness_root / "tests/e2e/tenant_validation/regression.py").is_file():
        parser.error("--harness-root must contain the reusable routing harness from PR #5173")
    sys.path.insert(0, str(harness_root))
    from tests.e2e.tenant_validation import cli as harness
    from tests.e2e.tenant_validation.fixtures import Fixtures

    cli_dir = Path(__file__).resolve().parents[1] / "cli"
    hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in cli_dir.iterdir() if path.is_file()}

    class CliFixtures(Fixtures):
        def api(self, method, path, body=None, who="admin", expected=(200,)):
            if method == "PUT" and path.startswith("/admin/bedrock-routing/mappings/"):
                self.s.data["cli_candidate_hashes"] = hashes
                self.s.save()
                return cli_command(self, cli_dir, path, body, who)
            return super().api(method, path, body, who, expected)

    harness.Fixtures = CliFixtures
    if args.provision:
        if args.hosted:
            parser.error("Use the separate hierarchy run for --hosted")
        from cli_provisioning import install

        install(harness, cli_dir, args.provision)
    if args.resume:
        from tests.e2e.tenant_validation import regression

        previous_start = regression.start_routing_matrix

        def start_resumed(state, suites):
            resume_matrix(previous_start, state, suites)

        regression.start_routing_matrix = start_resumed
    if args.hosted and not args.cleanup_only:
        from tests.e2e.tenant_validation import regression

        original_start = regression.start_routing_matrix
        original_run = harness.run_suites
        original_validate = harness.validate_suites

        def start_matrix(state, suites):
            original_start(state, suites)
            for phase, member, _ in regression.CASES:
                key = f"{phase}:hosted-chat:{member}"
                if key not in state.data["matrix"]:
                    state.data["matrix"].append(key)
                state.data["checks"][key] = {"status": "not_run", "details": {}, "at": harness.now()}
            state.save()

        def run_suites(config, state, aws, fixtures, suites, **kwargs):
            return original_run(config, state, aws, fixtures, [*suites, "hosted-chat"], **kwargs)

        def validate_suites(config, suites):
            return original_validate(config, [*suites, "hosted-chat"])

        regression.start_routing_matrix = start_matrix
        harness.run_suites = run_suites
        harness.validate_suites = validate_suites
    argv = ["cleanup" if args.cleanup_only else "run" if args.resume else "test", "--config", args.config, "--state-dir", args.state_dir]
    if not args.cleanup_only:
        argv += ["--suites", "ec2-claude", "ec2-codex"]
        if not args.provision:
            argv += ["--routing-matrix"]
    if args.maintenance_kubeconfig:
        argv += ["--maintenance-kubeconfig", args.maintenance_kubeconfig]
    return harness.main(argv)


if __name__ == "__main__":
    sys.exit(main())
