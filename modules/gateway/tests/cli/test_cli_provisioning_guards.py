"""Ownership and EC2 execution guards; also runnable using EC2's stdlib Python."""

import importlib.util
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


def load(name):
    directory = Path(__file__).parents[2] / "scripts"
    if not (directory / (name + ".py")).exists():
        directory = Path(__file__).parent  # Copied with worker onto EC2.
    spec = importlib.util.spec_from_file_location(name, directory / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


adapter = load("cli_provisioning")
worker = load("cli_provisioning_worker")


class ProvisioningGuards(unittest.TestCase):
    def setUp(self):
        self.config = {"region": "us-east-1", "bedrock_account": "222222222222"}
        self.prefix = "adp-e2e-20260915-120000-abcdef"
        name = "ADP-Agent-" + self.prefix
        self.stack = {
            "StackName": name,
            "StackId": "arn:aws:cloudformation:us-east-1:222222222222:stack/" + name + "/unique-id",
            "CreationTime": datetime(2026, 9, 15, 12, 1, tzinfo=timezone.utc),  # noqa: UP017 -- EC2 Python 3.9
            "Outputs": [{"OutputKey": "RoleArn", "OutputValue": "arn:aws:iam::222222222222:role/" + name}],
        }
        self.resources = {"cli_stack_absent_before": True, "cli_stack_id": self.stack["StackId"]}

    def validate(self, stack=None, resources=None):
        adapter.validate_cli_stack(
            self.stack if stack is None else stack,
            self.resources if resources is None else resources,
            self.prefix,
            self.config,
            "2026-09-15T12:00:00+00:00",
        )

    def test_accepts_exact_new_stack(self):
        self.validate()

    def test_requires_absence_evidence(self):
        with self.assertRaises(ValueError):
            self.validate(resources={})

    def test_rejects_other_account(self):
        stack = {**self.stack, "StackId": self.stack["StackId"].replace("222222222222", "333333333333")}
        with self.assertRaises(ValueError):
            self.validate(stack=stack)

    def test_rejects_preexisting_stack(self):
        with self.assertRaises(ValueError):
            self.validate(stack={**self.stack, "CreationTime": datetime(2026, 9, 14, tzinfo=timezone.utc)})  # noqa: UP017 -- EC2 Python 3.9

    def test_rejects_replacement(self):
        with self.assertRaises(ValueError):
            self.validate(resources={**self.resources, "cli_stack_id": self.stack["StackId"] + "-replacement"})

    def test_rejects_other_role(self):
        stack = deepcopy(self.stack)
        stack["Outputs"][0]["OutputValue"] += "-other"
        with self.assertRaises(ValueError):
            self.validate(stack=stack)

    def test_wrong_machine_cannot_start_cli_or_fetch_credentials(self):
        with patch.object(worker, "instance_identity", return_value={"instanceId": "i-other", "accountId": "111111111111"}):
            with patch.object(worker.subprocess, "run", side_effect=AssertionError("must not start process")):
                with self.assertRaisesRegex(RuntimeError, "owned EC2"):
                    worker.execute({"instance_id": "i-owned", "platform_account": "111111111111"}, {})


if __name__ == "__main__":
    unittest.main()
