# IMDSv2 regression — #6103 (CKV_AWS_79).
#
# The gVisor node launch template must require IMDSv2 with a single-hop limit.
# Without this, ordinary (non-hostNetwork) pods on gVisor nodes could reach the
# instance metadata service via routed traffic and obtain the node role's
# credentials. Hop-limit 1 constrains that routed path; hostNetwork and other
# host-level callers require separate admission control. This test prevents a
# future edit from removing the metadata_options block.

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}
mock_provider "kubernetes" {}
mock_provider "tls" {}
mock_provider "time" {}

variables {
  environment = "dev"
}

run "gvisor_launch_template_requires_imdsv2" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:user/test-deployer"
    }
  }

  assert {
    condition     = aws_launch_template.gvisor_nodes.metadata_options[0].http_tokens == "required"
    error_message = "gVisor node launch template must require IMDSv2 (http_tokens = required)."
  }

  assert {
    condition     = aws_launch_template.gvisor_nodes.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "gVisor node launch template must set http_put_response_hop_limit = 1 to constrain routed pod IMDS access (hostNetwork requires separate admission control)."
  }

  assert {
    condition     = aws_launch_template.gvisor_nodes.metadata_options[0].http_endpoint == "enabled"
    error_message = "gVisor node launch template must keep the metadata endpoint enabled for node bootstrap."
  }
}
