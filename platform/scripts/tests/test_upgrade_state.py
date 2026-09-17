"""Preservation contracts for older deployments and existing GitHub installs."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("upgrade_state", SCRIPTS / "upgrade-state.py")
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def resource(kind, name, attributes, module=""):
    return {"mode": "managed", "type": kind, "name": name, "module": module,
            "instances": [{"attributes": attributes}]}


class PreservationTests(unittest.TestCase):
    def test_preserves_admins_and_cidrs_without_promoting_other_principals(self):
        old = {"resources": [resource("aws_eks_access_entry", "admins", {"principal_arn": "admin"}, "module.eks"),
                             resource("aws_eks_access_entry", "viewer", {"principal_arn": "viewer"}, "module.eks")]}
        cluster = {"resourcesVpcConfig": {"publicAccessCidrs": ["192.0.2.1/32"]}}
        result = state.preserve_access(old, cluster, ["operator", "admin"], ["198.51.100.3/32"])
        self.assertEqual(result["extra_cluster_admin_principal_arns"], ["admin", "operator"])
        self.assertEqual(result["eks_public_access_cidrs"], ["192.0.2.1/32", "198.51.100.3/32"])

    def test_invalid_access_cidr_refuses(self):
        with self.assertRaises(ValueError):
            state.preserve_access({}, {"resourcesVpcConfig": {}}, requested_cidrs=["invalid"])

    def test_private_endpoint_is_not_made_public_by_an_upgrade(self):
        result = state.preserve_access({}, {"resourcesVpcConfig": {"endpointPublicAccess": False, "endpointPrivateAccess": True}})
        self.assertFalse(result["eks_endpoint_public_access"])
        self.assertTrue(result["eks_endpoint_private_access"])

    def test_retains_each_existing_repository_encryption(self):
        old = {"resources": [
            resource("aws_ecr_repository", "main", {"name": "old-kms", "encryption_configuration": [
                {"encryption_type": "KMS", "kms_key": "arn:aws:kms:us-east-1:123456789012:key/existing"}]}, "module.ecr"),
            resource("aws_ecr_repository", "main", {"name": "old-aes", "encryption_configuration": [
                {"encryption_type": "AES256", "kms_key": ""}]}, "module.ecr"),
            resource("aws_ecr_repository", "other", {"name": "unrelated"}, "module.other")]}
        self.assertEqual(state.repository_encryption(old), {
            "old-kms": {"encryption_type": "KMS", "kms_key": "arn:aws:kms:us-east-1:123456789012:key/existing"},
            "old-aes": {"encryption_type": "AES256", "kms_key": None}})

    def test_missing_existing_repository_encryption_key_refuses(self):
        for configuration in ([], [{"encryption_type": "KMS", "kms_key": ""}]):
            old = {"resources": [resource("aws_ecr_repository", "main", {
                "name": "old-repo", "encryption_configuration": configuration}, "module.ecr")]}
            with self.assertRaisesRegex(ValueError, "encryption"):
                state.repository_encryption(old)

    def test_broker_keeps_environment_allowlist_and_token_reference(self):
        result = state.broker_settings({"ALLOWLIST_MODE": "org", "ALLOWED_ORGS": "customer-team",
                                       "ALLOW_OPEN_SIGNUP": "false", "GITHUB_TOKEN_SECRET_ARN": "customer-secret"})
        self.assertEqual(result, {"github_auth_allowlist_mode": "org", "github_auth_allowed_orgs": "customer-team",
                                 "github_auth_allow_open_signup": False, "github_auth_token_secret_arn": "customer-secret"})

    def test_old_broker_open_mode_keeps_login_working(self):
        self.assertTrue(state.broker_settings({"ALLOWLIST_MODE": "open"})["github_auth_allow_open_signup"])
        self.assertFalse(state.broker_settings({"ALLOWLIST_MODE": "org"})["github_auth_allow_open_signup"])

    def factory(self):
        return {"resources": [
            resource("kubernetes_secret", "arc_runner", {"data": {"github_app_installation_id": "456", "github_app_private_key": "DO-NOT-EXPORT"},
                     "metadata": [{"namespace": "customer-runners"}]}, "module.arc_runner[0]"),
            resource("helm_release", "arc_runner_set", {"metadata": [{"values": json.dumps({"githubConfigUrl": "https://github.com/customer/repo"})}]}, "module.arc_runner[0]"),
            resource("aws_dynamodb_table_item", "scaledjob_worker_agent", {})]}

    def test_arc_keeps_org_repo_installation_namespace_and_seed(self):
        result = state.factory_settings(self.factory())
        self.assertEqual(result, {"enable_github_apps": True, "seed_agent_registry": True, "github_org": "customer",
                                 "github_repo": "repo", "github_app_dev_installation_id": "456", "runner_namespace": "customer-runners"})
        self.assertNotIn("DO-NOT-EXPORT", json.dumps(result))

    def test_cannot_recover_arc_config_refuses_instead_of_defaulting_org(self):
        old = self.factory()
        old["resources"] = old["resources"][:1]
        with self.assertRaisesRegex(ValueError, "organization"):
            state.factory_settings(old)

    def test_unconfigured_factory_stays_unconfigured(self):
        result = state.factory_settings({"outputs": {"secrets_prefix": {"value": "adp/customer/gh-app-"}}})
        self.assertFalse(result["enable_github_apps"])
        self.assertEqual(result["github_org"], "customer")

    def test_factory_installed_before_github_setup_can_be_upgraded_again(self):
        result = state.factory_settings({"outputs": {"github_org": {"value": ""}, "secrets_prefix": {"value": ""}}})
        self.assertEqual(result["github_org"], "")
        self.assertFalse(result["enable_github_apps"])

    def test_existing_factory_runner_role_name_is_recovered(self):
        for name in ("adp-dev-agent-runner-role", "adp-dev-agent-factory-runner-role"):
            result = state.factory_settings({"resources": [
                resource("aws_iam_role", "runner", {"name": name}, "module.runner_iam"),
                resource("aws_iam_role", "runner", {"name": "unrelated"}, "module.other")]})
            self.assertEqual(result["runner_role_name"], name)

    def baseline(self):
        return {"secrets": {"app-key": ["version-1"]}, "mappings": {"identity": [
            {"identity_type": {"S": "github_installation_id"}, "identity_value": {"S": "456"}, "org_id": {"S": "customer"}}]}}

    def test_existing_installation_and_credentials_survive_additive_changes(self):
        before = self.baseline()
        after = copy.deepcopy(before)
        after["secrets"]["new-app"] = ["new-version"]
        after["mappings"]["identity"].append({"identity_value": {"S": "789"}})
        state.assert_integrations_preserved(before, after)

    def test_secret_rotation_removal_and_mapping_change_refuse(self):
        before = self.baseline()
        for mutation in (lambda d: d["secrets"].clear(), lambda d: d["secrets"].update({"app-key": ["version-2"]}),
                         lambda d: d["mappings"]["identity"].clear(),
                         lambda d: d["mappings"]["identity"][0].update(org_id={"S": "wrong-tenant"})):
            after = copy.deepcopy(before)
            mutation(after)
            with self.assertRaises(ValueError):
                state.assert_integrations_preserved(before, after)

    def test_snapshot_never_reads_private_keys(self):
        calls = []
        def aws(*args):
            calls.append(args)
            if args[:2] == ("secretsmanager", "list-secrets"):
                return {"SecretList": [{"Name": "adp/dev/tenants/acme/github-app", "ARN": "app-key"}]}
            if args[:2] == ("secretsmanager", "describe-secret"):
                return {"VersionIdsToStages": {"version-1": ["AWSCURRENT"]}}
            self.fail(f"Unexpected secret access: {args[:2]}")
        with patch.object(state, "aws", aws):
            snapshot = state.integration_snapshot({}, "dev")
        self.assertEqual(snapshot["secrets"], {"app-key": ["version-1"]})
        self.assertEqual(len(calls), 2)


class RequiredFactoryTests(unittest.TestCase):
    def prepare(self, modules=None, org="customer", override="", account="111122223333", roles=(), owned_role=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = {"account": "111122223333", "modules": modules if modules is not None else ["platform", "gateway", "webhook-ingress"],
                      "broker_settings": {"ALLOWED_ORGS": org}}
            (root / "integration-before.json").write_text(json.dumps(before))
            original = {"environment": "dev", "aws_region": "us-east-1", "enable_github_apps": True,
                        "github_org": "existing-org", "github_app_dev_installation_id": "existing-installation"}
            if owned_role:
                original["runner_role_name"] = owned_role
            target = root / "agent-factory.tfvars.json"
            target.write_text(json.dumps(original))
            args = SimpleNamespace(directory=directory, github_org=override)
            def aws(*args):
                if args == ("sts", "get-caller-identity"):
                    return {"Account": account}
                if args == ("iam", "list-roles") and not owned_role:
                    return {"Roles": [{"RoleName": name} for name in roles]}
                self.fail(f"Unexpected AWS call: {args}")
            with patch.object(state, "aws", side_effect=aws):
                state.prepare_factory(args)
            return original, json.loads(target.read_text())

    def test_missing_factory_installs_without_copying_platform_app_configuration(self):
        _, result = self.prepare()
        self.assertEqual(result["github_org"], "customer")
        self.assertFalse(result["enable_github_apps"])
        self.assertFalse(result["seed_agent_registry"])
        self.assertEqual(result["github_app_dev_installation_id"], "")
        self.assertEqual(result["github_repo"], "")
        self.assertTrue(result["gateway_deployed"])
        self.assertEqual(result["environment"], "dev")

    def test_existing_factory_configuration_is_not_reinitialized(self):
        before, after = self.prepare(modules=["platform", "gateway", "webhook-ingress", "agent-factory"],
                                     owned_role="adp-dev-agent-runner-role")
        self.assertEqual(before, after)

    def test_unoccupied_default_runner_name_is_used(self):
        _, result = self.prepare()
        self.assertEqual(result["runner_role_name"], "adp-dev-agent-runner-role")

    def test_legacy_role_collision_is_avoided_for_missing_and_partial_factory(self):
        for modules in (["platform", "gateway", "webhook-ingress"],
                        ["platform", "gateway", "webhook-ingress", "agent-factory"]):
            before, after = self.prepare(modules=modules, roles=["adp-dev-agent-runner-role"])
            self.assertEqual(after["runner_role_name"], "adp-dev-agent-factory-runner-role")
            if "agent-factory" in modules:
                self.assertEqual({k: v for k, v in after.items() if k != "runner_role_name"}, before)

    def test_owned_alternate_role_is_preserved_on_later_upgrades(self):
        before, after = self.prepare(modules=["platform", "gateway", "webhook-ingress", "agent-factory"],
                                     owned_role="adp-dev-agent-factory-runner-role")
        self.assertEqual(before, after)

    def test_both_unowned_names_occupied_refuses_without_adopting_roles(self):
        with self.assertRaisesRegex(ValueError, "refusing to adopt"):
            self.prepare(roles=["adp-dev-agent-runner-role", "adp-dev-agent-factory-runner-role"])

    def test_missing_dependencies_stop_before_installation(self):
        for modules in (["platform"], ["platform", "gateway"], ["platform", "webhook-ingress"]):
            with self.subTest(modules=modules), self.assertRaisesRegex(ValueError, "prerequisites"):
                self.prepare(modules=modules)

    def test_unconfigured_or_multiple_orgs_do_not_require_upfront_github_setup(self):
        for org in ("", "one,two", "*", "one/two"):
            with self.subTest(org=org):
                _, result = self.prepare(org=org)
                self.assertEqual(result["github_org"], "")
                self.assertFalse(result["enable_github_apps"])
        _, result = self.prepare(org="one,two", override="intended-org")
        self.assertEqual(result["github_org"], "intended-org")
        with self.assertRaisesRegex(ValueError, "ADP_GITHUB_ORG"):
            self.prepare(override="one,two")

    def test_wrong_account_stops_before_installation(self):
        with self.assertRaisesRegex(ValueError, "account differs"):
            self.prepare(account="444455556666")

    def verify(self, deployed):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = {"account": "111122223333", "environment": "dev", "modules": [], "outputs": {},
                      "secrets": {}, "mappings": {}, "broker_function": None}
            (root / "integration-before.json").write_text(json.dumps(before))
            def aws(*args):
                if args[:2] == ("sts", "get-caller-identity"):
                    return {"Account": before["account"]}
                if args[:2] == ("s3api", "get-object"):
                    self.assertEqual(args[5], "dev/modules/agent-factory/terraform.tfstate")
                    if deployed is None:
                        raise RuntimeError("NoSuchKey")
                    Path(args[-1]).write_text(json.dumps(deployed))
                    return {}
                self.fail(f"Unexpected AWS call: {args[:2]}")
            args = SimpleNamespace(directory=directory, require_module=["agent-factory"])
            with patch.object(state, "aws", side_effect=aws), patch.object(state, "integration_snapshot", return_value=before):
                state.verify(args)
            return json.loads((root / "module-verification.json").read_text())

    def test_required_factory_is_checked_even_when_absent_from_original_snapshot(self):
        result = self.verify({"resources": [resource("aws_sqs_queue", "input", {"name": "factory-queue"})]})
        self.assertEqual(result, {"required": ["agent-factory"], "verified": ["agent-factory"]})

    def test_missing_or_empty_required_state_cannot_report_success(self):
        with self.assertRaisesRegex(RuntimeError, "NoSuchKey"):
            self.verify(None)
        with self.assertRaisesRegex(ValueError, "no deployed resources"):
            self.verify({"resources": []})


if __name__ == "__main__":
    unittest.main()
