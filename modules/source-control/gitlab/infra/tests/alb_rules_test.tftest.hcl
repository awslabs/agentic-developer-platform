# Issue #3593: Verify that the http_api_forward listener rule has been removed
# and the HTTP:80 listener default action forwards to the target group.
#
# Plan-only test with mocked providers — asserts the ALB listener configuration
# is correct after removing the /api/* bypass rule.

mock_provider "aws" {}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  gitlab_domain        = "gitlab.dev.adp.internal"
  route53_zone_name    = "dev.adp.internal"
  certificate_arn      = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
  cognito_user_pool_id = "us-east-1_TestPool"
  cognito_domain       = "adp-dev-auth"
}

run "http_listener_default_action_is_forward" {
  command = plan

  override_data {
    target = data.aws_caller_identity.current
    values = {
      account_id = "123456789012"
    }
  }

  override_data {
    target = data.terraform_remote_state.platform
    values = {
      outputs = {
        vpc_id             = "vpc-00000000000000000"
        vpc_cidr_block     = "10.0.0.0/16"
        private_subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"]
      }
    }
  }

  # HTTP:80 listener default action must be "forward" (not "redirect")
  assert {
    condition     = aws_lb_listener.http.default_action[0].type == "forward"
    error_message = "HTTP:80 listener default action must be 'forward', not 'redirect'"
  }

  # HTTP:80 listener forwards to the GitLab target group
  assert {
    condition     = aws_lb_listener.http.default_action[0].target_group_arn == aws_lb_target_group.gitlab.arn
    error_message = "HTTP:80 listener must forward to the GitLab target group"
  }
}
