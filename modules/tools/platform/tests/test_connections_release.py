"""Real release validator/overlay with fictional private plans, never cloud authority."""

from copy import deepcopy
import json
import unittest

from modules.tools.platform.scripts.knowledge_overlay import FIELDS, merge_connections
from modules.tools.platform.scripts.release_plan import CONNECTION_TARGETS, Refused, validate
from modules.tools.platform.tests.release_plan_fixtures import ACCOUNT, BINDING, SELECTORS, existing_target_changes, gateway_change, knowledge_change


def bindings(*targets):
    return {"version": 1, "targets": {
        target: {**deepcopy(BINDING), "endpoint": BINDING["endpoint"].replace("/knowledge/", f"/{target}/"),
                 "provider_arn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:token-vault/default/oauth2credentialprovider/adp-tools-dev-{'atlassian' if target == 'jira' else target}-user"}
        for target in targets
    }}


def target_change(target):
    value = knowledge_change()
    value["address"] = CONNECTION_TARGETS[target]
    after = value["change"]["after"]
    after["name"] = target
    after["target_configuration"][0]["mcp"][0]["mcp_server"][0]["endpoint"] = bindings(target)["targets"][target]["endpoint"]
    return value


class ConnectionsReleaseTests(unittest.TestCase):
    def validate(self, targets, *, create=None, config=None, selectors=SELECTORS):
        validate({"resource_changes": [gateway_change(), *existing_target_changes(), *targets]},
                 account=ACCOUNT, region="us-east-1", environment="dev", release_selectors=json.dumps(selectors),
                 connections_binding=json.dumps(create) if create else "",
                 connections_config=json.dumps(config) if config else "")

    def test_all_three_explicit_private_targets_and_existing_targets_are_retained(self):
        selected = bindings("github", "jira", "slack")
        self.validate([target_change(target) for target in selected["targets"]], create=selected, config=selected)

    def test_each_new_target_requires_its_own_creation_and_durable_binding(self):
        for create, config in ((None, bindings("jira")), (bindings("jira"), None),
                               (bindings("github"), bindings("github", "jira"))):
            with self.subTest(create=create, config=config), self.assertRaises(Refused):
                self.validate([target_change("jira")], create=create, config=config)

    def test_exact_target_route_network_and_credential_authority_is_enforced(self):
        mutations = [
            lambda a: a.update(name="github"),
            lambda a: a.update(gateway_identifier="other"),
            lambda a: a["target_configuration"][0]["mcp"][0]["mcp_server"][0].update(endpoint=BINDING["endpoint"]),
            lambda a: a["target_configuration"][0]["mcp"][0]["mcp_server"][0].update(listing_mode="DEFAULT"),
            lambda a: a["credential_provider_configuration"][0].update(oauth=[{"provider_arn": "other"}]),
            lambda a: a["private_endpoint"][0]["managed_vpc_resource"][0].update(routing_domain="other.example.invalid"),
            lambda a: a.update(metadata_configuration=[{"allowed_request_headers": ["authorization"]}]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(Refused):
                target = target_change("jira")
                mutate(target["change"]["after"])
                self.validate([target], create=bindings("jira"), config=bindings("jira"))
        for field in ("name", "private_endpoint", "credential_provider_configuration", "target_configuration"):
            with self.subTest(unknown=field), self.assertRaises(Refused):
                target = target_change("jira")
                target["change"]["after_unknown"][field] = True
                self.validate([target], create=bindings("jira"), config=bindings("jira"))

    def test_wrong_provider_or_extra_target_cannot_be_authorized(self):
        for field, value in (("provider_arn", "foreign"), ("endpoint", BINDING["endpoint"]), ("extra", "invalid")):
            selected = bindings("jira")
            selected["targets"]["jira"][field] = value
            with self.subTest(field=field), self.assertRaises(Refused):
                self.validate([target_change("jira")], create=selected, config=selected)
        selected = bindings("jira")
        selected["targets"]["other"] = deepcopy(selected["targets"]["jira"])
        with self.assertRaises(Refused):
            self.validate([target_change("jira")], create=selected, config=selected)

    def test_existing_connection_updates_keep_exact_binding_and_identity_without_create(self):
        target = target_change("jira")
        target["change"]["after"]["target_id"] = "jira-existing"
        target["change"].update(actions=["update"], before=deepcopy(target["change"]["after"]), after_unknown={})
        target["change"]["after"]["description"] = "Updated description"
        selectors = deepcopy(SELECTORS)
        selectors["components"]["platform targets"]["target_ids"].append("jira-existing")
        self.validate([target], config=bindings("jira"), selectors=selectors)
        for field, value in (("target_id", "other"), ("name", "github")):
            wrong = deepcopy(target)
            wrong["change"]["after"][field] = value
            with self.subTest(field=field), self.assertRaises(Refused):
                self.validate([wrong], config=bindings("jira"), selectors=selectors)
        for action in (["create", "delete"], ["delete"], ["delete", "create"]):
            wrong = deepcopy(target)
            wrong["change"]["actions"] = action
            with self.subTest(action=action), self.assertRaises(Refused):
                self.validate([wrong], config=bindings("jira"), selectors=selectors)


class ConnectionsOverlayTests(unittest.TestCase):
    def setUp(self):
        self.inputs = {"aws_account_id": ACCOUNT, "environment": "dev", "aws_region": "us-east-1",
                       "oauth_provider_secret_ids": {}, "targets": [{"name": "retained"}],
                       **{field: deepcopy(BINDING[source]) for field, (source, _) in FIELDS.items()}}

    def merge(self, inputs=None, config=None, create=None):
        return merge_connections(self.inputs if inputs is None else inputs, json.dumps(config or bindings("github", "jira", "slack")),
                                 selectors=json.dumps(SELECTORS), account=ACCOUNT, region="us-east-1", environment="dev",
                                 first_create=json.dumps(create) if create else "")

    def test_only_selected_endpoints_and_external_references_are_added_idempotently(self):
        original = deepcopy(self.inputs)
        result = self.merge()
        self.assertEqual({key: result[key] for key in original}, original)
        self.assertEqual(self.inputs, original)
        self.assertEqual(set(result) - set(original), {"external_oauth_provider_arns", "github_mediator_endpoint", "jira_mediator_endpoint", "slack_mediator_endpoint"})
        self.assertEqual(set(result["external_oauth_provider_arns"]), {"github", "atlassian", "slack"})
        result["knowledge_door_subnet_ids"].reverse()
        self.assertEqual(self.merge(result), result)

    def test_absent_configuration_preserves_inputs_but_does_not_allow_first_create(self):
        kwargs = dict(selectors="", account="", region="", environment="")
        self.assertEqual(merge_connections(self.inputs, "", **kwargs), self.inputs)
        with self.assertRaises(Refused):
            merge_connections(self.inputs, "", first_create=json.dumps(bindings("jira")), **kwargs)

    def test_edge_cannot_be_created_reconfigured_or_shared_across_different_networks(self):
        for field in FIELDS:
            with self.subTest(field=field), self.assertRaises(Refused):
                self.merge({key: value for key, value in self.inputs.items() if key != field})
        for field, invalid in (("subnet_ids", ["subnet-00000000000000003", "subnet-00000000000000004"]),
                               ("endpoint", BINDING["endpoint"].replace("fixture.", "another.").replace("/knowledge/", "/jira/"))):
            config = bindings("jira")
            config["targets"]["jira"][field] = invalid
            with self.subTest(field=field), self.assertRaises(Refused):
                self.merge(config=config)

    def test_provider_ownership_and_existing_config_cannot_be_replaced(self):
        for fields in ({"oauth_provider_secret_ids": {"atlassian": "retained-secret"}},
                       {"external_oauth_provider_arns": {"atlassian": "retained-provider"}},
                       {"jira_mediator_endpoint": "https://other.example.invalid"}):
            with self.subTest(fields=fields), self.assertRaises(Refused):
                self.merge({**self.inputs, **fields})

    def test_creation_must_match_durable_config(self):
        create = bindings("jira")
        create["targets"]["jira"]["security_group_ids"] = ["sg-00000000000000002"]
        with self.assertRaises(Refused):
            self.merge(create=create)
        with self.assertRaises(Refused):
            self.merge(config=bindings("jira"), create=bindings("slack"))
