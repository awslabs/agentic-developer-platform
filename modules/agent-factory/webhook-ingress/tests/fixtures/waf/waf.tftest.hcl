mock_provider "aws" {}

override_resource {
  target          = aws_kms_key.cloudwatch
  override_during = plan
  values = {
    arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
  }
}

run "defaults_preserve_unrestricted_rate_limited_stage" {
  command = plan
  assert {
    condition = (
      length(one(aws_wafv2_web_acl.webhook.default_action).allow) == 1 &&
      length(aws_wafv2_web_acl.webhook.rule) == 1 &&
      one(aws_wafv2_web_acl.webhook.rule).name == "rate-limit-per-ip" &&
      one(aws_wafv2_web_acl.webhook.rule).priority == 1 &&
      length(one(one(aws_wafv2_web_acl.webhook.rule).action).block) == 1 &&
      one(one(one(aws_wafv2_web_acl.webhook.rule).statement).rate_based_statement).limit == 2000 &&
      one(one(one(aws_wafv2_web_acl.webhook.rule).statement).rate_based_statement).aggregate_key_type == "IP"
    )
    error_message = "With no source sets, preserve the existing per-IP rate limit and default Allow."
  }
  assert {
    condition = (
      length(aws_cloudwatch_log_group.webhook_waf) == 0 &&
      length(aws_wafv2_web_acl_logging_configuration.webhook) == 0 &&
      one(aws_wafv2_web_acl.webhook.visibility_config).sampled_requests_enabled
    )
    error_message = "Default settings must preserve the existing logging and sampling behaviour."
  }
}

run "restricted_sources_still_reach_rate_limiter_first" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
  }
  assert {
    condition = (
      length(one(aws_wafv2_web_acl.webhook.default_action).block) == 1 &&
      length(aws_wafv2_web_acl.webhook.rule) == 2 &&
      one([for rule in aws_wafv2_web_acl.webhook.rule : rule.priority if rule.name == "rate-limit-per-ip"]) <
      one([for rule in aws_wafv2_web_acl.webhook.rule : rule.priority if rule.name == "allow-github-hooks-and-internal-callers"]) &&
      length(one(one([for rule in aws_wafv2_web_acl.webhook.rule : rule.action if rule.name == "rate-limit-per-ip"])).block) == 1
    )
    error_message = "An over-limit allowed source must hit a terminating Block before any Allow; unknown sources must default to Block."
  }
  assert {
    condition = toset([
      for statement in one(one(one([
        for rule in aws_wafv2_web_acl.webhook.rule : rule.statement
        if rule.name == "allow-github-hooks-and-internal-callers"
      ])).or_statement).statement : one(statement.ip_set_reference_statement).arn
    ]) == toset([var.github_hooks_ipv4_ip_set_arn, var.github_hooks_ipv6_ip_set_arn, var.internal_callers_ip_set_arn])
    error_message = "The Allow rule must reference both GitHub address families and internal callers, without extra sources."
  }
}

run "logging_redacts_credentials_without_unredacted_sampling" {
  command = plan
  variables {
    enable_waf_logging     = true
    waf_log_retention_days = 14
  }
  assert {
    condition = (
      aws_cloudwatch_log_group.webhook_waf[0].name == "aws-waf-logs-adp-test-webhook-ingress" &&
      aws_cloudwatch_log_group.webhook_waf[0].retention_in_days == 14 &&
      aws_cloudwatch_log_group.webhook_waf[0].kms_key_id == aws_kms_key.cloudwatch.arn &&
      toset([for field in aws_wafv2_web_acl_logging_configuration.webhook[0].redacted_fields : one(field.single_header).name]) ==
      toset(["authorization", "cookie", "x-api-key", "x-amz-security-token", "x-gitlab-token"]) &&
      !one(aws_wafv2_web_acl.webhook.visibility_config).sampled_requests_enabled &&
      alltrue([for rule in aws_wafv2_web_acl.webhook.rule : !one(rule.visibility_config).sampled_requests_enabled]) &&
      length(one(aws_wafv2_web_acl.webhook.default_action).allow) == 1
    )
    error_message = "Logging alone must redact credentials, encrypt logs and disable sampling without activating source restrictions."
  }
}

run "gitlab_can_share_internal_nat_with_logging_enabled" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
    gitlab_webhook_enabled       = true
    gitlab_webhook_ip_set_arns   = ["arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"]
    enable_waf_logging           = true
  }
  assert {
    condition = (
      length(local.waf_source_sets) == 3 &&
      alltrue([for rule in aws_wafv2_web_acl.webhook.rule : !one(rule.visibility_config).sampled_requests_enabled]) &&
      length(one(aws_wafv2_web_acl.webhook.default_action).block) == 1
    )
    error_message = "GitLab can explicitly reuse the internal NAT IP set; every rule must disable sampling while logs are enabled."
  }
}

run "gitlab_has_its_own_dual_stack_sources" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
    gitlab_webhook_enabled       = true
    gitlab_webhook_ip_set_arns = [
      "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/gitlab4/00000000-0000-0000-0000-000000000014",
      "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/gitlab6/00000000-0000-0000-0000-000000000016",
    ]
  }
  assert {
    condition = toset([
      for statement in one(one(one([
        for rule in aws_wafv2_web_acl.webhook.rule : rule.statement
        if rule.name == "allow-github-hooks-and-internal-callers"
      ])).or_statement).statement : one(statement.ip_set_reference_statement).arn
    ]) == toset(concat([var.github_hooks_ipv4_ip_set_arn, var.github_hooks_ipv6_ip_set_arn, var.internal_callers_ip_set_arn], var.gitlab_webhook_ip_set_arns))
    error_message = "GitLab IPv4/IPv6 source sets must be included in the actual Allow rule."
  }
}

run "gitlab_cannot_be_silently_locked_out" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
    gitlab_webhook_enabled       = true
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "gitlab_sets_without_primary_sets_are_rejected" {
  command = plan
  variables {
    gitlab_webhook_enabled     = true
    gitlab_webhook_ip_set_arns = ["arn:aws:wafv2:us-east-1:123456789012:regional/ipset/gitlab4/00000000-0000-0000-0000-000000000014"]
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "blank_gitlab_set_is_rejected" {
  command = plan
  variables {
    gitlab_webhook_ip_set_arns = [" "]
  }
  expect_failures = [var.gitlab_webhook_ip_set_arns]
}

run "invalid_retention_is_rejected" {
  command = plan
  variables {
    waf_log_retention_days = 2
  }
  expect_failures = [var.waf_log_retention_days]
}

run "global_ip_set_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:global/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "same_github_ip_set_for_both_families_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "partial_1_of_3_sources_case_1_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "partial_1_of_3_sources_case_2_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "partial_1_of_3_sources_case_3_is_rejected" {
  command = plan
  variables {
    internal_callers_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "partial_2_of_3_sources_case_1_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "partial_2_of_3_sources_case_2_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv4_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github4/00000000-0000-0000-0000-000000000004"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}

run "partial_2_of_3_sources_case_3_is_rejected" {
  command = plan
  variables {
    github_hooks_ipv6_ip_set_arn = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/github6/00000000-0000-0000-0000-000000000006"
    internal_callers_ip_set_arn  = "arn:aws:wafv2:us-east-1:123456789012:regional/ipset/internal/00000000-0000-0000-0000-000000000010"
  }
  expect_failures = [aws_wafv2_web_acl.webhook]
}
