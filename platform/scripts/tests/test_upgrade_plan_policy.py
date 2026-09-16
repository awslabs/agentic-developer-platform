import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("policy", Path(__file__).resolve().parents[1] / "upgrade-plan-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def change(address, kind, before, after, actions=("delete", "create")):
    return {"address": address, "type": kind, "change": {"before": before, "after": after, "actions": list(actions)}}


class PlanPolicyTests(unittest.TestCase):
    def evaluate(self, resource, module="gateway"):
        return policy.evaluate({"resource_changes": [resource]}, module, "123456789012")

    def test_api_revision_is_routine_only_with_same_api_and_create_before_delete(self):
        r = change("module.api_gateway[0].aws_api_gateway_deployment.main", "aws_api_gateway_deployment",
                   {"rest_api_id": "existing"}, {"rest_api_id": "existing"}, ("create", "delete"))
        self.assertEqual(self.evaluate(r)["routine"], [r["address"]])
        for after, order in (("different", ["create", "delete"]), ("existing", ["delete", "create"]), (None, ["create", "delete"])):
            bad = copy.deepcopy(r)
            bad["change"].update(after={"rest_api_id": after}, actions=order)
            self.assertTrue(self.evaluate(bad)["blocked"])

    def test_s3_permission_only_allows_source_account_tightening(self):
        before = {"function_name": "existing", "action": "lambda:InvokeFunction", "principal": "s3.amazonaws.com", "source_arn": "existing-bucket"}
        r = change("module.budget_lambda[0].aws_lambda_permission.usage_tracker_s3", "aws_lambda_permission", before,
                   dict(before, source_account="123456789012"))
        self.assertTrue(self.evaluate(r)["routine"])
        for key, value in (("source_account", "other-account"), ("principal", "*"), ("source_arn", "other-bucket")):
            bad = copy.deepcopy(r)
            bad["change"]["after"][key] = value
            self.assertTrue(self.evaluate(bad)["blocked"])

    def test_known_worker_manifests_can_change_in_same_cluster(self):
        old = {"namespace": "adp-agents", "cluster_name": "existing", "cluster_region": "us-east-1", "manifest_sha": "old"}
        r = change("null_resource.keda_scaledjob", "null_resource", {"triggers": old}, {"triggers": dict(old, manifest_sha="new")})
        self.assertTrue(self.evaluate(r, "webhook-ingress")["routine"])
        for key in ("namespace", "cluster_name", "cluster_region"):
            bad = copy.deepcopy(r)
            bad["change"]["after"]["triggers"][key] = "different"
            self.assertTrue(self.evaluate(bad, "webhook-ingress")["blocked"])
        r["address"] = "null_resource.unreviewed"
        self.assertTrue(self.evaluate(r, "webhook-ingress")["blocked"])

    def test_empty_and_null_lambda_qualifiers_are_equivalent(self):
        before = {"function_name": "existing", "action": "lambda:InvokeFunction", "principal": "s3.amazonaws.com",
                  "source_arn": "existing-bucket", "qualifier": ""}
        after = dict(before, source_account="123456789012", qualifier=None)
        r = change("module.budget_lambda[0].aws_lambda_permission.usage_tracker_s3", "aws_lambda_permission", before, after)
        self.assertTrue(self.evaluate(r)["routine"])
        for key, value in (("qualifier", "version-1"), ("principal_org_id", "different-org"),
                           ("function_url_auth_type", "NONE"), ("event_source_token", "different-token")):
            bad = copy.deepcopy(r)
            bad["change"]["after"][key] = value
            self.assertTrue(self.evaluate(bad)["blocked"])

    def test_stateful_delete_is_never_routine(self):
        for kind in ("aws_db_instance", "aws_s3_bucket", "aws_eks_access_entry", "aws_dynamodb_table"):
            r = change(kind + ".existing", kind, {"id": "existing"}, None, ("delete",))
            self.assertTrue(self.evaluate(r)["blocked"])

    def test_existing_credentials_cannot_be_reset_even_without_replacement(self):
        r = change("aws_secretsmanager_secret_version.github", "aws_secretsmanager_secret_version",
                   {"secret_id": "existing", "secret_string": "REAL-KEY"},
                   {"secret_id": "existing", "secret_string": "PLACEHOLDER"}, ("update",))
        self.assertTrue(self.evaluate(r)["protected"])

    def test_secret_metadata_updates_are_allowed(self):
        r = change("aws_secretsmanager_secret.github", "aws_secretsmanager_secret",
                   {"name": "existing", "description": "old"}, {"name": "existing", "description": "new"}, ("update",))
        self.assertFalse(self.evaluate(r)["protected"])

    def test_identity_table_and_installation_row_are_protected(self):
        table = change("aws_dynamodb_table.identity", "aws_dynamodb_table", {"name": "adp-dev-identity-index"}, None, ("delete",))
        row = change("aws_dynamodb_table_item.install", "aws_dynamodb_table_item", {"item": '{"identity_type":"github_installation_id"}'}, {"item": "{}"}, ("update",))
        for r in (table, row):
            self.assertTrue(self.evaluate(r)["protected"])


if __name__ == "__main__":
    unittest.main()
