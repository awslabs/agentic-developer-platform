# Issue #5685: GitLab's operational endpoints must not be readable by anonymous
# public callers, and closing them must not disturb the ALB health check or any
# user-facing route.
#
# The exposure these tests guard: GitLab runs in relative_url_root mode, and the
# CloudFront /gitlab/* behavior forwards every method and header to this ALB with
# no auth and no path filtering. So /-/metrics (Prometheus counters),
# /-/metrics/system (runtime and worker details), /-/readiness (per-component
# DB/cache/Gitaly/Sidekiq detail) and /-/liveness were reachable anonymously at
# https://<cloudfront-domain>/gitlab/-/<endpoint>.
#
# What is asserted here, and why each assertion earns its place:
#
#   1. Every restricted route is covered, including Rails' optional format
#      suffix. A missing route variant is the leak.
#   2. The action is fixed-response 403, NOT a forward. A rule that forwards to
#      the target group matches the path and still serves the data — it looks
#      like enforcement in a diff while changing nothing.
#   3. The only wildcards are route-local `.*` format suffixes. `/gitlab/-/*`
#      would match ordinary GitLab routing (/-/profile, /-/user_settings,
#      project subpaths) and break normal browsing.
#   4. Denies are attached only to the CloudFront-facing HTTP listener. The
#      VPC-only HTTPS listener forwards the same /gitlab-prefixed monitoring
#      routes to approved operators.
#   5. The health check still probes the unprefixed /-/health. Health-check
#      regression is the expensive failure mode: an unhealthy target takes
#      GitLab fully offline, which is worse than the leak being fixed.
#
# Plan-only with a mocked provider, mirroring alb_rules_test.tftest.hcl: these
# are configuration assertions and must run without AWS credentials.

mock_provider "aws" {}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  gitlab_domain        = "gitlab.dev.adp.internal"
  route53_zone_name    = "dev.adp.internal"
  certificate_arn      = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
  cloudfront_domain    = "d123abc.cloudfront.net"
  cognito_user_pool_id = "us-east-1_TestPool"
  cognito_domain       = "adp-dev-auth"
}

run "public_ops_endpoints_are_denied_while_private_ops_routes_forward" {
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

  override_data {
    target = data.aws_route53_zone.private
    values = {
      zone_id = "Z00000000000000000000"
    }
  }

  override_resource {
    target          = aws_lb_listener.http
    override_during = plan
    values = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/gitlab/http"
    }
  }

  override_resource {
    target          = aws_lb_listener.https[0]
    override_during = plan
    values = {
      arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/gitlab/https"
    }
  }

  # ---------------------------------------------------------------------------
  # 1. Every restricted endpoint path is denied on the HTTP listener used by
  #    the CloudFront VPC origin.
  # ---------------------------------------------------------------------------

  assert {
    condition = toset(flatten([
      for rule in values(aws_lb_listener_rule.deny_public_ops_http) :
      tolist(one(rule.condition).path_pattern[0].values)
      ])) == toset([
      "/gitlab/-/metrics",
      "/gitlab/-/metrics/",
      "/gitlab/-/metrics.*",
      "/gitlab/-/metrics/system",
      "/gitlab/-/metrics/system/",
      "/gitlab/-/metrics/system.*",
      "/gitlab/-/readiness",
      "/gitlab/-/readiness/",
      "/gitlab/-/readiness.*",
      "/gitlab/-/liveness",
      "/gitlab/-/liveness/",
      "/gitlab/-/liveness.*",
    ])
    error_message = "HTTP deny rules must cover every public operational route, including /gitlab/-/metrics/system, trailing slashes, and Rails format suffixes such as .json."
  }

  # ---------------------------------------------------------------------------
  # 2. The action is a flat 403, not a forward. A forward would still serve the
  #    operational data while appearing to be a restriction.
  # ---------------------------------------------------------------------------

  assert {
    condition = alltrue([
      for r in values(aws_lb_listener_rule.deny_public_ops_http) :
      r.action[0].type == "fixed-response"
    ])
    error_message = "HTTP deny rules must use fixed-response. A 'forward' action matches the path and still returns the operational data."
  }

  assert {
    condition = alltrue([
      for r in values(aws_lb_listener_rule.deny_public_ops_http) :
      r.action[0].fixed_response[0].status_code == "403"
    ])
    error_message = "HTTP deny rules must return 403 for the restricted operational endpoints."
  }

  # ---------------------------------------------------------------------------
  # 3. Patterns target only specific operational routes under GitLab's
  #    /gitlab relative root. The only wildcard permitted is the route-local
  #    `.*` needed for Rails' optional format suffix.
  # ---------------------------------------------------------------------------

  assert {
    condition = alltrue([
      for r in values(aws_lb_listener_rule.deny_public_ops_http) :
      alltrue([
        for p in tolist(one(r.condition).path_pattern[0].values) :
        !strcontains(p, "*") || endswith(p, ".*")
      ])
    ])
    error_message = "Deny patterns may use only a route-local `.*` format suffix. '/gitlab/-/*' also matches ordinary GitLab routing and would break normal browsing."
  }

  # The complete expected pattern set above and the forwarding default together
  # prove these login, Git, webhook, profile, project and health paths remain
  # forwarded: none is an exact restricted path or starts with a restricted
  # route plus a dot.
  assert {
    condition = alltrue(flatten([
      for allowed_path in [
        "/gitlab/users/sign_in",
        "/gitlab/users/auth/openid_connect/callback",
        "/gitlab/example/project.git/info/refs",
        "/gitlab/api/v4/projects/1/hooks",
        "/gitlab/-/profile",
        "/gitlab/-/user_settings/profile",
        "/gitlab/example/project/-/tree/main",
        "/gitlab/example/project/-/merge_requests/1",
        "/-/health",
        ] : [
        for pattern in flatten([
          for r in values(aws_lb_listener_rule.deny_public_ops_http) :
          tolist(one(r.condition).path_pattern[0].values)
        ]) : pattern != allowed_path &&
        !(endswith(pattern, ".*") && startswith(allowed_path, trimsuffix(pattern, "*")))
      ]
    ]))
    error_message = "Deny rules must not match login, SSO callback, Git, webhook, profile, project or health-check paths."
  }

  # ---------------------------------------------------------------------------
  # 4. Public denies cannot intercept the private operator path.
  #
  # The known listener ARNs prove every fixed-response rule is attached only to
  # HTTP:80. HTTPS:443 is admitted only from the VPC CIDR and forwards by default,
  # so the actual relative-root monitoring routes such as /gitlab/-/readiness
  # remain available to approved private callers and subject to GitLab's narrowed
  # monitoring allowlist.
  # ---------------------------------------------------------------------------

  assert {
    condition = alltrue([
      for r in values(aws_lb_listener_rule.deny_public_ops_http) :
      r.listener_arn == aws_lb_listener.http.arn &&
      r.listener_arn != aws_lb_listener.https[0].arn
    ])
    error_message = "Operational endpoint denies must attach only to the CloudFront-facing HTTP listener; attaching them to private HTTPS blocks approved operators."
  }

  assert {
    condition = (
      aws_security_group_rule.alb_ingress_https.from_port == 443 &&
      aws_security_group_rule.alb_ingress_https.to_port == 443 &&
      toset(aws_security_group_rule.alb_ingress_https.cidr_blocks) == toset(["10.0.0.0/16"])
    )
    error_message = "The operator HTTPS listener must remain restricted to the VPC CIDR."
  }

  assert {
    condition     = aws_lb_listener.https[0].default_action[0].type == "forward"
    error_message = "HTTPS:443 must forward by default so approved private callers can read /gitlab-prefixed readiness and metrics routes."
  }

  assert {
    condition = (
      strcontains(aws_instance.gitlab.user_data, "gitlab_rails['relative_url_root'] = \"/gitlab\"") &&
      strcontains(aws_instance.gitlab.user_data, "nginx['relative_url_root'] = \"/gitlab\"")
    )
    error_message = "The test must exercise relative-root mode, where private operator monitoring routes are /gitlab-prefixed too."
  }

  # ---------------------------------------------------------------------------
  # 5. Health check and default public routing are untouched.
  #
  # The target-group health check never traverses listener rules (the LB nodes
  # probe the target directly), and it targets the UNPREFIXED /-/health, which
  # nginx answers ahead of Rails. Both properties are asserted so a future edit
  # that points the probe at a restricted path, or at the prefixed form, fails
  # here instead of silently marking the target unhealthy.
  # ---------------------------------------------------------------------------

  assert {
    condition     = aws_lb_target_group.gitlab.health_check[0].path == "/-/health"
    error_message = "ALB health check must probe the unprefixed /-/health, which nginx answers directly and which no deny rule matches."
  }

  assert {
    condition = !contains(
      flatten([for r in values(aws_lb_listener_rule.deny_public_ops_http) : tolist(one(r.condition).path_pattern[0].values)]),
      aws_lb_target_group.gitlab.health_check[0].path
    )
    error_message = "No deny rule may match the health-check path; an unhealthy target takes GitLab entirely offline."
  }

  assert {
    condition     = aws_lb_listener.http.default_action[0].type == "forward"
    error_message = "HTTP:80 listener must still forward by default so login, Git and webhook traffic keeps working."
  }

  # ---------------------------------------------------------------------------
  # 6. GitLab's Rails monitoring allowlist is not world-open.
  #
  # Asserted on the rendered user_data because that is where the finding's
  # specific line lives. Note this is defence in depth, not the control that
  # closes the exposure: all traffic arrives via the ALB, so Rails sees the ALB's
  # private address as the client regardless of this value. The deny rules above
  # are what refuse a public request. This assertion exists so the world-open
  # literal cannot come back unnoticed.
  # ---------------------------------------------------------------------------

  assert {
    condition     = strcontains(aws_instance.gitlab.user_data, "gitlab_rails['monitoring_whitelist'] = ['10.0.0.0/16']")
    error_message = "monitoring_whitelist must be rendered from the VPC CIDR, not a hard-coded or world-open value."
  }

  assert {
    condition     = !strcontains(aws_instance.gitlab.user_data, "monitoring_whitelist'] = ['0.0.0.0/0']")
    error_message = "monitoring_whitelist must not be 0.0.0.0/0 — that declares GitLab's operational endpoints readable by any caller that can reach the instance."
  }

  # The nginx-served health endpoint must survive in the rendered config: it is
  # what answers the ALB probe ahead of Rails, so losing it takes the target
  # unhealthy even though no listener rule changed.
  assert {
    condition     = strcontains(aws_instance.gitlab.user_data, "location /-/health")
    error_message = "The nginx /-/health location must remain in user_data; it answers the ALB health probe independently of Rails and of the monitoring allowlist."
  }
}
