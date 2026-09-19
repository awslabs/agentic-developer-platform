# =============================================================================
# GitHub Webhook Lambda
# =============================================================================
# Full handler: HMAC validation via Secrets Manager, tenant resolution,
# rate limiting, intent parsing, and SQS publish.
#
# Code source: the Package Lambda Code CI job uploads the zip to
#   s3://${lambda_artifact_bucket}/lambda-artifacts/webhook-ingress/github.zip
# Terraform reads the zip's SHA from S3 object metadata, so apply tracks
# code version without needing the zip on the local filesystem. The
# Update Lambda Function Code CI job is the authoritative code publisher
# on each deploy — Terraform only manages config.
# =============================================================================

data "aws_s3_object" "github_lambda_zip" {
  bucket = local.lambda_artifact_bucket
  key    = "lambda-artifacts/webhook-ingress/github.zip"
}

# -----------------------------------------------------------------------------
# VPC attachment source (see variables.tf)
# -----------------------------------------------------------------------------
# The subnet and security-group ids can come from either the variables or SSM.
# SSM is the default because this module is applied from more than one place —
# deploy-webhook-ingress.sh passes only -var overrides, and
# webhook-ingress-deploy.yml passes only this directory's terraform.tfvars — so a
# value that lives in a per-environment tfvars file is read by some callers and
# silently ignored by others. A Lambda that quietly leaves the VPC on the next
# unrelated CI apply is a bad failure: it keeps working until whatever restricted
# the gateway API starts rejecting it.
#
# Same reasoning as Issue #575 for gateway_api_url and internal_api_key_arn,
# which resolve from SSM at apply time for exactly this reason.
#
# aws_ssm_parameters_by_path returns an empty result rather than erroring when
# the path holds nothing, so deployments that never create these parameters are
# unaffected — no count guard or try() needed.
data "aws_ssm_parameters_by_path" "webhook_lambda_vpc_config" {
  path = "/adp/${var.environment}/webhook-ingress/vpc-config/"
}

locals {
  # nonsensitive() because the provider marks SSM values sensitive as a class.
  # Subnet and security-group ids are not secrets, and leaving them marked
  # redacts the whole vpc_config block in every plan — hiding the one thing a
  # reviewer of this change needs to see.
  ssm_vpc_config = zipmap(
    [for name in data.aws_ssm_parameters_by_path.webhook_lambda_vpc_config.names : basename(name)],
    nonsensitive(data.aws_ssm_parameters_by_path.webhook_lambda_vpc_config.values)
  )

  # Explicit variables win, so an operator can override or force-detach without
  # touching SSM.
  webhook_lambda_subnet_ids = length(var.webhook_lambda_subnet_ids) > 0 ? var.webhook_lambda_subnet_ids : (
    contains(keys(local.ssm_vpc_config), "subnet-ids") ? split(",", local.ssm_vpc_config["subnet-ids"]) : []
  )

  webhook_lambda_security_group_ids = length(var.webhook_lambda_security_group_ids) > 0 ? var.webhook_lambda_security_group_ids : (
    contains(keys(local.ssm_vpc_config), "security-group-ids") ? split(",", local.ssm_vpc_config["security-group-ids"]) : []
  )
}

resource "aws_lambda_function" "github_webhook" {
  function_name                  = "${local.name_prefix}-github-webhook"
  description                    = "GitHub webhook ingress - validates and queues events"
  role                           = aws_iam_role.lambda_execution.arn
  reserved_concurrent_executions = var.enable_lambda_reserved_concurrency ? 20 : -1

  s3_bucket        = local.lambda_artifact_bucket
  s3_key           = "lambda-artifacts/webhook-ingress/github.zip"
  source_code_hash = data.aws_s3_object.github_lambda_zip.etag
  handler          = "handler.handler"
  runtime          = var.lambda_runtime
  timeout          = var.lambda_timeout
  memory_size      = var.lambda_memory_size

  tracing_config {
    mode = "Active"
  }

  # Optional VPC attachment. Omitted entirely when no subnets are given, so the
  # default remains a non-VPC Lambda rather than one attached to an empty subnet
  # list (which would fail).
  dynamic "vpc_config" {
    for_each = length(local.webhook_lambda_subnet_ids) > 0 ? [1] : []
    content {
      subnet_ids         = local.webhook_lambda_subnet_ids
      security_group_ids = local.webhook_lambda_security_group_ids
    }
  }

  environment {
    variables = {
      ENVIRONMENT                   = var.environment
      SUBMIT_QUEUE_URL              = aws_sqs_queue.agent_submit.url
      IDENTITY_INDEX_TABLE          = var.identity_index_table_name
      USER_IDENTITY_INDEX_TABLE     = "adp-${var.environment}-user-identity-index"
      EVENTS_TABLE                  = aws_dynamodb_table.webhook_events.name
      AGENT_AUTHORITY_TABLE         = aws_dynamodb_table.agent_authority.name
      AGENT_AUTHORITY_ENABLED       = tostring(var.agent_authority_enabled)
      ADP_WORK_CLAIMS_ENABLED       = tostring(var.agent_authority_enabled)
      ADP_AGENT_CONTROL_ENDPOINT    = "${data.aws_ssm_parameter.gateway_apigw_invoke_url.value}/internal/v1/agent"
      RATE_LIMITS_TABLE             = aws_dynamodb_table.rate_limits.name
      RATE_LIMIT_PER_WINDOW         = tostring(var.rate_limit_per_window)
      RATE_LIMIT_PER_HOUR           = tostring(var.rate_limit_per_hour)
      WEBHOOK_SECRET_ARN            = aws_secretsmanager_secret.webhook_secret.arn
      GATEWAY_API_URL               = var.gateway_api_url
      USER_IDENTITY_INDEX_V2_WRITE  = "false"
      USER_IDENTITY_INDEX_V2_READ   = "true"
      INTERNAL_API_KEY_ARN          = var.internal_api_key_arn
      RESOLVE_CANONICAL_VIA_GATEWAY = "true"
      CORRELATION_POINTERS_TABLE    = aws_dynamodb_table.correlation_pointers.name
      TENANT_REGISTRY_TABLE         = aws_dynamodb_table.tenant_registry.name
      # Issue #3179 (cred-binding S5): marker signature verification key
      MARKER_SIGNING_KEY_SECRET_ARN = aws_secretsmanager_secret.marker_signing_key.arn
      # Issue #4539: the DEDICATED engine-command attribution signing keyring.
      # Deliberately a different secret from MARKER_SIGNING_KEY_SECRET_ARN above —
      # that one is readable by the agent worker cohort by design, so reusing it
      # would let any worker mint a command envelope naming any tenant and any
      # commenter. Same env var name the gateway verifier reads: one key behind two
      # names drifts silently, and the failure mode is every command refused.
      ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN = aws_secretsmanager_secret.engine_command_signing_key.arn
      # Issue #4047 (#2724 slice C): TTL for negative-cache rows recording that
      # the gateway authoritatively does not know an installation. Set to 0 to
      # disable the cache (every unknown installation re-asks the gateway).
      INSTALLATION_NEGATIVE_CACHE_TTL_SECONDS = tostring(var.installation_negative_cache_ttl_seconds)
      # Issue #2724 (slice B): open-onboarding switch for the auto-register
      # tenant gate. Deliberately the SAME variable name the gateway reads
      # (modules/gateway/k8s/configmap.yaml) rather than a second Lambda-only
      # flag, because one trust decision behind two names drifts silently.
      # The two deploy units govern different halves of it: the gateway's
      # decides whether the unauthenticated install callback may CREATE an org
      # shell; this one decides whether the webhook may TRUST such a shell as a
      # tenant. An open-onboarding deployment sets both "true". Leaving the
      # gateway "true" and this "false" (the default) is the intended secure
      # posture, not drift: shells are still created so the install UI works,
      # but they are not promoted to tenants and get no agent dispatch.
      # Flipping this to "true" is the documented env-only rollback.
      ORG_TENANT_AUTO_CREATE = tostring(var.org_tenant_auto_create)
      # Issue #4128: strict-rejection switch for /agent/trigger provenance.
      # A FORGED signature is rejected regardless of this flag. This governs
      # only the INDETERMINATE case (unsigned / no marker / placeholder key):
      # false (default) strips the claim's authority and warns, true returns
      # 403. Default off per #4073 decision 5 — the in-repo trigger client does
      # not sign yet, so flipping this before it does would break legitimate
      # agent-to-agent hops. See variables.tf for the rollout sequence.
      REQUIRE_SIGNED_PROVENANCE = tostring(var.require_signed_provenance)
    }
  }

  # Terraform reads source_code_hash from the S3 object etag (see data
  # source above). When CI uploads a new zip to the same S3 key, the etag
  # changes and the next Terraform apply will pick it up automatically.
  # The separate Update Lambda Function Code CI job is still the fast-path
  # code publisher (no full TF apply needed for a code-only change); its
  # updates are idempotent with Terraform's view because they write to the
  # same S3 source.

  # Checked here rather than as a variable validation: cross-variable validation
  # needs Terraform >= 1.9 and this module declares >= 1.5. Subnets without
  # security groups is the failure worth catching early — AWS rejects it, but
  # only after the plan has been approved.
  lifecycle {
    precondition {
      condition     = length(local.webhook_lambda_subnet_ids) == 0 || length(local.webhook_lambda_security_group_ids) > 0
      error_message = "Security groups must be set when subnets are: set webhook_lambda_security_group_ids, or publish security-group-ids under /adp/<env>/webhook-ingress/vpc-config/."
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, terraform_data.worker_gateway_rollout]
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name_prefix}-github-webhook"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.cloudwatch.arn
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.webhook.execution_arn}/*/POST/github"
}

# Issue #2152: Lambda permission for POST /agent/trigger (AWS_IAM auth route)
resource "aws_lambda_permission" "api_gateway_agent_trigger" {
  statement_id  = "AllowAPIGatewayInvokeAgentTrigger"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.github_webhook.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.webhook.execution_arn}/*/POST/agent/trigger"
}
