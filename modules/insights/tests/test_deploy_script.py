"""Offline tests for the human/CI Insights deployment entrypoint."""

import base64
import fnmatch
import json
import os
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "deploy.sh"
IMAGE = "registry.example/insights@sha256:" + "a" * 64
DISTILLER_IMAGE = "registry.example/distiller@sha256:" + "b" * 64
PUBLIC_PEM = "-----BEGIN PUBLIC KEY-----\n" + base64.b64encode(
    bytes.fromhex("302a300506032b6570032100") + b"x" * 32).decode() + "\n-----END PUBLIC KEY-----\n"
AUTHORITY = "http://bedrockgateway.adp-gateway.svc.cluster.local:80"
STUB = r'''
import json
import os
import pathlib
import shlex
import sys

command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if command == "aws" and args[:2] == ["ssm", "get-parameter"] and "/insights/" in args[args.index("--name") + 1]:
    name = args[args.index("--name") + 1]
    with pathlib.Path(os.environ["TEST_RECEIVER_READS"]).open("a") as log:
        log.write(name + "\n")
    if os.environ.get("TEST_RECEIVER_DENIED"):
        sys.exit(45)
    value = os.environ["TEST_RECEIVER_KEYS"] if name.endswith("verification-keys") else os.environ["TEST_RECEIVER_URL"]
    print(os.environ.get("TEST_RECEIVER_RESPONSE", json.dumps({"Parameter": {
        "Name": name, "Type": "String", "Version": 1, "Value": value,
        "ARN": "arn:aws:ssm:us-east-1:000000000000:parameter" + name
    }})))
    sys.exit(0)
if command == "aws" and args[0] in ("ssm", "sts"):
    print("000000000000" if args[0] == "ssm" else os.environ.get("TEST_ACTIVE_ACCOUNT", "000000000000"))
    sys.exit(0)
with pathlib.Path(os.environ["TEST_CALLS"]).open("a") as log:
    log.write(shlex.join([command, *args]) + "\n")
if command == "terraform":
    backend = pathlib.Path(os.environ["TEST_BACKEND"])
    action = args[1]
    if action == "init":
        if os.environ.get("TEST_INIT_FAIL"):
            sys.exit(41)
        backend.write_text(next(value.removeprefix("-backend-config=key=") for value in args if value.startswith("-backend-config=key=")))
    elif action == "plan":
        for arg in args:
            if arg.startswith("-var-file="):
                source = pathlib.Path(arg.removeprefix("-var-file="))
                assert source.stat().st_mode & 0o077 == 0
                pathlib.Path(os.environ["TEST_VARIABLES"]).write_text(source.read_text())
    elif action == "output":
        if not backend.exists():
            sys.exit("Backend initialization required")
        if os.environ.get("TEST_OUTPUT_FAIL"):
            print("State read failed", file=sys.stderr)
            sys.exit(42)
        outputs = {name: {"value": os.environ[variable]} for name, variable in (
            ("emitter_image", "TEST_EXISTING_IMAGE"), ("distiller_image", "TEST_EXISTING_DISTILLER_IMAGE")
        ) if variable in os.environ}
        if "-json" in args:
            print(os.environ.get("TEST_OUTPUT_JSON", json.dumps(outputs)))
        else:
            print(outputs[args[-1]]["value"] if args[-1] in outputs else "Warning: No outputs found")
elif command == "aws" and args[:2] == ["eks", "update-kubeconfig"]:
    if os.environ.get("TEST_KUBECONFIG_FAIL"):
        sys.exit(43)
    pathlib.Path(args[args.index("--kubeconfig") + 1]).write_text(json.dumps({
        "cluster": args[args.index("--name") + 1], "context": args[args.index("--alias") + 1]
    }))
elif command == "kubectl":
    config = json.loads(pathlib.Path(args[args.index("--kubeconfig") + 1]).read_text())
    assert args[args.index("--context") + 1] == config["context"]
    if os.environ.get("TEST_KUBECTL_FAIL"):
        sys.exit(44)
'''


class DeployScriptTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        bin_dir = pathlib.Path(self.directory.name)
        self.log = bin_dir / "calls"
        self.backend = bin_dir / "backend"
        for command in ("aws", "terraform", "kubectl"):
            executable = bin_dir / command
            executable.write_text(f"#!{sys.executable}\n" + STUB)
            executable.chmod(0o755)
        self.env = os.environ | {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "TEST_CALLS": str(self.log),
            "TEST_BACKEND": str(self.backend),
            "TEST_RECEIVER_READS": str(bin_dir / "receiver-reads"),
            "TEST_VARIABLES": str(bin_dir / "variables"),
            "TEST_RECEIVER_KEYS": json.dumps({"test-key": PUBLIC_PEM}),
            "TEST_RECEIVER_URL": AUTHORITY,
        }

    def invoke(self, *args, env=None):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env or self.env)

    def test_plan_shows_changes_without_applying(self):
        result = self.invoke("--infra", "--plan", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text()
        self.assertIn(" init ", calls)
        self.assertIn(" validate", calls)
        self.assertIn(" plan ", calls)
        self.assertNotIn(" apply ", calls)
        self.assertIn("adp-terraform-state-000000000000", calls)
        plan = next(shlex.split(call) for call in calls.splitlines() if " plan " in call)
        self.assertIn("-var=emitter_image=", plan)
        self.assertIn("-var=distiller_image=", plan)
        self.assertFalse(pathlib.Path(self.env["TEST_RECEIVER_READS"]).exists())
        self.assertIn("output -json", calls)
        self.assertNotIn("output -raw", calls)

    def test_apply_uses_saved_plan(self):
        result = self.invoke("--infra", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(" apply -input=false ", self.log.read_text())

    def test_bootstrap_emits_policy_without_aws_calls(self):
        result = self.invoke("--bootstrap-ci", "--account", "000000000000", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        policy = json.loads(result.stdout)
        identity_read = next(statement for statement in policy["Statement"]
                             if "iam:GetRole" in statement["Action"])
        self.assertEqual(set(identity_read["Resource"]), {
            "arn:aws:iam::000000000000:role/adp-dev-insights-emitter",
            "arn:aws:iam::000000000000:role/adp-dev-insights-distiller",
        })
        self.assertTrue(all(action.startswith(("iam:Get", "iam:List"))
                            for action in identity_read["Action"]))
        self.assertIn("arn:aws:s3:::adp-terraform-state-000000000000/dev/platform/terraform.tfstate", str(policy))
        self.assertIn("ecr:DescribeImages", str(policy))
        ssm_read = next(statement for statement in policy["Statement"] if statement["Action"] == "ssm:GetParameter")
        self.assertEqual(set(ssm_read["Resource"]), {
            "arn:aws:ssm:us-east-1:000000000000:parameter/adp/dev/platform/account-id",
            "arn:aws:ssm:us-east-1:000000000000:parameter/adp/dev/insights/l5-consumer-verification-keys",
            "arn:aws:ssm:us-east-1:000000000000:parameter/adp/dev/insights/l5-authority-gateway-url",
        })
        network_read = next(statement for statement in policy["Statement"]
                            if "ec2:DescribeVpcEndpoints" in statement["Action"])
        self.assertEqual(set(network_read["Action"]), {
            "ec2:DescribePrefixLists", "ec2:DescribeVpcEndpoints", "ec2:DescribeNetworkInterfaces",
        })
        self.assertEqual(network_read["Condition"], {"StringEquals": {"aws:RequestedRegion": "us-east-1"}})
        self.assertFalse(self.log.exists())

    def test_bootstrap_cannot_bypass_role_admission_or_boundaries(self):
        result = self.invoke("--bootstrap-ci", "--account", "000000000000", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        statements = json.loads(result.stdout)["Statement"]
        for role in ("emitter", "distiller", "unreviewed"):
            resource = f"arn:aws:iam::000000000000:role/adp-dev-insights-{role}"
            for action in ("iam:CreateRole", "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy",
                           "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary",
                           "iam:AttachRolePolicy", "iam:PassRole"):
                with self.subTest(role=role, action=action):
                    for statement in statements:
                        actions = statement["Action"]
                        resources = statement["Resource"]
                        actions = [actions] if isinstance(actions, str) else actions
                        resources = [resources] if isinstance(resources, str) else resources
                        self.assertFalse(statement["Effect"] == "Allow" and
                                         any(fnmatch.fnmatchcase(action, pattern) for pattern in actions) and
                                         any(fnmatch.fnmatchcase(resource, pattern) for pattern in resources))
        self.assertFalse(self.log.exists())

    def test_bootstrap_state_access_excludes_optional_roots_and_platform_writes(self):
        result = self.invoke("--bootstrap-ci", "--account", "000000000000", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        statements = json.loads(result.stdout)["Statement"]

        def permits(action, resource):
            return any(statement["Effect"] == "Allow" and
                       any(fnmatch.fnmatchcase(action, pattern) for pattern in
                           ([statement["Action"]] if isinstance(statement["Action"], str) else statement["Action"])) and
                       any(fnmatch.fnmatchcase(resource, pattern) for pattern in
                           ([statement["Resource"]] if isinstance(statement["Resource"], str) else statement["Resource"]))
                       for statement in statements)

        bucket = "arn:aws:s3:::adp-terraform-state-000000000000"
        ordinary = bucket + "/dev/modules/insights/terraform.tfstate"
        platform = bucket + "/dev/platform/terraform.tfstate"
        optional = bucket + "/dev/modules/insights/poc/devlake/terraform.tfstate"
        self.assertTrue(permits("s3:ListBucket", bucket))
        for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
            with self.subTest(action=action):
                self.assertTrue(permits(action, ordinary))
                self.assertFalse(permits(action, optional))
                self.assertFalse(permits(action, ordinary + ".backup"))
                self.assertEqual(permits(action, platform), action == "s3:GetObject")
        self.assertFalse(self.log.exists())

    def test_ordinary_plans_and_upgrades_ignore_optional_flags_and_state(self):
        modes = ({}, {"FEATURE_DEVLAKE_CONNECTORS_ENABLED": "true", "DEVLAKE_COLLECTION_ENABLED": "false"},
                 {"FEATURE_DEVLAKE_CONNECTORS_ENABLED": "true", "DEVLAKE_COLLECTION_ENABLED": "true"})
        for flags in modes:
            for stage, options in (("infra", ("--plan",)), ("emitter", ("--image", IMAGE)),
                                   ("distiller", ("--image", DISTILLER_IMAGE))):
                with self.subTest(stage=stage, flags=flags):
                    self.log.write_text("")
                    result = self.invoke(f"--{stage}", *options, "--env", "dev", "--region", "us-east-1",
                                         env=self.env | flags)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(self.backend.read_text(), "dev/modules/insights/terraform.tfstate")
                    calls = [shlex.split(call) for call in self.log.read_text().splitlines()]
                    terraform_calls = [call for call in calls if call[0] == "terraform"]
                    self.assertTrue(terraform_calls)
                    self.assertTrue(all(call[1] == f"-chdir={SCRIPT.parent / 'platform'}" for call in terraform_calls))
                    self.assertFalse(any("devlake" in argument.lower() for call in calls for argument in call))
                    self.assertEqual(any("apply" in call for call in terraform_calls), stage != "infra")

    def test_infra_plan_preserves_existing_emitter_image(self):
        result = self.invoke("--infra", "--plan", "--env", "dev", "--region", "us-east-1",
                             env=self.env | {"TEST_EXISTING_IMAGE": IMAGE, "TEST_EXISTING_DISTILLER_IMAGE": DISTILLER_IMAGE})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-var=emitter_image=" + IMAGE, self.log.read_text())
        self.assertIn("-var=distiller_image=" + DISTILLER_IMAGE, self.log.read_text())

    def test_service_update_preserves_the_other_image(self):
        replacement_image = "registry.example/replacement@sha256:" + "c" * 64
        for stage, expected_emitter, expected_distiller in (
            ("emitter", replacement_image, DISTILLER_IMAGE), ("distiller", IMAGE, replacement_image)
        ):
            with self.subTest(stage=stage):
                result = self.invoke(f"--{stage}", "--image", replacement_image, "--plan", "--env", "dev", "--region", "us-east-1",
                                     env=self.env | {"TEST_EXISTING_IMAGE": IMAGE, "TEST_EXISTING_DISTILLER_IMAGE": DISTILLER_IMAGE})
                self.assertEqual(result.returncode, 0, result.stderr)
                plan = shlex.split(next(call for call in reversed(self.log.read_text().splitlines()) if " plan " in call))
                self.assertIn("-var=emitter_image=" + expected_emitter, plan)
                self.assertIn("-var=distiller_image=" + expected_distiller, plan)

    def test_selected_or_preserved_emitter_reads_exact_private_configuration(self):
        for stage, extra in (("emitter", ("--image", IMAGE)), ("infra", ()),
                             ("distiller", ("--image", DISTILLER_IMAGE))):
            with self.subTest(stage=stage):
                reads = pathlib.Path(self.env["TEST_RECEIVER_READS"])
                reads.unlink(missing_ok=True)
                result = self.invoke("--" + stage, "--plan", *extra, "--env", "dev", "--region", "us-east-1",
                                     env=self.env | {"TEST_EXISTING_IMAGE": IMAGE})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(reads.read_text().splitlines(), [
                    "/adp/dev/insights/l5-consumer-verification-keys", "/adp/dev/insights/l5-authority-gateway-url"])
                self.assertEqual(json.loads(pathlib.Path(self.env["TEST_VARIABLES"]).read_text()), {
                    "l5_consumer_verification_keys": {"test-key": PUBLIC_PEM}, "l5_authority_gateway_url": AUTHORITY})
                call = next(call for call in reversed(self.log.read_text().splitlines()) if " plan " in call)
                var_file = next(arg.split("=", 1)[1] for arg in shlex.split(call) if arg.startswith("-var-file="))
                self.assertFalse(pathlib.Path(var_file).exists())
                self.assertNotIn(PUBLIC_PEM, self.log.read_text())

    def test_unreadable_or_invalid_receiver_configuration_stops_before_plan(self):
        for change in ({"TEST_RECEIVER_DENIED": "1"}, {"TEST_RECEIVER_RESPONSE": "{}"},
                       {"TEST_RECEIVER_RESPONSE": "[]"}, {"TEST_RECEIVER_KEYS": "{}"},
                       {"TEST_RECEIVER_KEYS": json.dumps({"key": "-----BEGIN PRIVATE KEY-----"})},
                       {"TEST_RECEIVER_URL": AUTHORITY.replace(":80", ":8080")},
                       {"TEST_RECEIVER_URL": "https://gateway.example/path"}):
            with self.subTest(change=change):
                self.log.unlink(missing_ok=True)
                result = self.invoke("--infra", "--env", "dev", "--region", "us-east-1",
                                     env=self.env | {"TEST_EXISTING_IMAGE": IMAGE} | change)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No defaults were applied", result.stderr)
                self.assertNotIn(" plan ", self.log.read_text())
                self.assertNotIn(" apply ", self.log.read_text())

    def test_state_read_failure_stops_before_plan(self):
        result = self.invoke("--infra", "--env", "dev", "--region", "us-east-1", env=self.env | {"TEST_OUTPUT_FAIL": "1"})
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("State read failed", result.stderr)
        self.assertNotIn(" plan ", self.log.read_text())
        self.assertNotIn(" apply ", self.log.read_text())

    def test_malformed_outputs_do_not_default_to_missing_images(self):
        for outputs in ("not-json", "[]", '{"emitter_image": {}}', '{"emitter_image": {"value": null}}',
                        '{"distiller_image": {"value": "Warning: No outputs found"}}'):
            with self.subTest(outputs=outputs):
                result = self.invoke("--infra", "--plan", "--env", "dev", "--region", "us-east-1",
                                     env=self.env | {"TEST_OUTPUT_JSON": outputs})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(" plan ", self.log.read_text())

    def test_verify_initializes_clean_backend_and_binds_cluster(self):
        self.assertFalse(self.backend.exists())
        result = self.invoke("--verify", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backend.read_text(), "dev/modules/insights/terraform.tfstate")
        calls = [shlex.split(call) for call in self.log.read_text().splitlines()]
        self.assertEqual([call[2] for call in calls[:2]], ["init", "output"])
        self.assertIn("-reconfigure", calls[0])
        update, read = calls[-2:]
        self.assertEqual(update[:3], ["aws", "eks", "update-kubeconfig"])
        self.assertEqual(update[update.index("--name") + 1], "adp-dev-eks-cluster")
        self.assertEqual(update[update.index("--region") + 1], "us-east-1")
        kubeconfig = update[update.index("--kubeconfig") + 1]
        self.assertEqual(read[read.index("--kubeconfig") + 1], kubeconfig)
        self.assertEqual(read[read.index("--context") + 1], "arn:aws:eks:us-east-1:000000000000:cluster/adp-dev-eks-cluster")
        self.assertFalse(pathlib.Path(kubeconfig).exists())

    def test_verify_replaces_stale_target_without_changing_ambient_kubeconfig(self):
        self.backend.write_text("dev/modules/insights/terraform.tfstate")
        ambient = pathlib.Path(self.directory.name) / "ambient-kubeconfig"
        ambient.write_text("stale dev context")
        result = self.invoke("--verify", "--env", "prod", "--region", "us-west-2", env=self.env | {"KUBECONFIG": str(ambient)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.backend.read_text(), "prod/modules/insights/terraform.tfstate")
        self.assertEqual(ambient.read_text(), "stale dev context")
        calls = [shlex.split(call) for call in self.log.read_text().splitlines()]
        self.assertIn("-backend-config=region=us-west-2", calls[0])
        update, read = calls[-2:]
        self.assertEqual(update[update.index("--name") + 1], "adp-prod-eks-cluster")
        self.assertEqual(update[update.index("--region") + 1], "us-west-2")
        self.assertNotEqual(read[read.index("--kubeconfig") + 1], str(ambient))
        self.assertEqual(read[read.index("--context") + 1], "arn:aws:eks:us-west-2:000000000000:cluster/adp-prod-eks-cluster")

    def test_verify_propagates_failures_and_cleans_temporary_config(self):
        for failure, code in (("TEST_INIT_FAIL", 41), ("TEST_OUTPUT_FAIL", 42), ("TEST_KUBECONFIG_FAIL", 43), ("TEST_KUBECTL_FAIL", 44)):
            with self.subTest(failure=failure):
                self.log.unlink(missing_ok=True)
                result = self.invoke("--verify", "--env", "dev", "--region", "us-east-1", env=self.env | {failure: "1"})
                self.assertEqual(result.returncode, code, result.stderr)
                calls = [shlex.split(call) for call in self.log.read_text().splitlines()]
                if code < 43:
                    self.assertFalse(any(call[0] in ("aws", "kubectl") for call in calls))
                else:
                    update = next(call for call in calls if call[0] == "aws")
                    self.assertFalse(pathlib.Path(update[update.index("--kubeconfig") + 1]).exists())

    def test_verify_plan_is_rejected_before_target_access(self):
        result = self.invoke("--verify", "--plan", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())

    def test_rejects_mutable_images_and_account_mismatch(self):
        result = self.invoke("--emitter", "--image", "registry.example/insights:latest", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.log.exists())
        result = self.invoke("--emitter", "--image", IMAGE, "--plan", "--env", "dev", "--region", "us-east-1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("-var=emitter_image=" + IMAGE, self.log.read_text())
        result = self.invoke("--plan", "--env", "dev", "--region", "us-east-1", env=self.env | {"TEST_ACTIVE_ACCOUNT": "111111111111"})
        self.assertEqual(result.returncode, 2)
        self.assertIn("differs", result.stderr)


if __name__ == "__main__":
    unittest.main()
