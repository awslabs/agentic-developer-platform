mock_provider "aws" {}

variables {
  environment                    = "dev"
  name_prefix                    = "adp-dev"
  s3_bucket_regional_domain_name = "frontend.s3.us-east-1.amazonaws.com"
  s3_bucket_id                   = "frontend"
  alb_domain_name                = "internal.example.com"
}

run "default_preserves_alb" {
  command = plan
  assert {
    condition     = aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].path_pattern == "/api/*"
    error_message = "Default behavior must continue using ALB without a Task override."
  }
}

run "task_route_only" {
  command = plan
  variables {
    enable_task_api_route          = true
    enable_broker_cloudfront_route = false
    broker_origin_domain_name      = "abc123.execute-api.us-east-1.amazonaws.com"
    broker_origin_path             = "/dev"
  }
  assert {
    condition     = aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].path_pattern == "/api/v1/tasks" && aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].target_origin_id == "adp-dev-broker-origin"
    error_message = "The exact Task endpoint must precede /api/* and use REST ingress."
  }
  assert {
    condition     = aws_cloudfront_distribution.frontend.ordered_cache_behavior[1].path_pattern == "/api/*" && aws_cloudfront_distribution.frontend.ordered_cache_behavior[1].target_origin_id == "adp-dev-api-origin"
    error_message = "Task child streams and other APIs must retain ALB routing."
  }
  assert {
    condition     = !contains([for b in aws_cloudfront_distribution.frontend.ordered_cache_behavior : b.path_pattern], "/auth/github/*")
    error_message = "Task routing must not enable GitHub broker behavior."
  }
  assert {
    condition     = one([for o in aws_cloudfront_distribution.frontend.origin : o.origin_path if o.origin_id == "adp-dev-broker-origin"]) == "/dev"
    error_message = "REST origin must include its published stage."
  }
  assert {
    condition     = aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].cache_policy_id == data.aws_cloudfront_cache_policy.caching_disabled.id && aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].origin_request_policy_id == data.aws_cloudfront_origin_request_policy.all_viewer_except_host.id
    error_message = "Task admission must disable cache and forward authorization without viewer Host."
  }
  assert {
    condition     = contains(aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].allowed_methods, "POST") && length(aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].function_association) == 1
    error_message = "Task POST must use the existing prefix/trust-header function."
  }
}

run "broker_only_preserved" {
  command = plan
  variables {
    broker_origin_domain_name = "abc123.execute-api.us-east-1.amazonaws.com"
    broker_origin_path        = "/dev"
  }
  assert {
    condition     = contains([for b in aws_cloudfront_distribution.frontend.ordered_cache_behavior : b.path_pattern], "/auth/github/*") && !contains([for b in aws_cloudfront_distribution.frontend.ordered_cache_behavior : b.path_pattern], "/api/v1/tasks")
    error_message = "Existing broker module callers must remain unchanged."
  }
}

run "task_without_origin_fails" {
  command = plan
  variables { enable_task_api_route = true }
  expect_failures = [aws_cloudfront_distribution.frontend]
}

run "task_and_broker_share_rest_origin_with_vpc_streams" {
  command = plan
  variables {
    enable_task_api_route     = true
    broker_origin_domain_name = "abc123.execute-api.us-east-1.amazonaws.com"
    broker_origin_path        = "/dev"
    enable_vpc_origin         = true
    internal_alb_arn          = "arn:aws:elasticloadbalancing:us-east-1:879318057152:loadbalancer/app/gateway/abc123"
    internal_alb_dns          = "internal-gateway.us-east-1.elb.amazonaws.com"
  }
  assert {
    condition     = aws_cloudfront_distribution.frontend.ordered_cache_behavior[0].path_pattern == "/api/v1/tasks" && aws_cloudfront_distribution.frontend.ordered_cache_behavior[1].target_origin_id == "adp-dev-vpc-api-origin"
    error_message = "Task admission must use REST while child streams keep the VPC origin."
  }
  assert {
    condition     = contains([for b in aws_cloudfront_distribution.frontend.ordered_cache_behavior : b.path_pattern], "/auth/github/*") && length([for o in aws_cloudfront_distribution.frontend.origin : o if o.origin_id == "adp-dev-broker-origin"]) == 1
    error_message = "Task and broker routes must share one existing REST API origin."
  }
}
