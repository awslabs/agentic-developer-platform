#!/usr/bin/env python3
"""EC2-only live CLI provisioning acceptance worker; emits bounded evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def instance_identity():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    with opener.open(request, timeout=3) as response:
        token = response.read().decode()
    request = urllib.request.Request(
        "http://169.254.169.254/latest/dynamic/instance-identity/document",
        headers={"X-aws-ec2-metadata-token": token},
    )
    with opener.open(request, timeout=3) as response:
        return json.load(response)


def execute(config, evidence):
    identity = instance_identity()
    require(
        identity["instanceId"] == config["instance_id"] and identity["accountId"] == config["platform_account"],
        "Worker must run on the owned EC2 instance",
    )
    evidence["instance_id"] = identity["instanceId"]
    os.umask(0o077)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "ADP_", "ANTHROPIC_", "OPENAI_"))}
    env.update(
        AWS_DEFAULT_REGION=config["region"],
        AWS_PAGER="",
        AWS_CLI_AUTO_PROMPT="off",
        # Private STS endpoints can admit only platform services. FIPS remains
        # reachable through this disposable runner's HTTPS egress, like Secrets Manager.
        AWS_ENDPOINT_URL_STS=f"https://sts-fips.{config['region']}.amazonaws.com",
    )

    def aws(args, environment=env, missing=False):
        result = subprocess.run(
            ["aws", "--region", config["region"], "--output", "json", *args], env=environment, capture_output=True, text=True, timeout=660
        )
        if result.returncode:
            if missing and "(ValidationError)" in result.stderr and "does not exist" in result.stderr:
                return None
            code = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            raise RuntimeError("AWS operation failed: " + (code.group(1) if code else "command_failed"))
        return json.loads(result.stdout) if result.stdout.strip() else {}

    evidence["stage"] = "retrieve_session"
    payload = aws(["secretsmanager", "get-secret-value", "--secret-id", config["credential_secret"], "--endpoint-url", config["secrets_endpoint"]])
    auth = json.loads(payload["SecretString"])["cli_admin"]
    with tempfile.TemporaryDirectory(prefix="adp-cli-live-") as temporary:
        home = Path(temporary)
        env.update(HOME=str(home), AWS_CONFIG_FILE=str(home / "aws-config"), AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"))
        (home / "aws-config").write_text(
            "[profile destination]\nrole_arn = "
            + config["provisioner_arn"]
            + "\ncredential_source = Ec2InstanceMetadata\nregion = "
            + config["region"]
            + "\n"
        )
        evidence["stage"] = "install_candidate"
        source = Path(config["source_dir"])
        evidence["candidate_hashes"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir() if p.is_file()}
        installed = home / "bin"
        result = subprocess.run(
            ["sh", str(source / "install.sh"), "--prefix", str(installed), "--gateway-url", config["gateway_url"], "--no-path-edit"],
            env=env,
            capture_output=True,
            timeout=60,
        )
        require(result.returncode == 0, "Candidate installation failed")
        tokens = {
            "access_token": auth["AccessToken"],
            "id_token": auth.get("IdToken", ""),
            "refresh_token": auth.get("RefreshToken", ""),
            "expires_at": int(time.time()) + 1800,
        }
        (home / ".bedrock-gateway/tokens.json").write_text(json.dumps(tokens))

        def api(path):
            request = urllib.request.Request(config["gateway_url"] + path, headers={"Authorization": "Bearer " + auth["AccessToken"]})
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)

        def cli(args, environment=env, expected=0):
            result = subprocess.run(
                [str(installed / "adp"), "admin", "bedrock", *args, "--json"], env=environment, capture_output=True, text=True, timeout=900
            )
            try:
                outcome = json.loads(result.stdout)
            except ValueError:
                raise RuntimeError("CLI returned invalid JSON") from None
            if result.returncode != expected:
                # Only stable CLI error category; no provider messages or payloads.
                evidence["cli_failure"] = {
                    "exit_code": result.returncode,
                    "status": outcome.get("status"),
                    "error_type": type(outcome.get("error")).__name__,
                }
                raise RuntimeError("CLI exit did not match expected " + str(expected))
            return outcome

        def stack():
            return aws(["--profile", "destination", "cloudformation", "describe-stacks", "--stack-name", config["stack_name"]], missing=True)

        def unmapped():
            rows = api("/admin/bedrock-routing/mappings")
            require(
                not any(r.get("scope_id_org") == config["org"] and r.get("scope_id_team") == config["team"] for r in rows),
                "Unexpected fixture mapping before verification",
            )

        def no_destination():
            require(not any(r["label"] == config["org"] for r in api("/admin/bedrock-routing/destinations")), "Destination already exists")

        args = ["connect", "--account", config["bedrock_account"], "--org", config["org"], "--team", config["team"], "--name", config["org"], "--yes"]
        evidence["stage"] = "preflight"
        caller = aws(["--profile", "destination", "sts", "get-caller-identity"])
        require(caller["Account"] == config["bedrock_account"], "Provisioner account mismatch")
        evidence["provisioner_caller"] = caller["Arn"]
        require(stack() is None, "Destination stack already exists")
        no_destination()
        unmapped()
        wrong = args[:]
        wrong[2] = config["platform_account"]
        cli([*wrong, "--profile", "destination"], expected=5)
        cli([*args, "--profile", "destination", "--dry-run"])
        require(stack() is None, "Preflight created a stack")
        no_destination()
        unmapped()
        evidence["checks"].extend(["account_mismatch_no_mutation", "dry_run_no_mutation"])

        if config["mode"] == "direct":
            evidence["stage"] = "direct_connect"
            outcome = cli([*args, "--profile", "destination"])
            initial_stack = stack()["Stacks"][0]
            evidence["stage"] = "repeat_connect"
            repeated = cli([*args, "--profile", "destination"])
            require(
                repeated["detail"]["destination_id"] == outcome["detail"]["destination_id"]
                and stack()["Stacks"][0]["StackId"] == initial_stack["StackId"],
                "Retry did not reuse destination and stack",
            )
            evidence["checks"].extend(["direct_created_verified_assigned", "repeat_reuses_stack_and_destination"])
        else:
            # Any invocation of AWS by the ADP subprocess is recorded and denied.
            guard = home / "guard"
            guard.mkdir()
            marker = guard / "invoked"
            (guard / "aws").write_text("#!/bin/sh\n: > '" + str(marker) + "'\nexit 99\n")
            (guard / "aws").chmod(0o700)
            noaws = {
                **env,
                "PATH": str(guard) + ":" + env["PATH"],
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_CONFIG_FILE": str(home / "empty-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(home / "empty-credentials"),
            }
            download = home / "handoff"
            evidence["stage"] = "download_without_aws"
            pending = cli([*args, "--download", str(download)], noaws, expected=4)
            require(pending["status"] == "pending" and not pending["detail"]["assigned"], "Download unexpectedly assigned")
            require(stack() is None, "Download created a stack")
            unmapped()
            evidence["stage"] = "resume_before_apply"
            cli(["connect", "--resume", str(download), "--yes"], noaws, expected=5)
            unmapped()
            require(not marker.exists(), "ADP attempted AWS access during handoff")
            require(all(p.stat().st_mode & 0o077 == 0 for p in download.iterdir()), "Handoff files are not private")
            evidence["checks"].extend(["download_pending_without_aws", "premature_resume_no_mapping", "private_handoff_files"])
            evidence["stage"] = "aws_admin_apply"
            aws(
                [
                    "--profile",
                    "destination",
                    "cloudformation",
                    "create-stack",
                    "--stack-name",
                    config["stack_name"],
                    "--template-body",
                    "file://" + str(download / "template.yaml"),
                    "--parameters",
                    "file://" + str(download / "parameters.json"),
                    "--capabilities",
                    "CAPABILITY_NAMED_IAM",
                ]
            )
            aws(["--profile", "destination", "cloudformation", "wait", "stack-create-complete", "--stack-name", config["stack_name"]])
            evidence["stage"] = "resume_after_apply"
            outcome = cli(["connect", "--resume", str(download), "--yes"], noaws)
            require(not marker.exists(), "ADP attempted AWS access during resume")
            evidence["checks"].append("resume_verified_assigned_without_aws")

        detail = outcome["detail"]
        require(
            outcome["status"] == "verified" and detail["assigned"] and detail["account_id"] == config["bedrock_account"],
            "CLI did not verify and assign expected account",
        )
        final_stack = stack()["Stacks"][0]
        role = next(x["OutputValue"] for x in final_stack["Outputs"] if x["OutputKey"] == "RoleArn")
        require(role == config["role_arn"], "Stack role differs from planned destination")
        evidence.update(destination_id=detail["destination_id"], stack_id=final_stack["StackId"], role_arn=role, account_id=detail["account_id"])
        evidence["stage"] = "cli_effective_status"
        effective = cli(["status", "--user", config["member_id"]])["detail"]["effective"]
        require(effective["rung"] == "team" and effective["account_id"] == config["bedrock_account"], "CLI effective route is incorrect")
        evidence["checks"].append("cli_effective_team_route")
        evidence.update(stage="complete", success=True)


def main():
    evidence = {"success": False, "stage": "ec2_identity", "checks": []}
    try:
        execute(json.loads(Path(sys.argv[1]).read_text()), evidence)
    except Exception as exc:
        evidence["error_type"] = type(exc).__name__
        if type(exc) is RuntimeError:
            evidence["error"] = str(exc)
    print(json.dumps(evidence))
    return int(not evidence["success"])


if __name__ == "__main__":
    sys.exit(main())
