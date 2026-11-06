"""Guard optional OAuth provider registration and the no-secret-in-state contract."""

import re
import unittest
from pathlib import Path


INFRA = Path(__file__).resolve().parents[1] / "infra"


class OAuthProviderTests(unittest.TestCase):
    def test_identity_providers_use_secret_references_and_write_only_ids(self):
        source = (INFRA / "oauth-providers.tf").read_text()
        for name, vendor in (("github", "GithubOauth2"), ("atlassian", "AtlassianOauth2"), ("slack", "SlackOauth2")):
            if name != "slack":
                self.assertRegex(source, rf"\b{name}\s+=\s+\"{vendor}\"")
            else:
                self.assertIn('var.slack_user_oauth ? "CustomOauth2" : "SlackOauth2"', source)
            self.assertIn(f'{name}_oauth2_provider_config', source)
        self.assertIn('ephemeral "aws_secretsmanager_secret_version"', source)
        self.assertEqual(source.count("client_id_wo "), 4)
        self.assertEqual(source.count('client_secret_source          = "EXTERNAL"'), 4)
        self.assertEqual(source.count('json_key  = "client_secret"'), 4)
        self.assertNotIn("client_secret_wo", source)
        self.assertIn('for_each                   = var.oauth_provider_secret_ids', source)

    def test_optional_provider_setup_accepts_independent_apps(self):
        source = (INFRA / "variables.tf").read_text()
        self.assertIn('alltrue([for provider in keys(var.oauth_provider_secret_ids) : contains(["github", "atlassian", "slack"], provider)])', source)
        self.assertIn('variable "oauth_provider_credentials_version"', source)
        self.assertIn('variable "oauth_return_url"', source)
        self.assertIn('(api/)?auth/connections/oauth/return', source)
        workload = (INFRA / "oauth-providers.tf").read_text()
        self.assertIn('resource "aws_bedrockagentcore_workload_identity" "oauth_user"', workload)
        self.assertIn('allowed_resource_oauth2_return_urls = [var.oauth_return_url]', workload)


    def test_vendor_targets_are_optional_and_use_identity_oauth(self):
        target = (INFRA / "main.tf").read_text().split('resource "aws_bedrockagentcore_gateway_target" "user_connections" {', 1)[1].split('\nresource ', 1)[0]
        self.assertIn('vendor != "github" && vendor != "atlassian"', target)
        self.assertIn('credential_provider_arn', target)
        self.assertIn('grant_type         = "AUTHORIZATION_CODE"', target)
        self.assertIn('scopes             = local.oauth_target_scopes[each.key]', target)
        self.assertIn('var.interceptor_lambda_arn != ""', target)
        self.assertIn('var.assertion_signing_key_arn != ""', target)
        self.assertIn('open_api_schema {', target)
        self.assertIn('aws_bedrockagentcore_gateway_target.user_connections', (INFRA / "outputs.tf").read_text())
        mediated = (INFRA / "main.tf").read_text().split('resource "aws_bedrockagentcore_gateway_target" "github_user" {', 1)[1].split('\nresource ', 1)[0]
        self.assertIn('gateway_iam_role { service = "execute-api" }', mediated)
        self.assertIn('endpoint     = var.github_mediator_endpoint', mediated)
        self.assertIn('private_endpoint {', mediated)
        for name, operations in (("github", {"get_user"}), ("jira", {"get_issue"}), ("slack", {"get_user", "post_message"})):
            schema = (INFRA.parent / "schemas" / f"{name}.yaml").read_text()
            self.assertIn("openapi: 3.0.3", schema)
            self.assertRegex(schema, r"url: https://[a-z.]+")
            self.assertEqual(set(re.findall(r"operationId: ([a-z_]+)", schema)), operations)

    def test_slack_private_reader_replaces_legacy_target_only_when_configured(self):
        source = (INFRA / "main.tf").read_text()
        variables = (INFRA / "variables.tf").read_text()
        outputs = (INFRA / "outputs.tf").read_text()
        target = source.split('resource "aws_bedrockagentcore_gateway_target" "slack_user" {', 1)[1].split('\nresource ', 1)[0]
        self.assertIn('vendor != "slack" || var.slack_mediator_endpoint == ""', source)
        self.assertIn('count              = var.slack_mediator_endpoint == "" ? 0 : 1', target)
        self.assertIn('gateway_iam_role { service = "execute-api" }', target)
        self.assertIn('endpoint     = var.slack_mediator_endpoint', target)
        self.assertIn('private_endpoint {', target)
        self.assertIn('variable "slack_mediator_endpoint"', variables)
        self.assertIn('/internal/v1/tools/slack/mcp$', variables)
        self.assertIn('aws_bedrockagentcore_gateway_target.slack_user[0].target_id', outputs)

    def test_gateway_oauth_policy_references_cross_the_bootstrap_boundary(self):
        outputs = (INFRA / "outputs.tf").read_text().split('output "gateway_oauth" {', 1)[1]
        self.assertIn('length(var.oauth_provider_secret_ids) == 0 ? null', outputs)
        self.assertIn('aws_bedrockagentcore_gateway.tools.workload_identity_details[0].workload_identity_arn', outputs)
        self.assertIn('aws_bedrockagentcore_oauth2_credential_provider.user_connection', outputs)
        self.assertIn('provider.credential_provider_arn', outputs)
        self.assertIn('var.oauth_provider_secret_ids[vendor]', outputs)
        bootstrap = (INFRA.parent / "bootstrap" / "oauth.tf").read_text()
        self.assertIn('variable "gateway_oauth"', bootstrap)
        self.assertIn('data "aws_secretsmanager_secret" "gateway_oauth"', bootstrap)
        self.assertNotIn('secret_version', bootstrap)
        self.assertNotIn('secret_string', outputs + bootstrap)

    def test_public_return_route_matches_gateway_configuration(self):
        configmap = (INFRA.parents[2] / "gateway" / "k8s" / "configmap.yaml").read_text()
        variables = (INFRA / "variables.tf").read_text()
        self.assertIn('BG_OAUTH_RETURN_URL: "__GATEWAY_BASE_URL__/api/auth/connections/oauth/return"', configmap)
        self.assertIn('(api/)?auth/connections/oauth/return', variables)

if __name__ == "__main__":
    unittest.main()
