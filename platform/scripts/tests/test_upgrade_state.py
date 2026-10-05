"""Preservation contracts for older deployments and existing GitHub installs."""
import copy
import importlib.util
import json
import os
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
    def test_resume_does_not_resubmit_completed_eks_access_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "integration-before.json").write_text(json.dumps({"account": "123", "environment": "dev"}))
            (root / "eks-access.json").write_text(json.dumps({"publicAccessCidrs": ["203.0.113.10/32"]}))
            with patch.object(state, "aws", side_effect=[{"Account": "123"},
                    {"cluster": {"status": "ACTIVE", "resourcesVpcConfig": {"publicAccessCidrs": ["203.0.113.10/32"]}}}]) as aws:
                state.open_access(SimpleNamespace(directory=directory))
                self.assertEqual(aws.call_count, 2)


    def test_promoted_gateway_layers_keep_immutable_packages_and_retention(self):
        digest = "a" * 64
        layers = {"resources": [
            resource("aws_lambda_layer_version", "pyjwt", {
                "s3_bucket": "adp-terraform-state-123456789012",
                "s3_key": f"adp-releases/sha256/{digest}/pyjwt-py313.zip",
                "layer_name": "bedrockgw-dev-pyjwt-py313", "skip_destroy": True,
            }, "module.lambda_authorizer[0]"),
            resource("aws_lambda_layer_version", "psycopg2", {
                "s3_bucket": "adp-terraform-state-123456789012",
                "s3_key": f"adp-releases/sha256/{digest}/psycopg2-py312.zip",
                "layer_name": "bedrockgw-dev-psycopg2-py312", "skip_destroy": True,
            }, "module.budget_lambda[0]"),
        ]}
        self.assertEqual(state.gateway_layer_settings(layers, "123456789012", "dev"), {
            "pyjwt_layer_s3_key": f"adp-releases/sha256/{digest}/pyjwt-py313.zip",
            "pyjwt_layer_skip_destroy": True,
            "psycopg2_layer_s3_key": f"adp-releases/sha256/{digest}/psycopg2-py312.zip",
            "psycopg2_layer_skip_destroy": True,
        })
        self.assertEqual(state.gateway_layer_settings({}, "123456789012", "dev"), {})

    def test_gateway_layer_preservation_rejects_foreign_or_unknown_artifacts(self):
        layer = resource("aws_lambda_layer_version", "pyjwt", {
            "s3_bucket": "adp-terraform-state-123456789012",
            "s3_key": "lambda-layers/pyjwt-py313.zip",
            "layer_name": "bedrockgw-dev-pyjwt-py313", "skip_destroy": False,
        }, "module.lambda_authorizer[0]")
        self.assertEqual(state.gateway_layer_settings({"resources": [layer]}, "123456789012", "dev"), {
            "pyjwt_layer_s3_key": "lambda-layers/pyjwt-py313.zip", "pyjwt_layer_skip_destroy": False,
        })
        for field, value in (("s3_bucket", "foreign-bucket"), ("s3_key", "other.zip"),
                             ("layer_name", "foreign-layer"), ("skip_destroy", "false")):
            with self.subTest(field=field):
                bad = copy.deepcopy(layer)
                bad["instances"][0]["attributes"][field] = value
                with self.assertRaisesRegex(ValueError, "Cannot preserve installed pyjwt layer"):
                    state.gateway_layer_settings({"resources": [bad]}, "123456789012", "dev")

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

    def capacity(self, live, owned=("subnet-0own1", "subnet-0own2"), zones=None, requested=None):
        # Terraform-owned private subnets plus whatever the live cluster reports.
        platform = {"resources": [resource("aws_subnet", "private", {"id": i}, "module.networking") for i in owned]}
        cluster = {"resourcesVpcConfig": {"subnetIds": list(live)}}
        zones = zones or {"subnet-0extra1": "us-east-1a", "subnet-0extra2": "us-east-1b"}

        def aws(*args):
            if args[:2] == ("ec2", "describe-subnets"):
                asked = args[args.index("--subnet-ids") + 1:]
                return {"Subnets": [{"SubnetId": s, "AvailabilityZone": zones[s]} for s in asked if s in zones]}
            self.fail(f"Unexpected AWS access: {args[:2]}")

        with patch.object(state, "aws", aws):
            return state.retain_capacity_subnets(platform, cluster, requested)

    def test_additional_capacity_subnets_survive_a_routine_update(self):
        # The exhaustion fix is account-specific, so it lives only in live state.
        # Losing it here would shrink the subnet set and re-break pod scheduling.
        result = self.capacity(["subnet-0own1", "subnet-0own2", "subnet-0extra1", "subnet-0extra2"])
        self.assertEqual(result, {"us-east-1a": "subnet-0extra1", "us-east-1b": "subnet-0extra2"})

    def test_cluster_without_additions_exports_no_additional_subnets(self):
        # Every un-opted-in environment must keep planning the set it has today,
        # and must not pay for a describe-subnets call it has no reason to make.
        self.assertEqual(self.capacity(["subnet-0own1", "subnet-0own2"]), {})

    def test_terraform_owned_subnets_are_not_reinterpreted_as_additions(self):
        # They already arrive via private_subnet_ids. Pinning their ids here would
        # double-list them and freeze ids Terraform may legitimately replace.
        result = self.capacity(["subnet-0own1", "subnet-0own2", "subnet-0extra1"],
                               zones={"subnet-0extra1": "us-east-1a"})
        self.assertEqual(result, {"us-east-1a": "subnet-0extra1"})

    def test_subnet_added_during_the_update_run_is_not_dropped_by_the_export(self):
        # The exported tfvars is applied as a -var-file after the repository
        # overlays, so it overrides TF_VAR_ and must carry the operator's request.
        # The request declares the live addition too, which is what an operator
        # extending capacity supplies; omitting it is the refusal case below.
        result = self.capacity(["subnet-0own1", "subnet-0extra1"],
                               zones={"subnet-0extra1": "us-east-1a"},
                               requested={"us-east-1a": "subnet-0extra1",
                                          "us-east-1b": "subnet-0extra9"})
        self.assertEqual(result, {"us-east-1a": "subnet-0extra1", "us-east-1b": "subnet-0extra9"})

    def test_an_export_that_omits_a_live_addition_refuses(self):
        # Fail closed: applying it would remove the subnet from the live cluster and
        # re-break pod scheduling. A stale export is likelier than a deliberate
        # decision to shrink capacity during a routine update.
        with self.assertRaisesRegex(state.capacity_subnets.Refused, "subnet-0extra1"):
            self.capacity(["subnet-0own1", "subnet-0extra1"],
                          zones={"subnet-0extra1": "us-east-1a"},
                          requested={"us-east-1b": "subnet-0extra9"})

    def test_unresolvable_or_conflicting_additional_subnets_refuse(self):
        # Silently dropping either case would shrink the live subnet set.
        with self.assertRaisesRegex(state.capacity_subnets.Refused, "availability zone"):
            self.capacity(["subnet-0extra1", "subnet-0vanished"], zones={"subnet-0extra1": "us-east-1a"})
        with self.assertRaisesRegex(state.capacity_subnets.Refused, "availability zone"):
            self.capacity(["subnet-0extra1", "subnet-0extra2"],
                          zones={"subnet-0extra1": "us-east-1a", "subnet-0extra2": "us-east-1a"})
        # The same subnet claimed under two zones: a subnet lives in exactly one.
        with self.assertRaisesRegex(state.capacity_subnets.Refused, "more than one"):
            self.capacity(["subnet-0extra1", "subnet-0extra2"],
                          zones={"subnet-0extra1": "us-east-1a", "subnet-0extra2": "us-east-1b"},
                          requested={"us-east-1a": "subnet-0extra1", "us-east-1b": "subnet-0extra2",
                                     "us-east-1c": "subnet-0extra1"})

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


class CapacitySubnetExportTests(unittest.TestCase):
    """The retained subnets have to reach the file the update apply actually reads."""
    account = "111122223333"
    region = "us-east-1"

    def exported(self, live, requested=None):
        platform = {"outputs": {"eks_cluster_name": {"value": "adp-test-eks-cluster"}},
                    "resources": [resource("aws_eks_cluster", "main", {"name": "adp-test-eks-cluster"}),
                                  resource("aws_subnet", "private", {"id": "subnet-0own1"}, "module.networking")]}
        key = "test/platform/terraform.tfstate"

        def aws(*args):
            if args[:2] == ("sts", "get-caller-identity"):
                return {"Account": self.account}
            if args[:2] == ("s3api", "list-objects-v2"):
                return {"Contents": [{"Key": key}]}
            if args[:2] == ("s3api", "get-object"):
                Path(args[-1]).write_text(json.dumps(platform))
                return {}
            if args[:2] == ("eks", "describe-cluster"):
                return {"cluster": {"status": "ACTIVE", "resourcesVpcConfig": {
                    "endpointPublicAccess": False, "endpointPrivateAccess": True,
                    "publicAccessCidrs": [], "subnetIds": list(live)}}}
            if args[:2] == ("ec2", "describe-subnets"):
                asked = args[args.index("--subnet-ids") + 1:]
                return {"Subnets": [{"SubnetId": s, "AvailabilityZone": "us-east-1b"} for s in asked]}
            self.fail(f"Unexpected AWS access: {args[:2]}")

        environ = {} if requested is None else {"TF_VAR_additional_private_subnet_ids_by_az": json.dumps(requested)}
        with tempfile.TemporaryDirectory() as directory, patch.object(state, "aws", aws), \
                patch.dict(os.environ, environ, clear=False), \
                patch.object(state, "integration_snapshot", return_value={"secrets": {}, "mappings": {}}):
            state.prepare(SimpleNamespace(directory=directory, account=self.account,
                                          region=self.region, environment="test"))
            return json.loads((Path(directory) / "platform.tfvars.json").read_text())

    def test_update_run_exports_the_live_additional_capacity_subnets(self):
        # platform.tfvars.json is appended as a -var-file after the repository
        # overlays, so what lands here is what the update actually applies.
        exported = self.exported(["subnet-0own1", "subnet-0extra1"])
        self.assertEqual(exported["additional_private_subnet_ids_by_az"], {"us-east-1b": "subnet-0extra1"})

    def test_update_run_without_additions_exports_an_empty_map(self):
        exported = self.exported(["subnet-0own1"])
        self.assertEqual(exported["additional_private_subnet_ids_by_az"], {})

    def test_operator_supplied_subnet_is_carried_into_the_exported_inputs(self):
        exported = self.exported(["subnet-0own1"], requested={"us-east-1b": "subnet-0extra7"})
        self.assertEqual(exported["additional_private_subnet_ids_by_az"], {"us-east-1b": "subnet-0extra7"})


class EnginePreservationTests(unittest.TestCase):
    account = "111122223333"
    region = "eu-west-1"
    queue = "https://sqs.eu-west-1.amazonaws.com/111122223333/customer-submit.fifo"
    scope = "arn:aws:secretsmanager:eu-west-1:111122223333:secret:adp/test/tenants/*"

    def states(self):
        gateway = {"resources": [
            resource("aws_lambda_function", "tick", {"environment": [{"variables": {
                "BG_ORCH_DISPATCH_QUEUE_URL": self.queue, "WEBHOOK_EVENTS_TABLE": "customer-events"}}]}, "module.orchestration_tick[0]"),
            resource("aws_iam_role_policy", "tick", {"policy": json.dumps({"Statement": [
                {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"], "Resource": [self.scope]}]})}, "module.orchestration_tick[0]")]}
        webhook = {"resources": [
            resource("aws_sqs_queue", "agent_submit", {"url": self.queue,
                "arn": "arn:aws:sqs:eu-west-1:111122223333:customer-submit.fifo"}),
            resource("aws_dynamodb_table", "webhook_events", {"name": "customer-events",
                "arn": "arn:aws:dynamodb:eu-west-1:111122223333:table/customer-events",
                "server_side_encryption": [{"kms_key_arn": "arn:aws:kms:eu-west-1:111122223333:key/existing"}]})]}
        return gateway, webhook

    def test_prepare_keeps_live_dispatch_and_command_bridge_in_exported_gateway_inputs(self):
        gateway, webhook = self.states()
        platform = {"outputs": {"eks_cluster_name": {"value": "adp-test-eks-cluster"}},
                    "resources": [resource("aws_eks_cluster", "main", {"name": "adp-test-eks-cluster"})]}
        states = {"test/platform/terraform.tfstate": platform,
                  "test/modules/gateway/terraform.tfstate": gateway,
                  "test/modules/webhook-ingress/terraform.tfstate": webhook}
        def aws(*args):
            if args[:2] == ("sts", "get-caller-identity"):
                return {"Account": self.account}
            if args[:2] == ("s3api", "list-objects-v2"):
                return {"Contents": [{"Key": key} for key in states]}
            if args[:2] == ("s3api", "get-object"):
                Path(args[-1]).write_text(json.dumps(states[args[args.index("--key") + 1]]))
                return {}
            if args[:2] == ("eks", "describe-cluster"):
                return {"cluster": {"status": "ACTIVE", "resourcesVpcConfig": {
                    "endpointPublicAccess": False, "endpointPrivateAccess": True, "publicAccessCidrs": []}}}
            self.fail(f"Unexpected AWS access: {args[:2]}")
        with tempfile.TemporaryDirectory() as directory, patch.object(state, "aws", aws), \
                patch.object(state, "integration_snapshot", return_value={"secrets": {}, "mappings": {}}):
            state.prepare(SimpleNamespace(directory=directory, account=self.account, region=self.region, environment="test"))
            exported = json.loads((Path(directory) / "gateway.tfvars.json").read_text())
        self.assertEqual(exported["orchestration_dispatch_queue_url"], self.queue)
        self.assertEqual(exported["orchestration_dispatch_queue_arn"], webhook["resources"][0]["instances"][0]["attributes"]["arn"])
        self.assertEqual(exported["orchestration_webhook_events_table"], "customer-events")
        self.assertEqual(exported["orchestration_webhook_events_kms_key_arn"], "arn:aws:kms:eu-west-1:111122223333:key/existing")
        self.assertEqual(exported["orchestration_github_app_secret_arn_pattern"], self.scope)
        self.assertNotIn("orchestration_agent_authority_enabled", exported)

    def test_unwired_tick_does_not_gain_optional_integration(self):
        gateway, webhook = self.states()
        gateway["resources"][0]["instances"][0]["attributes"]["environment"][0]["variables"] = {}
        gateway["resources"].pop()
        self.assertEqual(state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test"), {})
        self.assertEqual(state.gateway_engine_settings({}, webhook, self.account, self.region, "test"), {})

    def test_missing_ambiguous_or_foreign_resource_refuses_instead_of_clearing_selectors(self):
        for mutation in (lambda w: w["resources"].clear(),
                         lambda w: w["resources"].append(copy.deepcopy(w["resources"][0])),
                         lambda w: w["resources"][0]["instances"][0]["attributes"].update(arn="arn:aws:sqs:eu-west-1:999999999999:queue")):
            gateway, webhook = self.states()
            mutation(webhook)
            with self.assertRaises(ValueError):
                state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")

    def test_missing_encryption_or_secret_scope_policy_refuses(self):
        gateway, webhook = self.states()
        webhook["resources"][1]["instances"][0]["attributes"]["server_side_encryption"] = []
        with self.assertRaisesRegex(ValueError, "encryption"):
            state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")
        gateway, webhook = self.states()
        gateway["resources"].pop()
        with self.assertRaisesRegex(ValueError, "tenant-secret scope"):
            state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")

    def test_wired_table_without_ack_grant_preserves_table_without_enabling_ack(self):
        gateway, webhook = self.states()
        gateway["resources"][1]["instances"][0]["attributes"]["policy"] = json.dumps({"Statement": []})
        result = state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")
        self.assertEqual(result["orchestration_webhook_events_table"], "customer-events")
        self.assertEqual(result["orchestration_webhook_events_kms_key_arn"], "arn:aws:kms:eu-west-1:111122223333:key/existing")
        self.assertNotIn("orchestration_github_app_secret_arn_pattern", result)
        self.assertFalse(any(key.endswith("enabled") for key in result))

    def test_narrow_existing_ack_scope_is_preserved_exactly(self):
        self.scope = self.scope.removesuffix("*") + "customer/github-app-*"
        gateway, webhook = self.states()
        result = state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")
        self.assertEqual(result["orchestration_github_app_secret_arn_pattern"], self.scope)

    def test_existing_ack_scope_is_retained_independently_of_table_and_queue(self):
        for variables in ({"BG_ORCH_DISPATCH_QUEUE_URL": self.queue}, {}):
            with self.subTest(variables=variables):
                gateway, webhook = self.states()
                gateway["resources"][0]["instances"][0]["attributes"]["environment"][0]["variables"] = variables
                result = state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")
                self.assertEqual(result["orchestration_github_app_secret_arn_pattern"], self.scope)
                self.assertNotIn("orchestration_webhook_events_table", result)
                self.assertFalse(any(key.endswith("enabled") for key in result))

    def test_null_environment_variables_remain_unwired(self):
        gateway, webhook = self.states()
        gateway["resources"][0]["instances"][0]["attributes"]["environment"][0]["variables"] = None
        gateway["resources"].pop()
        self.assertEqual(state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test"), {})

    def test_unrepresentable_or_foreign_ack_grants_refuse(self):
        for change in ({"Resource": "*"},
                       {"Resource": self.scope.replace("111122223333", "999999999999")},
                       {"Resource": self.scope.replace("/test/", "/prod/")},
                       {"Resource": [self.scope, self.scope + "/github-app-*"]},
                       {"Resource": []},
                       {"Action": "SecretsManager:GetSecretValue"},
                       {"Action": "*:GetSecretValue"},
                       {"Condition": {"StringEquals": {"aws:PrincipalTag/tenant": "customer"}}},
                       {"Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]}):
            with self.subTest(change=change):
                gateway, webhook = self.states()
                policy = gateway["resources"][1]["instances"][0]["attributes"]
                document = json.loads(policy["policy"])
                document["Statement"][0].update(change)
                policy["policy"] = json.dumps(document)
                with self.assertRaisesRegex(ValueError, "tenant-secret scope"):
                    state.gateway_engine_settings(gateway, webhook, self.account, self.region, "test")


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
