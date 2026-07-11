# Tests for GitLab VPC Origin conditional creation (Issue #3583)
#
# Validates:
# - VPC Origin and /gitlab/* behavior are NOT created when variables are empty (default)
# - VPC Origin and /gitlab/* behavior ARE created when both variables are populated

variables {
  environment                    = "dev"
  name_prefix                    = "adp-dev"
  s3_bucket_regional_domain_name = "adp-dev-frontend.s3.us-east-1.amazonaws.com"
  s3_bucket_id                   = "adp-dev-frontend"
}

# Test: empty defaults produce no GitLab resources
run "gitlab_origin_disabled_when_vars_empty" {
  command = plan

  variables {
    gitlab_origin_dns = ""
    gitlab_origin_arn = ""
  }

  assert {
    condition     = length(aws_cloudfront_vpc_origin.gitlab) == 0
    error_message = "GitLab VPC Origin should not be created when gitlab_origin_dns is empty."
  }
}

# Test: GitLab VPC Origin created when both variables set
run "gitlab_origin_enabled_when_vars_set" {
  command = plan

  variables {
    gitlab_origin_dns = "internal-gitlab-alb-123456.us-east-1.elb.amazonaws.com"
    gitlab_origin_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/gitlab-alb/abc123"
  }

  assert {
    condition     = length(aws_cloudfront_vpc_origin.gitlab) == 1
    error_message = "GitLab VPC Origin should be created when both gitlab_origin_dns and gitlab_origin_arn are set."
  }
}

# Test: GitLab origin NOT created when only DNS is set (ARN empty)
run "gitlab_origin_disabled_when_arn_empty" {
  command = plan

  variables {
    gitlab_origin_dns = "internal-gitlab-alb-123456.us-east-1.elb.amazonaws.com"
    gitlab_origin_arn = ""
  }

  assert {
    condition     = length(aws_cloudfront_vpc_origin.gitlab) == 0
    error_message = "GitLab VPC Origin should not be created when gitlab_origin_arn is empty."
  }
}

# Test: GitLab origin NOT created when only ARN is set (DNS empty)
run "gitlab_origin_disabled_when_dns_empty" {
  command = plan

  variables {
    gitlab_origin_dns = ""
    gitlab_origin_arn = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/gitlab-alb/abc123"
  }

  assert {
    condition     = length(aws_cloudfront_vpc_origin.gitlab) == 0
    error_message = "GitLab VPC Origin should not be created when gitlab_origin_dns is empty."
  }
}
