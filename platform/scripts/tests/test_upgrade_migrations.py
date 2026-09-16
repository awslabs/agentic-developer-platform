"""Legacy KMS ownership transfers must preserve key identity and recover safely."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("migrations", Path(__file__).resolve().parents[1] / "upgrade-migrations.py")
migrations = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migrations)


def resource(kind, resource_name, **attributes):
    return {"mode": "managed", "type": kind, "name": resource_name, "instances": [{"attributes": attributes}]}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.alias = {"AliasName": "alias/adp-dev-webhook-secrets", "TargetKeyId": "original-key"}
        self.states = {"platform": {"resources": []}, "gateway": {"resources": [
            resource("aws_kms_alias", "dynamodb", name="gateway-alias", target_key_id="gateway-key")]},
            "webhook-ingress": {"resources": [
                resource("aws_kms_key", "secrets", id="original-key"),
                resource("aws_kms_alias", "secrets", name=self.alias["AliasName"], target_key_id="original-key"),
                resource("aws_kms_alias", "gateway_dynamodb", name="gateway-alias", target_key_id="gateway-key")]}}

    def test_migrates_key_alias_and_removes_only_verified_duplicate(self):
        result = migrations.transfers(self.states, self.alias)
        self.assertEqual(len(result), 3)
        self.assertEqual([row[-1] for row in result], [True, True, False])
        self.assertEqual(result[0][4], "original-key")

    def test_resumes_after_import_without_reimporting_key(self):
        self.states["platform"]["resources"].append(resource("aws_kms_key", "webhook_secrets", id="original-key"))
        self.assertFalse(migrations.transfers(self.states, self.alias)[0][-1])

    def test_conflicting_platform_key_stops_before_state_changes(self):
        self.states["platform"]["resources"].append(resource("aws_kms_key", "webhook_secrets", id="different-key"))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            migrations.transfers(self.states, self.alias)

    def test_changed_alias_target_stops(self):
        alias = dict(self.alias, TargetKeyId="unexpected-key")
        with self.assertRaises(ValueError):
            migrations.transfers(self.states, alias)

    def test_cannot_forget_alias_without_matching_gateway_owner(self):
        for gateway in ({}, {"resources": [resource("aws_kms_alias", "dynamodb", name="gateway-alias", target_key_id="wrong-key")]}):
            states = copy.deepcopy(self.states)
            states["gateway"] = gateway
            with self.assertRaisesRegex(ValueError, "gateway ownership"):
                migrations.transfers(states, self.alias)

    def test_already_migrated_is_noop(self):
        self.states["webhook-ingress"] = {}
        self.assertEqual(migrations.transfers(self.states, self.alias), [])

    def test_failed_import_never_removes_source_tracking(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "integration-before.json").write_text(json.dumps({"account": "123", "environment": "dev", "modules": list(self.states)}))
            for name, state in self.states.items():
                (directory / (name + "-before.tfstate")).write_text(json.dumps(state))
            calls = []
            def run(command, cwd=None):
                calls.append(command)
                if command[:3] == ["aws", "sts", "get-caller-identity"]:
                    return '{"Account":"123"}'
                if command[:3] == ["aws", "kms", "list-aliases"]:
                    return json.dumps({"Aliases": [self.alias]})
                if command[1] == "init":
                    return ""
                if command[1:3] == ["state", "pull"]:
                    module = "webhook-ingress" if "webhook-ingress" in str(cwd) else "gateway" if "gateway" in str(cwd) else "platform"
                    return json.dumps(self.states[module])
                if command[1] == "import":
                    raise RuntimeError("Simulated import failure")
                self.fail("Unexpected mutation: " + repr(command))
            with patch.object(migrations, "run", run), patch("sys.argv", ["migration", "--root", temporary, "--directory", temporary]):
                with self.assertRaisesRegex(RuntimeError, "import failure"):
                    migrations.main()
            self.assertFalse(any(command[1:3] == ["state", "rm"] for command in calls))


if __name__ == "__main__":
    unittest.main()
