#!/usr/bin/env python3
"""E06 on the instance: configure a Bedrock destination through the ADP CLI.

Step 3 of the executable path. `adp admin bedrock connect` registers a
destination, provisions the cross-account role via CloudFormation, verifies it
and only then assigns the routing rule. The ordering is the property under test:
a destination that fails verification must never become the effective rule, or a
user's next request routes to an account that cannot serve it.

Provisioning credentials come from a scoped fixture role assumed through the
instance profile and exposed to the CLI as a named AWS profile — which is exactly
the shape a real administrator has. The ADP process is never handed those
credentials directly; `--profile` tells it which local profile to shell out with.

Three journeys, in one session so their interactions are visible:

- direct: connect, provision, verify, assign — then repeat, which must reuse the
  destination and stack rather than create a second pair.
- failure: verification of an unprovisioned destination must fail AND must leave
  the previous routing rule in place.
- download/resume: the CLI hands a template to an AWS administrator, does not
  provision, and resumes to verification after the stack exists.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import common
from common import require

ROUTING = "/admin/bedrock-routing"

# Where "which account would actually serve this person?" is read from. The admin
# router has no `/status`: its answer is `GET /admin/bedrock-routing/effective/{user_id}`,
# keyed by CANONICAL users.id — not a username and not a Cognito sub, so this journey
# has no id to put in that path. `/me/bedrock-routing/selection` answers the same
# question for the caller and takes no parameter, and the run's admin identity IS the
# `--user` the rule is assigned to, so it is the correct reading of the effective rung.
SELF_ROUTING = "/me/bedrock-routing"


def _profile(config, home, env):
    """A named AWS profile that assumes the destination provisioner role.

    `credential_source = Ec2InstanceMetadata` is what keeps the fixture's
    credentials out of this process: the AWS CLI resolves them itself from the
    instance role, so nothing here ever holds a key.
    """
    require(
        config.get("provisioner_arn"),
        "No provisioner role ARN was supplied; the destination role cannot be created",
    )
    (home / "aws-config").write_text(
        "[profile destination]\n"
        f"role_arn = {config['provisioner_arn']}\n"
        "credential_source = Ec2InstanceMetadata\n"
        f"region = {config['region']}\n"
    )
    caller = common.aws_cli(
        config, env, ["--profile", "destination", "sts", "get-caller-identity"]
    )
    require(
        caller.get("Account") == str(config["destination_account"]),
        "The provisioner profile resolves to the wrong AWS account",
    )
    require(
        ":assumed-role/" in (caller.get("Arn") or ""),
        "The provisioner is not an assumed EC2 role; CloudTrail could not attribute it",
    )
    return caller["Arn"]


def _stack(config, env, name):
    return common.aws_cli(
        config,
        env,
        [
            "--profile",
            "destination",
            "cloudformation",
            "describe-stacks",
            "--stack-name",
            name,
        ],
        missing_ok=True,
    )


def _effective(config, token):
    """The rule the product would actually apply, read from its own API.

    `GET /me/bedrock-routing/selection` returns `{effective: {rung, account_id,
    destination_id, ...}, ...}`, and the `effective` block is the resolved rung —
    the same `EffectiveMappingResponse` the admin endpoint returns. Flattened to
    that block so callers compare destinations, and `rung`/`source` are carried so
    a rule assigned at the wrong rung is visible rather than being reduced to a
    matching destination ID.
    """
    _status, payload = common.api(config, SELF_ROUTING + "/selection", token)
    effective = (payload or {}).get("effective")
    require(
        isinstance(effective, dict),
        "The gateway's selection response has no `effective` block; the effective "
        "routing rule could not be read and E06's ordering property is unassertable",
    )
    return effective


def execute(config, evidence):
    os.umask(0o077)
    # From the private on-instance vault. The payload carries only a reference,
    # since evidence leaving the instance is redacted; `load_session()` raises when
    # the session did not survive the stage boundary rather than letting a
    # placeholder be graded as a routing failure.
    session = common.load_session(config)
    token = session["access_token"]

    with tempfile.TemporaryDirectory(prefix="adp-bedrock-") as temporary:
        home = Path(temporary)
        env = common.clean_env(
            config,
            HOME=str(home),
            AWS_CONFIG_FILE=str(home / "aws-config"),
            AWS_SHARED_CREDENTIALS_FILE=str(home / "no-credentials"),
        )
        # Reuse the session install_auth established rather than logging in
        # again: E06 is about routing, and a second login would make a login
        # failure look like a routing failure.
        (home / ".bedrock-gateway").mkdir(mode=0o700)
        (home / ".bedrock-gateway" / "config.json").write_text(
            json.dumps({"gateway_url": config["gateway_url"]})
        )
        (home / ".bedrock-gateway" / "tokens.json").write_text(
            json.dumps(
                {
                    "access_token": token,
                    "id_token": session.get("id_token", ""),
                    "refresh_token": session.get("refresh_token", ""),
                    "expires_at": session.get("expires_at", 0),
                }
            )
        )
        os.chmod(home / ".bedrock-gateway" / "tokens.json", 0o600)

        evidence["provisioner_caller_arn"] = _profile(config, home, env)
        cli = common.Cli(Path(config["cli_path"]), env, evidence["transcript"])
        label = config["destination_label"]
        stack_name = "ADP-Agent-" + label
        account = str(config["destination_account"])
        base = [
            "admin",
            "bedrock",
            "connect",
            "--account",
            account,
            "--name",
            label,
            "--region",
            config["region"],
            "--user",
            config["test_user"],
            "--yes",
        ]

        evidence["stage"] = "preflight"
        require(
            _stack(config, env, stack_name) is None,
            "The destination stack already exists; E06 needs a fresh fixture",
        )
        before = _effective(config, token)
        evidence["rule_before"] = before.get("destination_id")

        # A dry run must plan without registering or provisioning anything.
        evidence["stage"] = "dry_run"
        plan = cli.json([*base, "--profile", "destination", "--dry-run"])
        require(
            (plan.get("detail") or {}).get("dry_run") is True,
            "A dry-run connect did not report itself as a dry run",
        )
        require(
            _stack(config, env, stack_name) is None,
            "A dry-run connect created a CloudFormation stack",
        )
        require(
            _effective(config, token).get("destination_id")
            == before.get("destination_id"),
            "A dry-run connect changed the effective routing rule",
        )
        evidence["checks"].append("dry_run_no_mutation")

        # Verification of a destination whose role does not exist must fail, and
        # must not assign the rule. This is the ordering property.
        evidence["stage"] = "verify_before_provision"
        _status, registered = common.api(
            config,
            ROUTING + "/destinations",
            token,
            method="POST",
            body={
                "source": "new_account",
                "account_id": account,
                "label": config["unprovisioned_label"],
                "link_to_org_id": config["org_id"],
                "region": config["region"],
            },
            expect=(200, 201),
        )
        unverified = ((registered or {}).get("destination") or {}).get("id")
        require(unverified, "The unprovisioned destination fixture was not registered")
        evidence["resources"] = [["bedrock_destination", unverified]]
        code, payload = cli.run(
            ["admin", "bedrock", "verify", unverified], expected=None
        )
        require(
            code != 0 and (payload or {}).get("status") != "verified",
            "Verifying a destination with no role in AWS reported success",
        )
        require(
            _effective(config, token).get("destination_id")
            == before.get("destination_id"),
            "A failed verification changed the effective routing rule",
        )
        evidence["checks"].append("failed_verification_prevents_assignment")

        # The real path: provision, verify, assign.
        evidence["stage"] = "connect"
        outcome = cli.json([*base, "--profile", "destination"], timeout=1800)
        detail = outcome.get("detail") or {}
        require(
            outcome.get("status") in ("verified", "configured"),
            f"adp admin bedrock connect reported {outcome.get('status')!r}",
        )
        destination_id = detail.get("destination_id") or detail.get(
            "destination", {}
        ).get("id")
        require(destination_id, "connect did not report the destination it assigned")
        created = _stack(config, env, stack_name)
        require(
            created is not None, "connect did not create the destination role stack"
        )
        evidence["stack_id"] = created["Stacks"][0]["StackId"]
        evidence["destination_id"] = destination_id
        evidence["resources"].append(["cloudformation_stack", evidence["stack_id"]])

        after = _effective(config, token)
        require(
            after.get("destination_id") == destination_id,
            "The effective routing rule is not the destination that was just verified",
        )
        evidence["rule_after"] = after.get("destination_id")
        evidence["rule_source"] = after.get("source")
        # The rung matters as much as the destination: a `platform` answer means no
        # rule matched and the run would be routing on ambient IRSA while the
        # destination ID happened to agree.
        evidence["rule_rung"] = after.get("rung")
        require(
            after.get("rung") in ("user", "team", "org"),
            f"The effective rule resolved at rung {after.get('rung')!r}; the "
            "destination that was just verified is not serving this identity",
        )
        evidence["checks"].append("verified_destination_becomes_effective_rule")

        # Idempotency: a repeat must reuse both the destination and the stack.
        evidence["stage"] = "repeat"
        repeated = cli.json([*base, "--profile", "destination"], timeout=1800)
        repeat_detail = repeated.get("detail") or {}
        require(
            (
                repeat_detail.get("destination_id")
                or repeat_detail.get("destination", {}).get("id")
            )
            == destination_id,
            "A repeated connect created a second destination instead of reusing the first",
        )
        require(
            _stack(config, env, stack_name)["Stacks"][0]["StackId"]
            == evidence["stack_id"],
            "A repeated connect replaced the CloudFormation stack",
        )
        _status, listing = common.api(
            config, ROUTING + "/destinations?page=1&page_size=100", token
        )
        matching = [
            row
            for row in ((listing or {}).get("destinations") or [])
            if row.get("label") == label
        ]
        require(
            len(matching) == 1,
            f"A repeated connect left {len(matching)} destinations named for this run; expected one",
        )
        evidence["checks"].append("repeat_connect_is_idempotent")

        # Download/resume: the CLI must not provision, and must resume to
        # verification once the administrator has applied the template.
        evidence["stage"] = "download"
        handoff_label = config["handoff_label"]
        handoff_stack = "ADP-Agent-" + handoff_label
        download = home / "handoff"
        pending = cli.json(
            [
                "admin",
                "bedrock",
                "connect",
                "--account",
                account,
                "--name",
                handoff_label,
                "--region",
                config["region"],
                "--user",
                config["test_user"],
                "--download",
                str(download),
                "--yes",
            ],
            expected=None,
        )
        require(
            pending.get("status") in ("pending", "configured"),
            f"A download-only connect reported {pending.get('status')!r}",
        )
        require(
            _stack(config, env, handoff_stack) is None,
            "A download-only connect provisioned the stack itself",
        )
        require(
            all(item.stat().st_mode & 0o077 == 0 for item in download.iterdir()),
            "The handoff package is not private to its owner",
        )
        evidence["checks"].append("download_hands_off_without_provisioning")

        evidence["stage"] = "administrator_apply"
        common.aws_cli(
            config,
            env,
            [
                "--profile",
                "destination",
                "cloudformation",
                "create-stack",
                "--stack-name",
                handoff_stack,
                "--template-body",
                "file://" + str(download / "template.yaml"),
                "--parameters",
                "file://" + str(download / "parameters.json"),
                "--capabilities",
                "CAPABILITY_NAMED_IAM",
            ],
        )
        common.aws_cli(
            config,
            env,
            [
                "--profile",
                "destination",
                "cloudformation",
                "wait",
                "stack-create-complete",
                "--stack-name",
                handoff_stack,
            ],
            timeout=1800,
        )
        applied = _stack(config, env, handoff_stack)
        require(applied is not None, "The administrator apply did not create the stack")
        evidence["resources"].append(
            ["cloudformation_stack", applied["Stacks"][0]["StackId"]]
        )

        evidence["stage"] = "resume"
        resumed = cli.json(
            ["admin", "bedrock", "connect", "--resume", str(download), "--yes"],
            timeout=1800,
        )
        require(
            resumed.get("status") in ("verified", "configured"),
            f"Resume after the administrator applied the template reported {resumed.get('status')!r}",
        )
        evidence["checks"].append("resume_verifies_after_administrator_apply")

        # Leave the run's effective rule pointing at the provisioned destination
        # so the inference step routes through the account this step proved.
        cli.json([*base, "--profile", "destination"], timeout=1800)
        evidence["effective_destination_id"] = _effective(config, token).get(
            "destination_id"
        )
        evidence["correlation"] = {
            "bedrock_destination_id": destination_id,
            "bedrock_destination_account": account,
            "bedrock_stack_id": evidence["stack_id"],
        }
        evidence.update(stage="complete", success=True)


if __name__ == "__main__":
    import sys

    sys.exit(common.run_script(execute))
