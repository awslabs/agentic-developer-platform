# =============================================================================
# Unknown-model pricing alarm (Issue #4592)
# =============================================================================
# pricing_fallback.get_model_pricing() emits ADP/Gateway · UnknownModelPricing
# whenever a model id misses MODEL_PRICING and is therefore billed at the
# generic "default" rate. The metric already existed; nothing alarmed on it,
# which is why three live model ids were mispriced for weeks with the evidence
# sitting in CloudWatch.
#
# The metric is emitted WITHOUT dimensions, deliberately: CloudWatch alarms
# cannot be created on SEARCH() expressions, so a per-ModelId dimension would
# make the metric un-alarmable — and each distinct id would mint a permanent
# paid custom metric with caller-controlled cardinality. The offending id is
# in the Lambda's WARNING log; the alarm only needs "something mispriced".
#
# treat_missing_data = "notBreaching": the metric is emitted ONLY on a miss —
# a healthy fleet publishes nothing at all and there is no "explicit 0"
# heartbeat, so missing data genuinely means "no unknown models".
#
# No ok_actions: the emitter goes quiet as soon as the id's records stop
# arriving, NOT when the id is added to the table — an auto-OK an hour after
# each burst would read as "self-healed" while the root cause is unfixed.
#
# Lives in budget-lambda rather than budget-alarms because the emitter is this
# module's Lambda and both are gated on enable_chat_logging. An alarm that
# outlives its emitter is an INSUFFICIENT_DATA trap.
resource "aws_cloudwatch_metric_alarm" "unknown_model_pricing" {
  alarm_name        = "${var.name_prefix}-unknown-model-pricing"
  alarm_description = <<-EOT
    A model id was billed at the generic "default" fallback rate because it is
    missing from MODEL_PRICING in modules/gateway/lambda/shared/pricing_fallback.py.

    Cost records for that model are computed at the wrong rate (the default is
    Sonnet-tier, so Opus-class models underbill ~40% on output). Budget caps
    and spend dashboards derived from these records misstate real spend until
    the id is added.

    Remediation: find the offending id in
    /aws/lambda/${var.name_prefix}-budget-usage-tracker ("Unknown model ..."),
    add an entry with ALL FOUR keys (input, output, cache_read_input,
    cache_creation_input) to MODEL_PRICING, then dispatch
    gateway-infra-apply.yml to ship the updated Lambda.

    Note: historical rows already written at the default rate are not
    retroactively repriced.
  EOT

  namespace           = "ADP/Gateway"
  metric_name         = "UnknownModelPricing"
  statistic           = "Sum"
  period              = 3600
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-unknown-model-pricing"
    Service = "cloudwatch"
    Purpose = "budget-usage-tracker"
  })
}
