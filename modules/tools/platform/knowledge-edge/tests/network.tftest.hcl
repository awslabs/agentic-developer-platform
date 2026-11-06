mock_provider "aws" {
  mock_resource "aws_api_gateway_rest_api" {
    defaults = {
      id            = "fixtureapi"
      execution_arn = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi"
    }
  }
  mock_resource "aws_vpc_endpoint" {
    defaults = {
      id = "vpce-00000000000000003"
      dns_entry = [
        { dns_name = "vpce-00000000000000003-fixture-us-east-1a.execute-api.us-east-1.vpce.amazonaws.com", hosted_zone_id = "FIXTURE" },
        { dns_name = "vpce-00000000000000003-fixture.execute-api.us-east-1.vpce.amazonaws.com", hosted_zone_id = "FIXTURE" }
      ]
    }
  }
}
mock_provider "random" {}
mock_provider "external" {
  mock_data "external" {
    defaults = { result = { prepared = "false", document = "{}", api_policy = "{}", endpoint_policy = "{}", deployment_id = "", api_id = "" } }
  }
}

override_data {
  target = data.aws_subnet.knowledge["subnet-00000000000000001"]
  values = { vpc_id = "vpc-00000000000000001", availability_zone_id = "use1-az1", map_public_ip_on_launch = false }
}
override_data {
  target = data.aws_subnet.knowledge["subnet-00000000000000002"]
  values = { vpc_id = "vpc-00000000000000001", availability_zone_id = "use1-az2", map_public_ip_on_launch = false }
}
override_resource {
  target = aws_security_group.endpoint[0]
  values = { id = "sg-00000000000000001" }
}
override_resource {
  target = aws_security_group.resource_gateway[0]
  values = { id = "sg-00000000000000002" }
}

variables {
  enabled                  = true
  name_prefix              = "fixture-adp"
  stage                    = "dev"
  region                   = "us-east-1"
  gateway_role_arn         = "arn:aws:iam::123456789012:role/fixture-tools"
  gateway_vpc_link_id      = "fixture-link"
  gateway_internal_alb_dns = "internal-fixture-0000000000.us-east-1.elb.amazonaws.com"
  gateway_internal_alb_arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/fixture/0000000000000000"
  gateway_edge_provenance  = "fixture-proof-000000000000000000000000000000000000000000000000"
  managed_network = {
    vpc_id     = "vpc-00000000000000001"
    subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
  }
}

run "network_stays_dormant_until_edge_enabled" {
  command = plan
  variables { enabled = false }
  assert {
    condition = (
      length(aws_vpc_endpoint.knowledge) == 0 && length(data.aws_subnet.knowledge) == 0 &&
      length(aws_security_group.endpoint) == 0 && length(aws_security_group.resource_gateway) == 0 &&
      length(aws_vpc_endpoint_policy.knowledge) == 0 && output.managed_private_endpoint == null
    )
    error_message = "Supplying optional network inputs must not enable infrastructure by itself."
  }
}

run "route_only_knowledge_without_shadowing_regional_api_dns" {
  command = apply
  assert {
    condition = (
      !aws_vpc_endpoint.knowledge[0].private_dns_enabled &&
      aws_vpc_endpoint.knowledge[0].service_name == "com.amazonaws.us-east-1.execute-api" &&
      output.managed_private_endpoint.routing_domain == "vpce-00000000000000003-fixture.execute-api.us-east-1.vpce.amazonaws.com" &&
      output.mediator_url == "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev/internal/v1/tools/knowledge/mcp" &&
      toset(aws_api_gateway_rest_api.knowledge[0].endpoint_configuration[0].vpc_endpoint_ids) == toset(["vpce-00000000000000003"])
    )
    error_message = "Preserve the API's Host/TLS identity and route only this target through the regional endpoint-specific name."
  }
  assert {
    condition = (
      aws_vpc_security_group_ingress_rule.endpoint[0].security_group_id == "sg-00000000000000001" &&
      aws_vpc_security_group_ingress_rule.endpoint[0].referenced_security_group_id == "sg-00000000000000002" &&
      aws_vpc_security_group_egress_rule.resource_gateway[0].security_group_id == "sg-00000000000000002" &&
      aws_vpc_security_group_egress_rule.resource_gateway[0].referenced_security_group_id == "sg-00000000000000001" &&
      aws_vpc_security_group_ingress_rule.endpoint[0].from_port == 443 &&
      aws_vpc_security_group_ingress_rule.endpoint[0].to_port == 443 &&
      aws_vpc_security_group_egress_rule.resource_gateway[0].from_port == 443 &&
      aws_vpc_security_group_egress_rule.resource_gateway[0].to_port == 443 &&
      output.managed_private_endpoint.security_group_ids == ["sg-00000000000000002"]
    )
    error_message = "Only the dedicated resource gateway may connect, over HTTPS, to its endpoint."
  }
  assert {
    condition = jsondecode(aws_vpc_endpoint_policy.knowledge[0].policy).Statement == [{
      Effect   = "Allow", Principal = { AWS = var.gateway_role_arn }, Action = "execute-api:Invoke",
      Resource = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev/POST/internal/v1/tools/knowledge/mcp"
    }]
    error_message = "The endpoint policy must bind the exact role, API, stage, method and route."
  }
}

run "refuse_conflicting_endpoint_ownership" {
  command = plan
  variables { execute_api_vpc_endpoint_id = "vpce-00000000000000004" }
  expect_failures = [var.managed_network]
}

run "retain_prepared_endpoint_permissions_during_ordinary_apply" {
  command = apply
  override_data {
    target = data.external.retained_handoff[0]
    values = { result = {
      prepared        = "true", api_id = "fixtureapi", deployment_id = "selected-baseline",
      document        = "{\"swagger\":\"2.0\",\"paths\":{}}", api_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[]}",
      endpoint_policy = "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Sid\":\"ADPAdapterPreparedConnectors\",\"Effect\":\"Allow\",\"Action\":\"execute-api:Invoke\",\"Resource\":\"arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev/POST/internal/v1/tools/jira/mcp\"}]}"
    } }
  }
  assert {
    condition = (
      aws_api_gateway_stage.knowledge[0].deployment_id == "selected-baseline" &&
      aws_vpc_endpoint_policy.knowledge[0].policy == data.external.retained_handoff[0].result.endpoint_policy &&
      !aws_vpc_endpoint.knowledge[0].private_dns_enabled
    )
    error_message = "An ordinary apply must preserve the observed connector endpoint permissions and selected baseline without shadowing public APIs."
  }
}

run "refuse_duplicate_subnet" {
  command = plan
  variables {
    managed_network = {
      vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000001"]
    }
  }
  expect_failures = [var.managed_network]
}

run "refuse_foreign_vpc_subnet" {
  command = plan
  override_data {
    target = data.aws_subnet.knowledge["subnet-00000000000000002"]
    values = { vpc_id = "vpc-00000000000000009", availability_zone_id = "use1-az2", map_public_ip_on_launch = false }
  }
  expect_failures = [aws_vpc_endpoint.knowledge]
}

run "refuse_single_availability_zone" {
  command = plan
  override_data {
    target = data.aws_subnet.knowledge["subnet-00000000000000002"]
    values = { vpc_id = "vpc-00000000000000001", availability_zone_id = "use1-az1", map_public_ip_on_launch = false }
  }
  expect_failures = [aws_vpc_endpoint.knowledge]
}

run "refuse_public_ip_subnet" {
  command = plan
  override_data {
    target = data.aws_subnet.knowledge["subnet-00000000000000002"]
    values = { vpc_id = "vpc-00000000000000001", availability_zone_id = "use1-az2", map_public_ip_on_launch = true }
  }
  expect_failures = [aws_vpc_endpoint.knowledge]
}

run "refuse_missing_regional_dns" {
  command   = apply
  state_key = "missing-regional-dns"
  override_resource {
    target = aws_vpc_endpoint.knowledge[0]
    values = {
      id        = "vpce-00000000000000003"
      dns_entry = [{ dns_name = "vpce-00000000000000003-fixture-us-east-1a.execute-api.us-east-1.vpce.amazonaws.com", hosted_zone_id = "FIXTURE" }]
    }
  }
  expect_failures = [output.managed_private_endpoint]
}
