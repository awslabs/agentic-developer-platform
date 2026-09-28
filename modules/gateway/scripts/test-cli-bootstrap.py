#!/usr/bin/env python3
"""Check publicly served CLI files, then native admin login and session refresh.

Credentials come only from a private JSON file. Run against an explicitly named
Cognito deployment; the report never contains credentials or process output.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cli"))
import adp_common as common  # noqa: E402

FILES = ("install.sh", "adp", "bg-cognito-auth.sh", "bg-gateway-proxy.py", "adp_common.py", "adp-admin.py")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--expected-pool", required=True)
    parser.add_argument("--expected-client", required=True, help="Native CLI app client ID, distinct from the browser app client")
    parser.add_argument("--expected-cli-dir", required=True, type=Path)
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--artifacts-only", action="store_true")
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args(argv)
    if not args.artifacts_only and not args.credentials_file:
        parser.error("--credentials-file is required unless --artifacts-only is selected")
    base = args.gateway_url.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error("Use the deployment's HTTPS gateway URL")
    base = base if parsed.path.endswith("/api") else base + "/api"
    report = {"gateway_url": base, "pool": args.expected_pool, "client": args.expected_client, "checks": {}, "status": "failed"}
    stage = "deployment"
    opener = urllib.request.build_opener(common.NoRedirect())

    def fetch(path):
        with opener.open(base + path, timeout=30) as response:
            return response.read()

    try:
        discovery = json.loads(fetch("/.well-known/cognito-config"))
        if discovery["user_pool_id"] != args.expected_pool or discovery.get("cli_client_id") != args.expected_client:
            raise common.CliError("Gateway Cognito binding differs from the requested deployment.", "deployment_mismatch")
        report["checks"][stage] = "passed"
        files = [*FILES]
        if (args.expected_cli_dir / "adp-bedrock.py").is_file():
            files.append("adp-bedrock.py")
        with tempfile.TemporaryDirectory(prefix="adp-bootstrap-smoke-") as temporary:
            root = Path(temporary)
            sources = root / "source"
            sources.mkdir(mode=0o700)
            stage = "served_artifacts"
            hashes = {}
            for name in files:
                actual = fetch("/cli/" + name)
                hashes[name] = digest(actual)
                if hashes[name] != digest((args.expected_cli_dir / name).read_bytes()):
                    raise common.CliError("Served CLI differs from the expected revision.", "artifact_mismatch")
                (sources / name).write_bytes(actual)
            report["artifact_sha256"] = hashes
            report["checks"][stage] = "passed"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "ADP_", "BG_", "ANTHROPIC_", "OPENAI_"))}
            env["HOME"] = str(root)
            install = root / "bin"

            def run(command, data=None):
                result = subprocess.run(command, input=data, capture_output=True, text=True, timeout=180, env=env)
                if result.returncode:
                    raise common.CliError("CLI smoke step failed; rerun the named step with the test identity.", "cli_step_failed")
                return result

            stage = "fresh_install"
            run(["sh", str(sources / "install.sh"), "--prefix", str(install), "--gateway-url", base, "--no-path-edit"])
            for name in files:
                if name != "install.sh" and digest((install / name).read_bytes()) != hashes[name]:
                    raise common.CliError("Installed CLI differs from the served files.", "artifact_mismatch")
            report["checks"][stage] = "passed"
            if not args.artifacts_only:
                stage = "native_admin_login"
                credentials = common.read_private_json(args.credentials_file)
                run([str(install / "adp"), "admin", "login", "--credentials-stdin", "--json"], json.dumps(credentials))
                report["checks"][stage] = "passed"
                stage = "refresh"
                run([str(install / "adp"), "refresh"])
                run([str(install / "adp"), "admin", "setup", "--dry-run", "--json"])
                report["checks"][stage] = "passed"
        report["status"] = "passed"
    except Exception as exc:
        report["checks"][stage] = "failed"
        report["error"] = exc.code if isinstance(exc, common.CliError) else type(exc).__name__
    common.write_json(args.report, report)
    print(json.dumps(report))
    return int(report["status"] != "passed")


if __name__ == "__main__":
    sys.exit(main())
