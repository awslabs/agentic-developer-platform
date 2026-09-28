"""Extend #5173 fixtures with actual EC2 CLI provisioning, without local CLI calls."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shlex
import tarfile
from datetime import datetime
from pathlib import Path


def provisioner_template(config, prefix):
    role = f"arn:aws:iam::{config['bedrock_account']}:role/ADP-Agent-{prefix}"
    stack = f"arn:aws:cloudformation:{config['region']}:{config['bedrock_account']}:stack/ADP-Agent-{prefix}/*"
    return {
        "Resources": {
            "Provisioner": {
                "Type": "AWS::IAM::Role",
                "Properties": {
                    "RoleName": prefix + "-cli-provisioner",
                    "AssumeRolePolicyDocument": {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "sts:AssumeRole",
                                "Principal": {"AWS": f"arn:aws:iam::{config['platform_account']}:root"},
                                "Condition": {
                                    "ArnLike": {"aws:PrincipalArn": f"arn:aws:iam::{config['platform_account']}:role/{prefix}-runner-Role-*"}
                                },
                            }
                        ],
                    },
                    "Policies": [
                        {
                            "PolicyName": "OnlyFixtureRoleAndStack",
                            "PolicyDocument": {
                                "Version": "2012-10-17",
                                "Statement": [
                                    {
                                        "Effect": "Allow",
                                        "Action": [
                                            "cloudformation:CreateStack",
                                            "cloudformation:DescribeStacks",
                                            "cloudformation:DescribeStackEvents",
                                            "cloudformation:GetTemplate",
                                            "cloudformation:DeleteStack",
                                        ],
                                        "Resource": stack,
                                    },
                                    {
                                        "Effect": "Allow",
                                        "Action": [
                                            "iam:CreateRole",
                                            "iam:GetRole",
                                            "iam:DeleteRole",
                                            "iam:PutRolePolicy",
                                            "iam:GetRolePolicy",
                                            "iam:DeleteRolePolicy",
                                            "iam:ListRolePolicies",
                                            "iam:TagRole",
                                            "iam:UntagRole",
                                            "iam:ListAttachedRolePolicies",
                                        ],
                                        "Resource": role,
                                    },
                                ],
                            },
                        }
                    ],
                },
            }
        }
    }


def validate_cli_stack(stack, resources, prefix, config, created_at):
    """CLI stacks are untagged: accept only the recorded, initially absent stack."""

    def require(condition, message):
        if not condition:
            raise ValueError(message)

    name = "ADP-Agent-" + prefix
    expected = f"arn:aws:cloudformation:{config['region']}:{config['bedrock_account']}:stack/{name}/"
    require(resources.get("cli_stack_absent_before") is True, "No pre-creation ownership evidence")
    require(stack["StackName"] == name and stack["StackId"].startswith(expected), "CLI stack identity mismatch")
    require(stack["CreationTime"] >= datetime.fromisoformat(created_at), "CLI stack predates fixture")
    require(resources.get("cli_stack_id", stack["StackId"]) == stack["StackId"], "CLI stack was replaced")
    expected_role = f"arn:aws:iam::{config['bedrock_account']}:role/{name}"
    require(all(o["OutputKey"] != "RoleArn" or o["OutputValue"] == expected_role for o in stack.get("Outputs", [])), "CLI stack role mismatch")


def install(harness, cli_dir, mode):
    from tests.e2e.tenant_validation import runner as runner_module
    from tests.e2e.tenant_validation.aws import Aws
    from tests.e2e.tenant_validation.common import require
    from tests.e2e.tenant_validation.fixtures import Fixtures
    from tests.e2e.tenant_validation.runner import Runner

    original_template = runner_module.runner_template

    def runner_template(config, secret, ami):
        template = original_template(config, secret, ami)
        statements = template["Resources"]["Role"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        statements[0]["Action"] = ["bedrock:*"]
        # Derive the same namespace from the owned Secrets Manager ARN.
        prefix = secret.split(":secret:", 1)[1].split("/", 1)[0]
        statements.append(
            {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": f"arn:aws:iam::{config['bedrock_account']}:role/{prefix}-cli-provisioner"}
        )
        return template

    class ProvisioningAws(Aws):
        def owned_stack(self, name, account="platform"):
            r, prefix = self.state.data["resources"], self.state.data["prefix"]
            planned = "ADP-Agent-" + prefix
            arn_prefix = f"arn:aws:cloudformation:{self.config['region']}:{self.config['bedrock_account']}:stack/{planned}/"
            if account != "bedrock" or not (name == planned or name.startswith(arn_prefix)):
                return super().owned_stack(name, account)
            cfn = self.client("cloudformation", account)
            try:
                stack = cfn.describe_stacks(StackName=name)["Stacks"][0]
            except cfn.exceptions.ClientError as exc:
                if exc.response["Error"]["Code"] == "ValidationError" and "does not exist" in exc.response["Error"]["Message"]:
                    return None
                raise
            validate_cli_stack(stack, r, prefix, self.config, self.state.data["created_at"])
            items = cfn.list_stack_resources(StackName=stack["StackId"])["StackResourceSummaries"]
            require(
                all(
                    x["ResourceType"] == "AWS::IAM::Role"
                    and x["LogicalResourceId"] == "AdpRoutingRole"
                    and x.get("PhysicalResourceId", planned) == planned
                    for x in items
                ),
                "Unexpected resource in CLI stack",
            )
            r["cli_stack_id"] = stack["StackId"]
            self.state.save()
            return stack

        def cleanup_logging(self):
            super().cleanup_logging()
            name = self.state.data["resources"].get("cli_provisioner_stack")
            if name:
                self.delete_stack(name, "bedrock")

    class ProvisioningRunner(Runner):
        def setup(self):
            r = self.s.data["resources"]
            if r.get("cli_runner_ready"):
                return
            super().setup()
            # Existing inference worker still consumes member refresh tokens.
            secret = {u: self.s.data["users"][u]["tokens"]["RefreshToken"] for u in ("member1", "member2")}
            secret["cli_admin"] = self.s.data["users"]["admin"]["tokens"]
            self.aws.client("secretsmanager").put_secret_value(SecretId=r["runner_secret_arn"], SecretString=json.dumps(secret))
            bundle = io.BytesIO()
            with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
                for path in cli_dir.iterdir():
                    if path.is_file():
                        archive.add(path, arcname="candidate/" + path.name)
                for filename in ("cli_provisioning_worker.py", "cli_provisioning.py"):
                    archive.add(Path(__file__).with_name(filename), arcname=filename)
                archive.add(cli_dir.parent / "tests/cli/test_cli_provisioning_guards.py", arcname="test_cli_provisioning_guards.py")
            payload = bundle.getvalue()
            self.aws.client("s3").put_object(Bucket=r["runner_outputs"]["Bucket"], Key="bundle.tgz", Body=payload, ServerSideEncryption="AES256")
            result = self.command(
                [
                    "set -eu",
                    f"aws s3 cp s3://{r['runner_outputs']['Bucket']}/bundle.tgz /tmp/adp-cli.tgz",
                    f"echo '{hashlib.sha256(payload).hexdigest()}  /tmp/adp-cli.tgz' | sha256sum -c -",
                    "tar xzf /tmp/adp-cli.tgz -C /home/ec2-user/adp-validation",
                    "chown -R ec2-user:ec2-user /home/ec2-user/adp-validation",
                    'runuser -l ec2-user -c "python3 /home/ec2-user/adp-validation/test_cli_provisioning_guards.py"',
                ],
                "candidate-install",
                120,
            )
            require(result["Status"] == "Success", "Candidate transfer failed")
            r["cli_runner_ready"] = True
            self.s.save()

        def provision(self):
            r = self.s.data["resources"]
            config = {k: self.c[k] for k in ("platform_account", "bedrock_account", "gateway_url", "region")}
            config.update(
                mode=mode,
                instance_id=r["runner_outputs"]["Instance"],
                credential_secret=r["runner_secret_arn"],
                secrets_endpoint=self.c.get("runner_secrets_endpoint", f"https://secretsmanager-fips.{self.c['region']}.amazonaws.com"),
                source_dir="/home/ec2-user/adp-validation/candidate",
                org=r["org"],
                team=r["team"],
                provisioner_arn=r["cli_provisioner_arn"],
                stack_name=r["destination_stack"],
                role_arn=f"arn:aws:iam::{self.c['bedrock_account']}:role/{r['destination_role']}",
                member_id=self.s.data["users"]["member1"]["adp_id"],
            )
            encoded = base64.b64encode(json.dumps(config).encode()).decode()
            result = self.command(
                [
                    "set -eu",
                    f"printf %s {shlex.quote(encoded)} | base64 -d > /home/ec2-user/adp-validation/provision.json",
                    "chown ec2-user:ec2-user /home/ec2-user/adp-validation/provision.json",
                    'runuser -l ec2-user -c "python3 /home/ec2-user/adp-validation/cli_provisioning_worker.py '
                    '/home/ec2-user/adp-validation/provision.json"',
                ],
                "cli-provision-" + mode,
                2400,
            )
            try:
                outcome = json.loads(result["StandardOutputContent"])
            except ValueError:
                outcome = {"success": False, "command_status": result["Status"]}
            key = "cli_provision:" + mode
            self.s.check(key, result["Status"] == "Success" and outcome.get("success"), outcome)
            require(outcome.get("success") and result["Status"] == "Success", "EC2 CLI provisioning failed; see report check " + key)
            return outcome

    class ProvisioningFixtures(Fixtures):
        def destination(self):
            r, prefix = self.s.data["resources"], self.s.data["prefix"]
            require(not r.get("cli_provision_mode"), "Use a fresh fixture for provisioning")
            r.update(
                cli_provision_mode=mode,
                destination_stack="ADP-Agent-" + prefix,
                destination_role="ADP-Agent-" + prefix,
                cli_provisioner_stack=prefix + "-cli-provisioner",
                cli_provisioner_arn=f"arn:aws:iam::{self.c['bedrock_account']}:role/{prefix}-cli-provisioner",
                mapping_scope=f"team:{prefix}:{r['team']}",
            )
            self.s.save()
            # An existing untagged stack fails the ownership check; never adopt it.
            require(self.aws.owned_stack(r["destination_stack"], "bedrock") is None, "Destination stack already exists")
            r["cli_stack_absent_before"] = True
            self.s.save()
            self.aws.create_stack(r["cli_provisioner_stack"], account="bedrock", TemplateBody=json.dumps(provisioner_template(self.c, prefix)))
            runner = ProvisioningRunner(self.c, self.s, self.aws)
            runner.setup()
            outcome = runner.provision()
            r.update(destination_id=outcome["destination_id"], cli_stack_id=outcome["stack_id"])
            self.s.save()
            self.aws.owned_stack(outcome["stack_id"], "bedrock")
            org = self.api("GET", f"/admin/organizations/{prefix}")
            self.api("PUT", f"/admin/organizations/{prefix}", {"settings": {**org["settings"], "bedrock_routing_enforce": True}})
            for who in ("member1", "member2"):
                resolved = self.api("GET", "/admin/bedrock-routing/effective/" + self.s.data["users"][who]["adp_id"])
                require(resolved["rung"] == "team" and resolved["account_id"] == self.c["bedrock_account"], "Team destination was not selected")

    runner_module.runner_template = runner_template
    original_start = harness.start_matrix

    def start_matrix(state, suites):
        require(state.data["resources"].get("cli_provision_mode", mode) == mode, "Provisioning mode differs from retained fixture")
        original_start(state, suites)
        state.data["matrix"].append("cli_provision:" + mode)
        state.save()

    harness.start_matrix = start_matrix
    harness.Aws = ProvisioningAws
    harness.Runner = ProvisioningRunner
    harness.Fixtures = ProvisioningFixtures
