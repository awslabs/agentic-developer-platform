# CloudWatch alarm for rate-limit monitoring.
# Fires when any tenant hits rate limits more than 10 times per minute.

resource "aws_cloudwatch_metric_alarm" "rate_limit_alarm" {
  alarm_name          = "adp-${var.environment}-webhook-rate-limit-high"
  alarm_description   = "Rate limit hits > 10/min for webhook ingress — potential abuse or misconfigured tenant"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "RateLimited"
  namespace           = "WebhookIngress"
  period              = 60
  statistic           = "Sum"
  threshold           = 10
  treat_missing_data  = "notBreaching"

  tags = merge(var.tags, {
    Name      = "adp-${var.environment}-webhook-rate-limit-high"
    Module    = "webhook-ingress"
    Component = "monitoring"
  })
}

# =============================================================================
# Agent bootstrap log group (issue #4028)
# =============================================================================
# The agent worker writes step-level Setup/bootstrap logs here so bootstrap
# failures remain diagnosable after KEDA garbage-collects the pod
# (agent-worker-image/lib/bootstrap_logger.py:174).
#
# Declared in Terraform rather than left to the worker's runtime CreateLogGroup
# so that (a) retention is fixed at this module's 14-day convention instead of
# the worker's hardcoded 7 (see the PutRetentionPolicy note in
# scaledjob-iam.tf), and (b) the group is CMK-encrypted and visible to checkov —
# a worker-created group would be unencrypted and outside state.
#
# The worker still holds logs:CreateLogGroup (scaledjob-iam.tf) on purpose: it
# calls CreateLogGroup unconditionally and only tolerates
# ResourceAlreadyExistsException, so revoking it would disable CloudWatch
# bootstrap logging for the entire run.
#
# NOTE: kms_key_id here requires no extra KMS permission on the agent-worker
# role. For CMK-encrypted log groups the CloudWatch Logs *service* performs the
# encryption under its own key-policy grant (kms.tf CloudWatchLogsService
# statement, ArnLike on log-group:*), not the PutLogEvents caller.
resource "aws_cloudwatch_log_group" "agent_bootstrap" {
  name              = "/adp/${var.environment}/agent-factory/bootstrap"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.cloudwatch.arn

  tags = merge(var.tags, {
    Name      = "/adp/${var.environment}/agent-factory/bootstrap"
    Module    = "webhook-ingress"
    Component = "hosted-agent-worker"
  })
}

# =============================================================================
# Agent primary log group (issue #4221)
# =============================================================================
# The worker's main structured log stream — everything the Node entrypoints emit
# via log() after bootstrap hands off (agent/src/lib/logGroup.ts).
#
# Before this existed, all five entrypoints (agent-worker, agent-superpower,
# agent-pm, skill-agent, components/Logger) hardcoded the env-less group
# /github-ccsdk-agent/logs, which was created by no Terraform resource and
# existed in no account. The failure was silent and one step earlier than a
# denied PutLogEvents: initCloudWatch() calls CreateLogStream on the missing
# group, gets ResourceNotFoundException, and leaves cwInitialized false — so
# log() never buffers and PutLogEvents is never attempted at all. The single
# console.warn went to pod stdout, which KEDA garbage-collects with the pod.
# Operators debugging a failed run found nothing and assumed the run never
# logged. #4184 is a bug that stayed hidden for months behind exactly this gap.
#
# Env-scoped rather than reusing the env-less name, per #4028: an env-less group
# has staging/prod agents writing into dev's logs, and makes env-scoped IAM deny
# silently outside dev.
#
# Retention/CMK mirror aws_cloudwatch_log_group.agent_bootstrap above, including
# the note that a CMK-encrypted group needs no extra KMS grant on the worker
# role — CloudWatch Logs encrypts under its own key-policy grant (kms.tf).
#
# Unlike the bootstrap group, this one is NOT at risk of the #4051
# ResourceAlreadyExistsException wedge: no code path creates it at runtime. The
# TS workers only ever call CreateLogStream — CreateLogGroup is called solely by
# bootstrap_logger.py, and only for the bootstrap group. The corresponding IAM
# statement therefore does not grant CreateLogGroup (see scaledjob-iam.tf), so
# no import is required and repeated applies are a clean no-op.
resource "aws_cloudwatch_log_group" "agent_logs" {
  name              = "/adp/${var.environment}/agent-factory/agent"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.cloudwatch.arn

  tags = merge(var.tags, {
    Name      = "/adp/${var.environment}/agent-factory/agent"
    Module    = "webhook-ingress"
    Component = "hosted-agent-worker"
  })
}
