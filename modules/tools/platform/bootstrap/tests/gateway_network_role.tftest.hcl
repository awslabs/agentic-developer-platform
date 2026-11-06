mock_provider "aws" {}

variables {
  aws_account_id        = "123456789012"
  tools_facade_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn           = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/example"
}

run "network_role_is_default_off" {
  command = plan
  assert {
    condition     = length(aws_iam_service_linked_role.gateway_network) == 0
    error_message = "Existing bootstrap owners must not silently adopt the shared service-linked role."
  }
}

run "owner_explicitly_manages_gateway_network_service_role" {
  command = plan
  variables { enable_gateway_network_service_linked_role = true }
  assert {
    condition = (
      length(aws_iam_service_linked_role.gateway_network) == 1 &&
      aws_iam_service_linked_role.gateway_network[0].aws_service_name == "bedrock-agentcore.amazonaws.com" &&
      aws_iam_service_linked_role.gateway_network[0].custom_suffix == null &&
      var.knowledge_door_endpoint == ""
    )
    error_message = "The foundation owner must provision the exact account-wide service-linked role independently of target enablement."
  }
}
