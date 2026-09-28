#!/usr/bin/env python3
"""EC2-only acceptance worker for `adp aws connect` (E04/E05).

This is the adapter the handoff asked for. The merged `test-cli-routing.py
--provision direct|handoff` exercises `adp admin bedrock connect`, which is a
different command with a different endpoint set; nothing in the tree drove
`adp aws connect` before this. Renaming a state directory would not have tested
it, so this worker executes the real command and records a transcript.

It runs ONLY on the disposable EC2 instance: `execute()` refuses to proceed
unless the instance identity document matches the instance the harness created.
Stdlib only, because the instance has no third-party Python.

E04 covers provision, import, list/verify parity, mismatch failure and
disconnect-preserves-the-role. E05 covers the download to separate-AWS-admin
apply to resume path with AWS access disabled inside the ADP process, which is
proven by a PATH shim that records any `aws` invocation and exits nonzero.
"""

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

# Flag values never reach the transcript; the flag names do.
SENSITIVE_VALUE = "<redacted>"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def instance_identity():
    """IMDSv2 identity. Refuses a proxy so this cannot be spoofed via env."""
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


def sanitize(argv):
    """Transcript form of a command: keep the shape, drop the values."""
    parts = []
    for item in argv:
        text = str(item)
        if text.startswith("-"):
            parts.append(text.split("=", 1)[0] if "=" in text else text)
        elif parts and parts[-1].startswith("-"):
            parts.append(SENSITIVE_VALUE)
        else:
            parts.append(
                text if all(not p.startswith("-") for p in parts) else SENSITIVE_VALUE
            )
    return " ".join(parts)


def execute(config, evidence):
    identity = instance_identity()
    require(
        identity["instanceId"] == config["instance_id"]
        and identity["accountId"] == config["platform_account"],
        "Worker must run on the owned EC2 instance",
    )
    evidence["instance_id"] = identity["instanceId"]
    os.umask(0o077)

    # Strip every inherited provider/AWS variable: the CLI under test must not
    # be able to borrow the runner's identity.
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("AWS_", "ADP_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "BG_"))
    }
    env.update(
        AWS_DEFAULT_REGION=config["region"],
        AWS_PAGER="",
        AWS_CLI_AUTO_PROMPT="off",
        # The approved private subnet has required regional FIPS endpoints.
        AWS_ENDPOINT_URL_STS=config["sts_endpoint"],
    )

    def aws(args, environment=env, missing=False):
        result = subprocess.run(
            ["aws", "--region", config["region"], "--output", "json", *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=660,
        )
        if result.returncode:
            if (
                missing
                and "(ValidationError)" in result.stderr
                and "does not exist" in result.stderr
            ):
                return None
            code = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            raise RuntimeError(
                "AWS operation failed: " + (code.group(1) if code else "command_failed")
            )
        return json.loads(result.stdout) if result.stdout.strip() else {}

    evidence["stage"] = "retrieve_session"
    payload = aws(
        [
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            config["credential_secret"],
            "--endpoint-url",
            config["secrets_endpoint"],
        ]
    )
    auth = json.loads(payload["SecretString"])[config["session_key"]]

    with tempfile.TemporaryDirectory(prefix="adp-personal-aws-") as temporary:
        home = Path(temporary)
        env.update(
            HOME=str(home),
            AWS_CONFIG_FILE=str(home / "aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
        )
        # A named profile that assumes the destination provisioner role via the
        # instance role. This is what a user's own AWS admin credentials stand in
        # for; the ADP process itself never gets them (see `noaws` below).
        (home / "aws-config").write_text(
            "[profile destination]\nrole_arn = "
            + config["provisioner_arn"]
            + "\ncredential_source = Ec2InstanceMetadata\nregion = "
            + config["region"]
            + "\n"
        )

        evidence["stage"] = "install_candidate"
        source = Path(config["source_dir"])
        evidence["candidate_hashes"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in source.iterdir()
            if p.is_file()
        }
        installed = home / "bin"
        result = subprocess.run(
            [
                "sh",
                str(source / "install.sh"),
                "--prefix",
                str(installed),
                "--gateway-url",
                config["gateway_url"],
                "--no-path-edit",
            ],
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
            request = urllib.request.Request(
                config["gateway_url"] + path,
                headers={"Authorization": "Bearer " + auth["AccessToken"]},
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)

        def cli(args, environment=env, expected=0):
            """Run `adp aws ...` and return its JSON envelope.

            Exit code 4 is 'pending', not a failure, and 0 can accompany a
            non-verified status, so the caller asserts on `status` — never on the
            exit code alone.
            """
            argv = [str(installed / "adp"), "aws", *args, "--json"]
            evidence["transcript"].append(sanitize(argv))
            result = subprocess.run(
                argv, env=environment, capture_output=True, text=True, timeout=900
            )
            try:
                outcome = json.loads(result.stdout)
            except ValueError:
                raise RuntimeError("CLI returned invalid JSON") from None
            if result.returncode != expected:
                # Stable categories only: never echo provider text or payloads.
                evidence["cli_failure"] = {
                    "command": sanitize(argv),
                    "exit_code": result.returncode,
                    "status": outcome.get("status"),
                    "error_code": (outcome.get("error") or {}).get("code"),
                }
                raise RuntimeError(
                    f"CLI exit {result.returncode} did not match expected {expected}"
                )
            return outcome

        def connections():
            return api("/auth/credentials?scope=user").get("credentials", [])

        def role_exists():
            return (
                aws(
                    [
                        "--profile",
                        "destination",
                        "iam",
                        "get-role",
                        "--role-name",
                        config["role_name"],
                    ],
                    missing=True,
                )
                is not None
            )

        def stack():
            return aws(
                [
                    "--profile",
                    "destination",
                    "cloudformation",
                    "describe-stacks",
                    "--stack-name",
                    config["stack_name"],
                ],
                missing=True,
            )

        account = config["destination_account"]
        name = config["connection_name"]
        base = [
            "connect",
            "--account",
            account,
            "--name",
            name,
            "--region",
            config["region"],
            "--yes",
        ]

        evidence["stage"] = "preflight"
        caller = aws(["--profile", "destination", "sts", "get-caller-identity"])
        require(caller["Account"] == account, "Provisioner account mismatch")
        # CloudTrail attribution for E05: this is the identity AWS will record as
        # the creator of the role, and it must be the EC2 provisioner, not a laptop.
        evidence["provisioner_caller_arn"] = caller["Arn"]
        require(
            ":assumed-role/" in caller["Arn"],
            "Provisioner is not an assumed EC2 role; CloudTrail could not attribute the provisioner",
        )
        require(
            stack() is None, "Destination stack already exists; use a fresh fixture"
        )
        require(
            not any(row.get("label") == name for row in connections()),
            "Connection already exists; use a fresh fixture",
        )

        # Negative: an account that is not the caller's must be refused before
        # anything is created.
        wrong = base[:]
        wrong[2] = config["platform_account"]
        cli([*wrong, "--profile", "destination"], expected=5)
        require(stack() is None, "Account mismatch created a stack")
        require(
            not any(row.get("label") == name for row in connections()),
            "Account mismatch created a connection",
        )
        evidence["checks"].append("account_mismatch_no_mutation")

        # Negative: a mutation without --yes must refuse non-interactively.
        cli(
            [
                "connect",
                "--account",
                account,
                "--name",
                name,
                "--profile",
                "destination",
            ],
            expected=1,
        )
        require(stack() is None, "Unconfirmed connect created a stack")
        evidence["checks"].append("unconfirmed_connect_refused")

        # Dry run must plan without mutating.
        plan = cli([*base, "--profile", "destination", "--dry-run"])
        require(
            plan["detail"]["dry_run"] is True
            and plan["detail"]["action"] == "create_role",
            "Dry run did not report a create_role plan",
        )
        require(stack() is None, "Dry run created a stack")
        evidence["checks"].append("dry_run_no_mutation")

        if config["mode"] == "provision":
            evidence["stage"] = "provision"
            outcome = cli([*base, "--profile", "destination"])
            detail = outcome["detail"]
            require(
                outcome["status"] == "verified"
                and detail["verified"]
                and detail["account_id"] == account,
                "adp aws connect did not verify the provisioned connection",
            )
            created = stack()
            require(
                created is not None,
                "Provisioning did not create the CloudFormation stack",
            )
            evidence["stack_id"] = created["Stacks"][0]["StackId"]
            evidence["connection_id"] = detail["connection_id"]
            evidence["checks"].append("provision_created_and_verified")

            # Idempotency: a repeat must reuse, not duplicate.
            repeated = cli([*base, "--profile", "destination"])
            require(
                repeated["detail"]["connection_id"] == detail["connection_id"]
                and stack()["Stacks"][0]["StackId"] == evidence["stack_id"],
                "Repeat connect did not reuse the existing connection and stack",
            )
            matching = [row for row in connections() if row.get("label") == name]
            require(
                len(matching) == 1,
                f"Repeat connect produced {len(matching)} connections; expected exactly one",
            )
            evidence["checks"].append("repeat_connect_reuses_connection")

        else:
            evidence["stage"] = "download_without_aws"
            # Prove the ADP process never touches AWS during handoff/resume: any
            # `aws` invocation from it hits this shim, which records and fails.
            guard = home / "guard"
            guard.mkdir()
            marker = guard / "invoked"
            (guard / "aws").write_text(
                "#!/bin/sh\n: > '" + str(marker) + "'\nexit 99\n"
            )
            (guard / "aws").chmod(0o700)
            noaws = {
                **env,
                "PATH": str(guard) + ":" + env["PATH"],
                "AWS_EC2_METADATA_DISABLED": "true",
                "AWS_CONFIG_FILE": str(home / "empty-config"),
                "AWS_SHARED_CREDENTIALS_FILE": str(home / "empty-credentials"),
            }
            download = home / "handoff"
            pending = cli(
                [
                    "connect",
                    "--account",
                    account,
                    "--name",
                    name,
                    "--region",
                    config["region"],
                    "--download",
                    str(download),
                    "--yes",
                ],
                noaws,
                expected=4,
            )
            require(
                pending["status"] == "pending"
                and pending["detail"]["verified"] is False,
                "Download unexpectedly verified",
            )
            require(stack() is None, "Download created a stack")
            require(not marker.exists(), "ADP attempted AWS access during download")
            require(
                all(p.stat().st_mode & 0o077 == 0 for p in download.iterdir()),
                "Handoff files are not private",
            )
            evidence["checks"].extend(
                ["download_pending_without_aws", "private_handoff_files"]
            )

            # Stale verification: resuming before the admin applies must fail and
            # must not leave a half-connected record.
            evidence["stage"] = "resume_before_apply"
            cli(["connect", "--resume", str(download), "--yes"], noaws, expected=5)
            require(
                not marker.exists(), "ADP attempted AWS access during premature resume"
            )
            evidence["checks"].append("stale_resume_fails")

            evidence["stage"] = "aws_admin_apply"
            # The separate AWS-administrator step, with credentials the ADP
            # process never sees.
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
            aws(
                [
                    "--profile",
                    "destination",
                    "cloudformation",
                    "wait",
                    "stack-create-complete",
                    "--stack-name",
                    config["stack_name"],
                ]
            )

            evidence["stage"] = "resume_after_apply"
            outcome = cli(["connect", "--resume", str(download), "--yes"], noaws)
            detail = outcome["detail"]
            require(
                outcome["status"] == "verified"
                and detail["verified"]
                and detail["account_id"] == account,
                "Resume did not verify the connection after the administrator applied the template",
            )
            require(not marker.exists(), "ADP attempted AWS access during resume")
            evidence["connection_id"] = detail["connection_id"]
            evidence["stack_id"] = stack()["Stacks"][0]["StackId"]
            evidence["checks"].append("resume_verified_without_aws_access")

        # Shared tail: list/verify parity against live API records, then
        # disconnect and prove the AWS role survives.
        evidence["stage"] = "list_and_verify"
        listing = cli(["list"])["detail"]["connections"]
        row = next(
            (item for item in listing if item["id"] == evidence["connection_id"]), None
        )
        require(
            row is not None, "CLI list did not return the connection it just created"
        )
        live = next(
            (item for item in connections() if item["id"] == evidence["connection_id"]),
            None,
        )
        require(
            live is not None,
            "The live API does not record the connection the CLI created",
        )
        require(
            row["account_id"] == str(live.get("account_id")) == account
            and row["name"] == live.get("label") == name,
            "CLI list disagrees with the live API record",
        )
        fresh = cli(["verify", evidence["connection_id"]])
        require(
            fresh["detail"]["verified"] is True
            and fresh["detail"]["account_id"] == account,
            "Fresh verify did not confirm the connection",
        )
        evidence["routing_capable"] = fresh["detail"].get("routing_capable")
        evidence["checks"].extend(
            ["cli_list_matches_live_api", "fresh_verify_succeeds"]
        )

        # Negative: verifying someone else's / a non-existent connection must fail.
        cli(["verify", config["absent_connection_id"]], expected=5)
        evidence["checks"].append("unknown_connection_verify_fails")

        evidence["stage"] = "disconnect"
        require(role_exists(), "The destination IAM role is missing before disconnect")
        removal = cli(["disconnect", evidence["connection_id"], "--yes"])
        require(
            removal["detail"]["disconnected"] is True,
            "Disconnect did not report success",
        )
        require(
            not any(item["id"] == evidence["connection_id"] for item in connections()),
            "The connection is still present in the live API after disconnect",
        )
        # The contract: ADP forgets the connection, AWS keeps the role.
        require(
            role_exists(),
            "Disconnect deleted the IAM role; it must be preserved in the user's AWS account",
        )
        evidence["checks"].append("disconnect_removes_adp_record_preserves_aws_role")

        evidence.update(stage="complete", success=True)


def main():
    evidence = {
        "success": False,
        "stage": "ec2_identity",
        "checks": [],
        "transcript": [],
    }
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
