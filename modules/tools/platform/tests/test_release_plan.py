import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml

from modules.tools.platform.scripts.release_plan import Refused, validate
from modules.tools.platform.tests.release_plan_fixtures import ACCOUNT, BINDING, SELECTORS, existing_target_changes, gateway_change, knowledge_change


ROOT = Path(__file__).resolve().parents[4]


class ReleasePlanTests(unittest.TestCase):
    def check(self, *resources, binding=BINDING, selectors=SELECTORS):
        addresses = {resource["address"] for resource in resources}
        resources = [*resources, *(target for target in existing_target_changes() if target["address"] not in addresses)]
        validate({"resource_changes": resources}, account=ACCOUNT, region="us-east-1", environment="dev",
                 release_selectors=json.dumps(selectors) if selectors is not None else "",
                 knowledge_binding=json.dumps(binding) if binding is not None else "")

    def test_approved_first_create_preserves_existing_updates(self):
        existing = {"type": "aws_bedrockagentcore_gateway_target", "address": 'aws_bedrockagentcore_gateway_target.tools["browser"]',
                    "change": {"actions": ["update"], "before": {"gateway_identifier": "example", "target_id": "browser-target"},
                               "after": {"gateway_identifier": "example", "target_id": "browser-target", "description": "updated"}}}
        self.check(gateway_change(["update"]), existing, knowledge_change())
        self.check(gateway_change(), existing, binding=None)

    def test_provider_empty_optionals_and_subnet_order_are_accepted(self):
        target = knowledge_change()
        target["change"]["after"]["private_endpoint"][0]["managed_vpc_resource"][0]["subnet_ids"].reverse()
        target["change"]["after"]["credential_provider_configuration"][0]["gateway_iam_role"][0]["region"] = None
        self.check(gateway_change(), target)

    def test_new_target_is_default_off(self):
        with self.assertRaisesRegex(Refused, "disabled"):
            self.check(gateway_change(), knowledge_change(), binding=None)

    def test_dynamic_target_requires_semantic_search_disabled_before_apply(self):
        gateway = gateway_change(["update"])
        gateway["change"]["before"]["protocol_configuration"][0]["mcp"][0]["search_type"] = "SEMANTIC"
        self.check(gateway, knowledge_change())
        gateway["change"]["after"]["protocol_configuration"][0]["mcp"][0]["search_type"] = "SEMANTIC"
        with self.assertRaisesRegex(Refused, "without semantic tool search"):
            self.check(gateway, knowledge_change())
        # Existing static targets retain semantic search when no dynamic target exists.
        self.check(gateway, binding=None)
        for protocol in (None, [], [{"mcp": []}]):
            gateway["change"]["after"]["protocol_configuration"] = protocol
            with self.subTest(protocol=protocol), self.assertRaises(Refused):
                self.check(gateway, knowledge_change())
        gateway = gateway_change()
        gateway["change"]["after_unknown"]["protocol_configuration"] = [{"mcp": [{"search_type": True}]}]
        with self.assertRaises(Refused):
            self.check(gateway, knowledge_change())

    def test_dynamic_priority_must_be_omitted_and_only_its_computed_leaf_can_be_unknown(self):
        # AWS provider 6.68.0 makes this optional+computed. An omitted value
        # produces this nested unknown on first creation, even with a known URL.
        target = knowledge_change()
        server = target["change"]["after"]["target_configuration"][0]["mcp"][0]["mcp_server"][0]
        server.pop("resource_priority")
        target["change"]["after_unknown"]["target_configuration"] = [
            {"mcp": [{"mcp_server": [{"resource_priority": True}]}]}
        ]
        self.check(gateway_change(), target)
        for value in (0, 999, 1000, 1001, "1000", True):
            with self.subTest(value=value), self.assertRaisesRegex(Refused, "exact DYNAMIC"):
                server["resource_priority"] = value
                self.check(gateway_change(), target)
        server["resource_priority"] = None
        self.check(gateway_change(), target)
        unresolved = target["change"]["after_unknown"]["target_configuration"][0]["mcp"][0]["mcp_server"][0]
        for field in ("endpoint", "listing_mode", "mcp_tool_schema"):
            unresolved[field] = True
            with self.subTest(field=field), self.assertRaisesRegex(Refused, "authority fields must be known"):
                self.check(gateway_change(), target)
            del unresolved[field]
        # The exception cannot hide another target configuration or server.
        target["change"]["after_unknown"]["target_configuration"].append({"mcp": True})
        with self.assertRaises(Refused):
            self.check(gateway_change(), target)

    def test_noop_and_output_only_plans_remain_supported(self):
        self.check(gateway_change(), binding=None)
        with self.assertRaises(Refused):
            self.check(binding=None)

    def test_new_unrelated_targets_and_second_knowledge_address_are_refused(self):
        for address in ('aws_bedrockagentcore_gateway_target.tools["extra"]',
                        "aws_bedrockagentcore_gateway_target.credentials[0]",
                        "aws_bedrockagentcore_gateway_target.knowledge[1]",
                        'aws_bedrockagentcore_gateway_target.user_connections["github"]'):
            with self.subTest(address=address), self.assertRaises(Refused):
                target = knowledge_change()
                target["address"] = address
                self.check(gateway_change(), target)

    def test_foreign_gateway_or_changed_gateway_authority_is_refused(self):
        for field, value in (("gateway_id", "other"), ("gateway_arn", BINDING["gateway_arn"].replace(ACCOUNT, "000000000000")),
                             ("role_arn", f"arn:aws:iam::{ACCOUNT}:role/other"), ("authorizer_type", "CUSTOM_JWT")):
            with self.subTest(field=field), self.assertRaises(Refused):
                gateway = gateway_change(["update"])
                gateway["change"]["after"][field] = value
                self.check(gateway, knowledge_change())
        with self.assertRaises(Refused):
            self.check(knowledge_change())

    def test_wrong_name_gateway_credential_endpoint_or_listing_mode_is_refused(self):
        changes = [
            (lambda after: after.update(name="other")),
            (lambda after: after.update(gateway_identifier="other")),
            (lambda after: after.update(region="us-west-2")),
            (lambda after: after["credential_provider_configuration"][0]["gateway_iam_role"][0].update(service="s3")),
            (lambda after: after["credential_provider_configuration"][0].update(oauth=[{"provider_arn": "foreign"}])),
            (lambda after: after["target_configuration"][0]["mcp"][0]["mcp_server"][0].update(endpoint="https://door.example.invalid/mcp")),
            (lambda after: after["target_configuration"][0]["mcp"][0]["mcp_server"][0].update(listing_mode="DEFAULT")),
            (lambda after: after["target_configuration"][0]["mcp"][0].update(lambda_target=[{"arn": "foreign"}])),
            (lambda after: after.update(metadata_configuration=[{"allowed_request_headers": ["authorization"]}])),
            (lambda after: after.update(certificate_configuration=[{"certificate_authority_arn": "foreign"}])),
        ]
        for index, mutate in enumerate(changes):
            with self.subTest(index=index), self.assertRaises(Refused):
                target = knowledge_change()
                mutate(target["change"]["after"])
                self.check(gateway_change(), target)

    def test_wrong_private_network_or_route_is_refused(self):
        for field, value in (("vpc_identifier", "vpc-00000000000000002"), ("subnet_ids", ["subnet-00000000000000003", "subnet-00000000000000004"]),
                             ("security_group_ids", ["sg-00000000000000002"]), ("endpoint_ip_address_type", "IPV6"),
                             ("routing_domain", "other.example.invalid"), ("tags", {"unexpected": "value"})):
            with self.subTest(field=field), self.assertRaises(Refused):
                target = knowledge_change()
                target["change"]["after"]["private_endpoint"][0]["managed_vpc_resource"][0][field] = value
                self.check(gateway_change(), target)
        with self.assertRaises(Refused):
            target = knowledge_change()
            target["change"]["after"]["private_endpoint"][0]["resource_configuration"] = [{"arn": "foreign"}]
            self.check(gateway_change(), target)

    def test_unknown_authority_is_refused_even_if_known_projection_looks_correct(self):
        for field in ("name", "gateway_identifier", "credential_provider_configuration", "target_configuration", "private_endpoint",
                      "metadata_configuration", "certificate_configuration", "region"):
            with self.subTest(field=field), self.assertRaises(Refused):
                target = knowledge_change()
                target["change"]["after_unknown"][field] = [{"nested": True}]
                self.check(gateway_change(), target)

    def test_protected_binding_rejects_wrong_gateway_region_route_and_unbounded_network(self):
        for field, value in (("gateway_arn", BINDING["gateway_arn"] + "other"), ("endpoint", BINDING["endpoint"].replace("us-east-1", "us-west-2")),
                             ("endpoint", BINDING["endpoint"] + "?query=bad"), ("vpc_id", "*"), ("subnet_ids", []),
                             ("subnet_ids", [BINDING["subnet_ids"][0]] * 2), ("security_group_ids", ["*"]),
                             ("routing_domain", BINDING["routing_domain"].replace("-regional.", "-regional-us-east-1a.")), ("version", True)):
            with self.subTest(field=field, value=value), self.assertRaises(Refused):
                binding = {**BINDING, field: value}
                self.check(gateway_change(), knowledge_change(), binding=binding)
        with self.assertRaises(Refused):
            self.check(gateway_change(), knowledge_change(), binding={**BINDING, "extra": "value"})

    def test_deletion_replacement_import_move_and_duplicate_are_refused(self):
        for actions in (["delete"], ["delete", "create"], ["create", "delete"], ["read"]):
            with self.subTest(actions=actions), self.assertRaises(Refused):
                target = knowledge_change()
                target["change"]["actions"] = actions
                self.check(gateway_change(), target)
        for mutate in (lambda target: target["change"].update(importing={"id": "foreign"}),
                       lambda target: target.update(previous_address="old"),
                       lambda target: target["change"].update(before={"target_id": "existing"})):
            with self.assertRaises(Refused):
                target = knowledge_change()
                mutate(target)
                self.check(gateway_change(), target)
        with self.assertRaises(Refused):
            self.check(gateway_change(), knowledge_change(), knowledge_change())
        with self.assertRaises(Refused):
            self.check(gateway_change(["create"]), knowledge_change())

    def test_existing_target_update_cannot_retarget_or_replace_identity(self):
        for field in ("gateway_identifier", "target_id"):
            with self.subTest(field=field), self.assertRaises(Refused):
                identity = {"gateway_identifier": "example", "target_id": "existing"}
                self.check(gateway_change(), {"address": 'aws_bedrockagentcore_gateway_target.tools["browser"]',
                           "type": "aws_bedrockagentcore_gateway_target", "change": {"actions": ["update"], "before": identity,
                           "after": {**identity, field: "foreign"}}}, binding=None)

    def test_resource_policy_update_remains_bound_to_existing_gateway(self):
        policy = {"address": "aws_bedrockagentcore_resource_policy.gateway", "type": "aws_bedrockagentcore_resource_policy",
                  "change": {"actions": ["update"], "before": {"resource_arn": BINDING["gateway_arn"]},
                             "after": {"resource_arn": BINDING["gateway_arn"]}}}
        self.check(gateway_change(), policy, binding=None)
        policy["change"]["after"]["resource_arn"] += "foreign"
        with self.assertRaises(Refused):
            self.check(gateway_change(), policy)

    def test_binding_json_duplicate_keys_are_refused(self):
        with self.assertRaises(Refused):
            validate({"resource_changes": [gateway_change(), *existing_target_changes(), knowledge_change()]}, account=ACCOUNT, region="us-east-1", environment="dev",
                     release_selectors=json.dumps(SELECTORS),
                     knowledge_binding='{"version":1,"version":1}')

    def test_protected_existing_selectors_are_required_and_fence_noop_state(self):
        for selectors in (None, {**SELECTORS, "account": "000000000000"}, {**SELECTORS, "region": "us-west-2"},
                          {**SELECTORS, "components": {"platform targets": {"gateway_id": "foreign", "target_ids": SELECTORS["components"]["platform targets"]["target_ids"]}}},
                          {**SELECTORS, "components": {"platform targets": {"gateway_id": "example", "target_ids": ["foreign-target"]}}}):
            with self.subTest(selectors=selectors), self.assertRaises(Refused):
                self.check(gateway_change(), selectors=selectors)
        target = existing_target_changes()[0]
        target["change"]["before"]["target_id"] = "foreign-target"
        target["change"]["after"]["target_id"] = "foreign-target"
        with self.assertRaisesRegex(Refused, "protected selectors"):
            self.check(gateway_change(), target)

    def test_expected_shared_lambda_noop_and_deferred_reads_are_supported(self):
        data = {"mode": "data", "type": "aws_lambda_function", "address": "data.aws_lambda_function.shared_tools[0]",
                "change": {"actions": ["read"], "before": None, "after": {"function_name": "adp-dev-shared-tools-shared", "region": "us-east-1"},
                           "after_unknown": {"environment": True, "role": True}}}
        self.check(gateway_change(), data)
        data["change"]["actions"] = ["no-op"]
        self.check(gateway_change(), data)
        data["change"]["after"]["function_name"] = "foreign"
        with self.assertRaises(Refused):
            self.check(gateway_change(), data)


class ProtectedPlanWorkflowTests(unittest.TestCase):
    def test_current_guard_is_required_before_role_and_native_gates_are_preserved(self):
        workflow = yaml.load((ROOT / ".github/workflows/tools-platform-deploy.yml").read_text(), Loader=yaml.BaseLoader)
        steps = workflow["jobs"]["release"]["steps"]
        guard = next(i for i, step in enumerate(steps) if step.get("name") == "Pin target mutation guard to the protected workflow revision")
        assume = next(i for i, step in enumerate(steps) if step.get("name") == "Assume environment-scoped deployment role")
        self.assertLess(guard, assume)
        self.assertEqual(steps[guard]["env"]["ADP_WORKFLOW_REVISION"], "${{ github.workflow_sha }}")
        release = next(step for step in steps if "bash modules/tools/deploy.sh --infra --targets" in step.get("run", ""))
        self.assertEqual(release["env"]["ADP_TOOLS_KNOWLEDGE_CREATE_JSON"], "${{ vars.ADP_TOOLS_KNOWLEDGE_CREATE_JSON || '' }}")
        self.assertEqual(release["env"]["ADP_TOOLS_KNOWLEDGE_CONFIG_JSON"], "${{ vars.ADP_TOOLS_KNOWLEDGE_CONFIG_JSON || '' }}")
        self.assertEqual(release["env"]["ADP_TOOLS_RELEASE_SELECTORS_JSON"], "${{ secrets.ADP_TOOLS_RELEASE_SELECTORS_JSON }}")
        self.assertLess(next(i for i, step in enumerate(steps) if "native_smoke_inputs download" in step.get("run", "")), assume)
        script = (ROOT / "modules/tools/deploy.sh").read_text()
        self.assertLess(script.index("native_release preflight"), script.index('terraform -chdir="$infra_dir" init'))
        self.assertLess(script.index("release_plan.py"), script.index('terraform -chdir="$infra_dir" apply'))
        drift_steps = workflow["jobs"]["drift"]["steps"]
        drift_guard = next(i for i, step in enumerate(drift_steps) if step.get("name") == "Pin target mutation guard to the protected workflow revision")
        drift_assume = next(i for i, step in enumerate(drift_steps) if step.get("name") == "Assume environment-scoped role for live-state reads")
        self.assertLess(drift_guard, drift_assume)
        drift = next(step for step in drift_steps if "bash modules/tools/deploy.sh --infra --targets" in step.get("run", ""))
        self.assertEqual(drift["env"]["ADP_TOOLS_KNOWLEDGE_CONFIG_JSON"], release["env"]["ADP_TOOLS_KNOWLEDGE_CONFIG_JSON"])
        self.assertNotIn("ADP_TOOLS_KNOWLEDGE_CREATE_JSON", drift["env"])
        self.assertEqual(release["env"]["ADP_TOOLS_CONNECTIONS_CONFIG_JSON"], "${{ vars.ADP_TOOLS_CONNECTIONS_CONFIG_JSON || '' }}")
        self.assertEqual(release["env"]["ADP_TOOLS_CONNECTIONS_CREATE_JSON"], "${{ vars.ADP_TOOLS_CONNECTIONS_CREATE_JSON || '' }}")
        self.assertEqual(drift["env"]["ADP_TOOLS_CONNECTIONS_CONFIG_JSON"], release["env"]["ADP_TOOLS_CONNECTIONS_CONFIG_JSON"])
        self.assertNotIn("ADP_TOOLS_CONNECTIONS_CREATE_JSON", drift["env"])

        # Execute the real workflow guard against a local Git history. An old
        # source is refused whether it changes the driver or just the validator.
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            subprocess.run(["git", "init", "-q", temp], check=True)
            files = ("modules/tools/deploy.sh", "modules/tools/platform/scripts/release_plan.py", "modules/tools/platform/scripts/knowledge_overlay.py", "modules/tools/platform/scripts/operational_release.py", "modules/tools/platform/scripts/native_release.py")
            for name in files:
                file = directory / name
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text((ROOT / name).read_text())
            subprocess.run(["git", "add", "."], cwd=directory, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "guard"], cwd=directory, check=True)
            revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=directory, text=True).strip()
            env = {**os.environ, "ADP_WORKFLOW_REVISION": revision}
            for guard_script in (steps[guard]["run"], drift_steps[drift_guard]["run"]):
                def run():
                    return subprocess.run(["bash", "-c", guard_script], cwd=directory, env=env, capture_output=True)
                self.assertEqual(run().returncode, 0)
                for name in files:
                    file = directory / name
                    original = file.read_text()
                    file.write_text("old unguarded source\n")
                    self.assertNotEqual(run().returncode, 0)
                    file.write_text(original)
