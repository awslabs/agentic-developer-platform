mock_provider "aws" {
  mock_resource "aws_api_gateway_rest_api" {
    defaults = {
      id            = "fixtureapi"
      execution_arn = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi"
    }
  }
}
mock_provider "random" {}
mock_provider "external" {
  mock_data "external" {
    defaults = { result = { prepared = "false", document = "{}", api_policy = "{}", endpoint_policy = "{}", deployment_id = "", api_id = "" } }
  }
}

run "retain_selected_connector_deployment_during_ordinary_apply" {
  command = apply
  variables { enabled = true }
  override_data {
    target = data.external.retained_handoff[0]
    values = { result = {
      prepared   = "true", api_id = "fixtureapi", deployment_id = "selected-adapter",
      document   = "{\"swagger\":\"2.0\",\"paths\":{\"/internal/v1/tools/atlassian/mcp\":{}}}",
      api_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[]}", endpoint_policy = "{}"
    } }
  }
  assert {
    condition = (
      aws_api_gateway_stage.knowledge[0].deployment_id == "selected-adapter" &&
      contains(keys(jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths), "/internal/v1/tools/atlassian/mcp") &&
      aws_api_gateway_rest_api_policy.knowledge[0].policy == data.external.retained_handoff[0].result.api_policy
    )
    error_message = "Ordinary Terraform apply must preserve independently read-back connector routes, policy and selected deployment."
  }
}

variables {
  name_prefix                 = "fixture-adp"
  stage                       = "dev"
  region                      = "us-east-1"
  gateway_role_arn            = "arn:aws:iam::123456789012:role/fixture-tools"
  execute_api_vpc_endpoint_id = "vpce-00000000000000000"
  gateway_vpc_link_id         = "fixture-link"
  gateway_internal_alb_dns    = "internal-fixture-0000000000.us-east-1.elb.amazonaws.com"
  gateway_internal_alb_arn    = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/fixture/0000000000000000"
  gateway_edge_provenance     = "fixture-proof-000000000000000000000000000000000000000000000000"
}

run "dormant_default_creates_nothing" {
  command = plan
  assert {
    condition = (
      length(aws_api_gateway_rest_api.knowledge) == 0 && length(aws_iam_role_policy.invoke) == 0 &&
      length(random_password.private_provenance) == 0 && output.mediator_url == "" && length(output.oauth_mediator_urls) == 0
    )
    error_message = "The private edge must be explicitly enabled after review."
  }
}

run "private_route_requires_exact_role_vpce_and_independent_proof" {
  command = apply
  variables { enabled = true }
  assert {
    condition = (
      aws_api_gateway_rest_api.knowledge[0].endpoint_configuration[0].types == tolist(["PRIVATE"]) &&
      jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths["/internal/v1/tools/knowledge/mcp"].post["x-amazon-apigateway-auth"].type == "AWS_IAM" &&
      jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths["/internal/v1/tools/knowledge/mcp"].post["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Caller-Identity"] == "context.identity.userArn" &&
      jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths["/internal/v1/tools/knowledge/mcp"].post["x-amazon-apigateway-integration"].requestParameters["integration.request.header.X-Adp-Knowledge-Edge-Provenance"] == "'${random_password.private_provenance[0].result}'"
    )
    error_message = "Only the private IAM integration may write caller and private-edge provenance."
  }
  assert {
    condition = (
      jsondecode(aws_api_gateway_rest_api_policy.knowledge[0].policy).Statement[0].Condition.StringNotEquals["aws:SourceVpce"] == var.execute_api_vpc_endpoint_id &&
      jsondecode(aws_api_gateway_rest_api_policy.knowledge[0].policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == var.gateway_role_arn &&
      jsondecode(aws_iam_role_policy.invoke[0].policy).Statement[0].Resource == ["arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev/POST/internal/v1/tools/knowledge/mcp"]
    )
    error_message = "IAM must grant only the exact private POST route and deny other roles/endpoints."
  }
}

run "vendor_routes_share_exact_private_boundary" {
  command = plan
  variables {
    enabled                = true
    oauth_mediator_vendors = ["github", "atlassian", "slack"]
  }
  assert {
    condition = (
      length(jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths) == 4 &&
      alltrue([for vendor in var.oauth_mediator_vendors :
        jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths["/internal/v1/tools/${vendor == "atlassian" ? "jira" : vendor}/mcp"].post["x-amazon-apigateway-auth"].type == "AWS_IAM" &&
        jsondecode(aws_api_gateway_rest_api.knowledge[0].body).paths["/internal/v1/tools/${vendor == "atlassian" ? "jira" : vendor}/mcp"].post["x-amazon-apigateway-integration"].uri == "http://${var.gateway_internal_alb_dns}/internal/v1/tools/${vendor == "atlassian" ? "jira" : vendor}/mcp" &&
        contains(jsondecode(aws_iam_role_policy.invoke[0].policy).Statement[0].Resource, "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev/POST/internal/v1/tools/${vendor == "atlassian" ? "jira" : vendor}/mcp")
      ]) &&
      length(output.oauth_mediator_urls) == 3
    )
    error_message = "Every vendor route must use the existing private integration and exact IAM POST grant."
  }
}

run "reject_unknown_vendor_route" {
  command = plan
  variables { oauth_mediator_vendors = ["other"] }
  expect_failures = [var.oauth_mediator_vendors]
}
