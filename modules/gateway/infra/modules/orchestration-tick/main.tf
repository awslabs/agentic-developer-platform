# =============================================================================
# Orchestration Tick Module (Issue #4203)
# =============================================================================
# The engine's heartbeat: a scheduled Lambda in the VPC that reads the durable
# orchestration graph in RDS, moves the nodes whose predecessors are satisfied
# from `pending` to `ready`, and exits. Stateless — all continuity lives in
# Postgres, so the tick is safe to kill, retry and overlap.
#
# Terraform shape follows `../budget-lambda` (the `pricing_refresh` function, the
# scheduled sibling — NOT the S3-event-driven `usage_tracker`): schedule rule →
# target → invoke permission, Lambda in `vpc_config`, dedicated SG with egress to
# RDS, and `rds-db:connect` scoped to a single dbuser in `iam.tf`.
#
# ONE DELIBERATE DEVIATION from that precedent: packaging. The budget Lambdas are
# flat `archive_file` zips using raw psycopg2 via a layer. This function runs
# `src/orchestration/tick.py`, which is async SQLAlchemy over asyncpg, and:
#   * no existing Lambda layer ships sqlalchemy/asyncpg/greenlet, and adding one
#     requires a new CodeBuild project in `platform/infra/` — a DIFFERENT
#     Terraform state that `gateway-infra-apply.yml` does not apply, so the
#     gateway apply would fail at plan time on the missing layer object;
#   * every `archive_file` in this repo flattens filenames, so `from src...`
#     cannot resolve inside such a zip;
#   * `src/shared/database.py` verifies RDS TLS against
#     `/etc/ssl/certs/rds-global-bundle.pem`, which the Dockerfile provides and
#     the bare Lambda runtime does not.
# The `adp-gateway` image already contains all three, so the tick runs from it.
# See the header of `src/orchestration/tick_handler.py` for the full rationale.
#
# NAMING: the function, log group and rule are all `${var.name_prefix}-orchestration-tick`
# and the caller passes `adp-<env>` (NOT the gateway's `bedrockgw-<env>`), because
# the wave-3 evaluation pins `adp-dev-orchestration-tick` exactly.
# =============================================================================

locals {
  tick_name = "${var.name_prefix}-orchestration-tick"
}

# =============================================================================
# Security Group
# =============================================================================

resource "aws_security_group" "tick" {
  name        = "${local.tick_name}-sg"
  description = "Security group for the orchestration tick Lambda"
  vpc_id      = var.vpc_id

  egress {
    description     = "PostgreSQL to RDS"
    from_port       = var.db_port
    to_port         = var.db_port
    protocol        = "tcp"
    security_groups = [var.rds_security_group_id]
  }

  # Needed for the RDS IAM auth token (STS/RDS) and PutMetricData. Without it a
  # VPC Lambda in private subnets cannot reach the AWS APIs at all.
  egress {
    description = "HTTPS for AWS APIs (RDS IAM auth token, CloudWatch metrics)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  dynamic "egress" {
    for_each = var.redis_enabled ? [1] : []
    content {
      description     = "Redis budget access to the existing gateway store"
      from_port       = var.redis_port
      to_port         = var.redis_port
      protocol        = "tcp"
      security_groups = [var.redis_security_group_id]
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${local.tick_name}-sg"
    Service = "lambda"
    Purpose = "orchestration-tick"
  })
}

# Reciprocal ingress on the RDS SG. Declared as a separate rule (not inline on
# the RDS SG) so this module never has to own that security group.
resource "aws_security_group_rule" "tick_to_rds" {
  description              = "Allow the orchestration tick Lambda to reach RDS PostgreSQL"
  type                     = "ingress"
  from_port                = var.db_port
  to_port                  = var.db_port
  protocol                 = "tcp"
  security_group_id        = var.rds_security_group_id
  source_security_group_id = aws_security_group.tick.id
}

# Reciprocal ingress on the shared VPC-interface-endpoint SG, so the tick can
# reach SQS (and STS/Secrets Manager) over the private endpoints. Same shape and
# rationale as `vpc_endpoints_from_eks_cluster` in platform/infra/main.tf: the
# endpoint SG in modules/networking only admits the EKS security groups inline,
# and it carries `lifecycle { ignore_changes = [ingress] }` precisely so that
# standalone rules like this one and the EKS rule do not revert each other on
# every apply. Declared here (not inline on the endpoint SG) so this module never
# has to own a security group it did not create — identical to `tick_to_rds`.
#
# Scoped to this Lambda's SG on 443 only. Deliberately NOT a CIDR-wide rule and
# NOT a timeout increase: a longer timeout would only make the hang in #4316 take
# longer to fail, and widening the endpoint SG would grant every workload in the
# VPC access to the private AWS endpoints to fix one Lambda. See #4316.
resource "aws_security_group_rule" "tick_to_vpc_endpoints" {
  count                    = var.vpc_endpoint_security_group_id != "" ? 1 : 0
  description              = "HTTPS to interface VPC endpoints (SQS) from the orchestration tick Lambda"
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = var.vpc_endpoint_security_group_id
  source_security_group_id = aws_security_group.tick.id
}

# =============================================================================
# CloudWatch Log Group
# =============================================================================

# Created explicitly rather than left to Lambda's implicit creation so retention
# and encryption are managed, and so the group the smoke check greps for
# (`/aws/lambda/adp-<env>-orchestration-tick`) exists from the first apply.
resource "aws_cloudwatch_log_group" "tick" {
  name              = "/aws/lambda/${local.tick_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${local.tick_name}-logs"
    Service = "cloudwatch"
    Purpose = "orchestration-tick"
  })
}

# =============================================================================
# Lambda Function
# =============================================================================

# IAM policy attachment is not implied by referencing the role ARN. Allow the
# initial VPC permissions to propagate before Lambda starts provisioning ENIs.
resource "time_sleep" "tick_iam_ready" {
  create_duration = "30s"
  depends_on      = [aws_iam_role_policy.tick]
}

resource "aws_lambda_function" "tick" {
  lifecycle {
    precondition {
      condition     = !var.redis_enabled || (var.redis_host != "" && var.redis_security_group_id != "" && var.redis_port > 0 && var.redis_port <= 65535 && (!var.redis_iam_auth || (var.redis_username != "" && var.redis_cache_name != "")))
      error_message = "Redis budget admission requires the existing store endpoint, network identity, and configured IAM user."
    }
    precondition {
      condition     = !var.agent_authority_enabled || (var.webhook_events_table_name != "" && var.webhook_events_kms_key_arn != "")
      error_message = "Protected engine dispatch requires the webhook events table and its KMS key."
    }
  }
  function_name = local.tick_name
  description   = "Orchestration engine tick: advances graph nodes whose predecessors are satisfied (Issue #4203)"

  package_type = "Image"
  image_uri    = var.image_uri

  image_config {
    # The gateway image is built for the pod, so its ENTRYPOINT/CMD are uvicorn.
    # Supply the Lambda Runtime Interface Client here rather than changing the
    # image, so the K8s deployment (which overrides neither) keeps working off the
    # same artifact. `awslambdaric` is installed by the Dockerfile.
    entry_point       = ["/usr/local/bin/python", "-m", "awslambdaric"]
    command           = ["src.orchestration.tick_handler.handler"]
    working_directory = "/app"
  }

  role        = aws_iam_role.tick.arn
  memory_size = var.tick_memory
  timeout     = var.tick_timeout

  # Not a correctness guard (the tick is safe to overlap by construction) — it
  # bounds how many connections the tick can ever open against RDS.
  reserved_concurrent_executions = var.reserved_concurrency

  tracing_config {
    mode = "Active"
  }

  vpc_config {
    subnet_ids         = var.private_subnet_ids
    security_group_ids = [aws_security_group.tick.id]
  }

  environment {
    variables = merge(local.redis_environment, {
      # BG_ prefix: src/shared/config.py Settings uses env_prefix = "BG_".
      BG_RDS_IAM_AUTH   = "true"
      BG_RDS_HOST       = var.db_host
      BG_RDS_PORT       = tostring(var.db_port)
      BG_RDS_DBNAME     = var.db_name
      BG_RDS_USERNAME   = var.db_username
      BG_RDS_TLS_VERIFY = tostring(var.rds_tls_verify)
      BG_AWS_REGION     = var.aws_region
      BG_LOG_LEVEL      = "INFO"

      # Issue #4211. The delivery target is configuration, never a hard-coded
      # address — `notify.py` reads this and nothing else. An unset value makes
      # `notify()` raise, which the tick records as a failed notification rather
      # than treating an unwired environment as a delivered one.
      BG_ORCH_NOTIFY_TOPIC_ARN = aws_sns_topic.alerts.arn

      # Issue #4313 — engine dispatch. The tick resolves genesis in-process and
      # produces the agent envelope onto this queue itself; there is no transport
      # and no other producer added. All four are read by `dispatch_pass.py` and
      # nothing else. Empty queue URL or repo means nothing is dispatched and the
      # tick reports `dispatch_enabled=false` — visible, not silent.
      BG_ORCH_DISPATCH_QUEUE_URL             = var.agent_submit_queue_url
      BG_ORCH_DISPATCH_REPO                  = var.dispatch_repo
      BG_ORCH_DISPATCH_PERSONA               = var.dispatch_persona
      BG_ORCH_DISPATCH_MAX_PER_TICK          = tostring(var.dispatch_max_per_tick)
      AGENT_AUTHORITY_ENABLED                = tostring(var.agent_authority_enabled)
      ADP_SHARED_RUN_REPORTING_ENABLED       = tostring(var.shared_run_reporting_enabled)
      ADP_SHARED_WORKER_CONTINUATION_ENABLED = tostring(var.shared_worker_continuation_enabled)
      AGENT_WORKER_ROLE_ARN                  = var.shared_worker_role_arn
      AGENT_RUN_CREDENTIAL_KEY_PARAMETER     = var.run_report_key_parameter
      PERSONA_MODEL_MAPPING_ENABLED          = tostring(var.persona_model_mapping_enabled)
      AGENT_AUTHORITY_TABLE                  = "${var.name_prefix}-agent-authority"

      # Issue #4527 — the GitHub engine-command bridge. NOT BG_-prefixed: both are
      # read with a bare `os.environ.get`, the flag because `engine_commands.py`
      # hand-rolls the platform's fail-closed flag semantics rather than importing
      # the route module onto the tick path, and the table name because it is the
      # same variable the gateway's activity and stats services already read for
      # the same table.
      #
      # Flag off, or table name empty, means the pass reads nothing, writes nothing
      # and — deliberately — acknowledges nothing: it reports
      # `commands_enabled=false` instead. A "the engine is disabled" reply would
      # advertise the bridge to anyone who can comment on an issue.
      FEATURE_ORCHESTRATION_ENGINE_ENABLED = tostring(var.engine_enabled)
      WEBHOOK_EVENTS_TABLE                 = var.webhook_events_table_name

      # Issue #4539 — command attribution. NOT BG_-prefixed, and deliberately the
      # SAME name the signer reads in the webhook Lambda: one signing key behind two
      # env-var names drifts silently, and the failure mode of that drift is every
      # command refused. `command_attribution.py` reads it with a bare
      # `os.environ.get` for the same reason `engine_commands.py` does.
      #
      # Empty means the verifier has no key, so every pending command quarantines
      # with `no_verification_key` rather than being applied unverified. That is the
      # fail-closed default: an unwired verifier must never be read as permission to
      # trust a row whose authority fields could have been authored rather than
      # delivered.
      ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN = var.engine_command_signing_key_secret_arn
    })
  }

  tags = merge(var.common_tags, {
    Name    = local.tick_name
    Service = "lambda"
    Purpose = "orchestration-tick"
  })

  depends_on = [
    time_sleep.tick_iam_ready,
    aws_iam_role_policy.tick_redis,
    aws_security_group_rule.redis_from_tick,
    aws_cloudwatch_log_group.tick,
    aws_security_group_rule.tick_to_rds,
    aws_security_group_rule.tick_to_vpc_endpoints,
  ]
}

# =============================================================================
# EventBridge Schedule
# =============================================================================

# Name is exactly `${var.name_prefix}-orchestration-tick` — no `-schedule`
# suffix — so the rule, the function and the log group all share one name.
resource "aws_cloudwatch_event_rule" "tick" {
  name                = local.tick_name
  description         = "Triggers the orchestration engine tick (Issue #4203)"
  schedule_expression = var.tick_schedule
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"

  tags = merge(var.common_tags, {
    Name    = local.tick_name
    Service = "eventbridge"
    Purpose = "orchestration-tick"
  })
}

resource "aws_cloudwatch_event_target" "tick" {
  rule      = aws_cloudwatch_event_rule.tick.name
  target_id = local.tick_name
  arn       = aws_lambda_function.tick.arn
}

resource "aws_lambda_permission" "tick_eventbridge" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.tick.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.tick.arn
}
