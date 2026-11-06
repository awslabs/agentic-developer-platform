mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::000000000000:policy/adp-dev-l5-transport" }
  }
}

variables {
  environment       = "dev"
  aws_region        = "us-east-1"
  aws_account_id    = "000000000000"
  gateway_role_name = "example-gateway"
}

run "managed_transport" {
  command = apply
  assert {
    condition     = length(aws_iam_role_policy_attachment.gateway_transport) == 1 && length(aws_iam_role_policy.gateway_transport) == 0
    error_message = "Normal installations use exactly one managed attachment."
  }
}

run "inline_transport_at_attachment_quota" {
  command = plan
  variables {
    gateway_transport_mode = "inline"
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.gateway_transport) == 0 && length(aws_iam_role_policy.gateway_transport) == 1
    error_message = "The operator-selected inline form consumes no managed attachment."
  }
  assert {
    condition     = aws_iam_role_policy.gateway_transport[0].policy == aws_iam_policy.gateway_transport.policy
    error_message = "Attachment form must not alter transport authority."
  }
  assert {
    condition = length(jsondecode(aws_iam_policy.gateway_transport.policy).Statement) == 2 && alltrue([
      for s in jsondecode(aws_iam_policy.gateway_transport.policy).Statement : length(s.Resource) == 5 && alltrue([for r in s.Resource : !strcontains(r, "*")])
    ])
    error_message = "Transport permits only the five exact service queues and discovery parameters."
  }
}
