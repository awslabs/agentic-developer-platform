"""Protected config is a bounded addition to the job's authentic private inputs."""

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unittest

from modules.tools.platform.scripts.knowledge_overlay import FIELDS, merge
from modules.tools.platform.scripts.release_plan import Refused, validate
from modules.tools.platform.tests.release_plan_fixtures import (
    ACCOUNT, BINDING, SELECTORS, existing_target_changes, gateway_change, knowledge_change,
)


ROOT = Path(__file__).resolve().parents[4]


class KnowledgeOverlayTests(unittest.TestCase):
    def setUp(self):
        self.inputs = {
            "aws_account_id": ACCOUNT, "aws_region": "us-east-1", "environment": "dev",
            "gateway_role_arn": f"arn:aws:iam::{ACCOUNT}:role/adp-tools-dev-gateway",
            "oauth_provider_secret_ids": {"github": "original-private-reference"},
            "targets": [{"name": "original", "input_properties": [{"name": "query", "type": "string"}]}],
        }

    def merge(self, inputs=None, config=None, **kwargs):
        arguments = {
            "selectors": json.dumps(SELECTORS), "account": ACCOUNT,
            "region": "us-east-1", "environment": "dev", **kwargs,
        }
        return merge(self.inputs if inputs is None else inputs,
                     json.dumps(BINDING if config is None else config), **arguments)

    def test_absent_config_preserves_ordinary_release_without_new_selectors(self):
        self.assertEqual(merge(self.inputs, "", selectors="", account="", region="", environment=""), self.inputs)

    def test_exact_five_additions_preserve_original_inputs_without_mutation(self):
        before = deepcopy(self.inputs)
        result = self.merge()
        self.assertEqual(set(result) - set(before), set(FIELDS))
        self.assertEqual({key: result[key] for key in before}, before)
        self.assertEqual(self.inputs, before)
        result["oauth_provider_secret_ids"]["github"] = "changed-local-copy"
        self.assertEqual(self.inputs, before)

    def test_explicit_source_defaults_are_first_enablement(self):
        inactive = {**self.inputs, **{field: deepcopy(default) for field, (_, default) in FIELDS.items()}}
        self.assertEqual(self.merge(inactive), self.merge())
        source = (ROOT / "modules/tools/platform/infra/variables.tf").read_text()
        for field, (_, default) in FIELDS.items():
            body = re.search(r'variable "' + field + r'"\s*\{(.*?)(?=\n\})', source, re.S).group(1)
            self.assertRegex(body, r"default\s*=\s*" + re.escape(json.dumps(default)))

    def test_matching_existing_config_is_idempotent_with_set_order(self):
        original = self.merge()
        original["knowledge_door_subnet_ids"].reverse()
        result = self.merge(original, first_create=json.dumps({**BINDING, "subnet_ids": list(reversed(BINDING["subnet_ids"]))}))
        self.assertEqual(result, original)

    def test_conflicting_or_null_existing_fields_are_refused(self):
        for field, (_, default) in FIELDS.items():
            for value in (None, ["foreign"] if isinstance(default, list) else "foreign"):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(Refused, "conflicts"):
                    self.merge({**self.inputs, field: value})

    def test_duplicate_existing_network_entries_are_not_an_idempotent_set(self):
        original = self.merge()
        original["knowledge_door_subnet_ids"].append(original["knowledge_door_subnet_ids"][0])
        with self.assertRaisesRegex(Refused, "conflicts"):
            self.merge(original)

    def test_binding_cannot_clear_replace_or_add_arbitrary_inputs(self):
        for field, invalid in (("endpoint", ""), ("subnet_ids", []), ("security_group_ids", []),
                               ("routing_domain", ""), ("gateway_arn", "foreign"),
                               ("gateway_role_arn", "arbitrary-role"), ("targets", [])):
            with self.subTest(field=field), self.assertRaises(Refused):
                self.merge(config={**BINDING, field: invalid})

    def test_wrong_environment_account_region_or_gateway_is_refused(self):
        for kwargs in ({"account": "000000000000"}, {"region": "us-west-2"}, {"environment": "prod"},
                       {"selectors": json.dumps({**SELECTORS, "components": {"platform targets": {"gateway_id": "foreign", "target_ids": ["existing"]}}})},
                       {"selectors": ""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(Refused):
                self.merge(**kwargs)
        for field, value in (("aws_account_id", "000000000000"), ("aws_region", "us-west-2"), ("environment", "prod")):
            with self.subTest(field=field), self.assertRaises(Refused):
                self.merge({**self.inputs, field: value})

    def test_binding_network_and_route_validation_are_not_bypassed(self):
        for changed in ({"version": True}, {"subnet_ids": [BINDING["subnet_ids"][0]]},
                        {"subnet_ids": [BINDING["subnet_ids"][0]] * 2},
                        {"security_group_ids": ["sg-foreign"]},
                        {"endpoint": BINDING["endpoint"] + "?secret=do-not-print"},
                        {"endpoint": BINDING["endpoint"].replace("/dev/", "/prod/")},
                        {"routing_domain": BINDING["routing_domain"].replace("-regional.", "-regional-us-east-1a.")}):
            with self.subTest(changed=changed), self.assertRaises(Refused):
                self.merge(config={**BINDING, **changed})

    def test_separate_first_create_binding_must_match_durable_config(self):
        for changed in ({"endpoint": BINDING["endpoint"].replace("fixture.", "different.")},
                        {"vpc_id": "vpc-00000000000000002"},
                        {"subnet_ids": ["subnet-00000000000000003", "subnet-00000000000000004"]},
                        {"security_group_ids": ["sg-00000000000000002"]},
                        {"routing_domain": BINDING["routing_domain"].replace("regional.", "different.")}):
            with self.subTest(changed=changed), self.assertRaisesRegex(Refused, "binding differ"):
                self.merge(first_create=json.dumps({**BINDING, **changed}))

    def test_durable_config_remains_after_create_binding_is_removed(self):
        initial = self.merge(first_create=json.dumps(BINDING))
        retained = self.merge(initial, first_create="")
        self.assertEqual(initial, retained)
        created = knowledge_change()
        plan = {"resource_changes": [gateway_change(), *existing_target_changes(), created]}
        with self.assertRaisesRegex(Refused, "new targets are disabled"):
            validate(plan, account=ACCOUNT, region="us-east-1", environment="dev", release_selectors=json.dumps(SELECTORS))
        existing = deepcopy(created["change"]["after"])
        existing["target_id"] = "knowledge-existing"
        created["change"].update(actions=["no-op"], before=existing, after=deepcopy(existing), after_unknown={})
        selectors = deepcopy(SELECTORS)
        selectors["components"]["platform targets"]["target_ids"].append("knowledge-existing")
        validate(plan, account=ACCOUNT, region="us-east-1", environment="dev", release_selectors=json.dumps(selectors))

    def test_cli_keeps_secret_private_and_refuses_overwrite_or_bad_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            original, destination = directory / "inputs.json", directory / "merged.json"
            original.write_text(json.dumps(self.inputs))
            before = original.read_bytes()
            env = {**os.environ, "ADP_TOOLS_KNOWLEDGE_CONFIG_JSON": json.dumps(BINDING),
                   "ADP_TOOLS_RELEASE_SELECTORS_JSON": json.dumps(SELECTORS),
                   "ADP_TOOLS_ACCOUNT_ID": ACCOUNT, "ADP_TOOLS_AWS_REGION": "us-east-1",
                   "ADP_TOOLS_ENVIRONMENT": "dev", "ADP_TOOLS_KNOWLEDGE_CREATE_JSON": ""}
            command = [sys.executable, "-m", "modules.tools.platform.scripts.knowledge_overlay", str(original), str(destination)]
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(original.read_bytes(), before)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(json.loads(destination.read_text()), self.merge())
            result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            destination.unlink()
            for value in ('{"version":1,"version":1}', 'invalid-original-private-reference'):
                env["ADP_TOOLS_KNOWLEDGE_CONFIG_JSON"] = value
                result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn(b"original-private-reference", result.stdout + result.stderr)
                self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
