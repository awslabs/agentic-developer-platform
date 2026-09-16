"""Preservation contracts for older deployments and existing GitHub installs."""
import copy
import importlib.util
import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
