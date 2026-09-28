# =============================================================================
# Budget enforcement alarms (Issue #4075)
# =============================================================================
# Budget enforcement fails CLOSED: an unreadable ledger denies the request
# rather than admitting uncapped spend. A bounded grace window keeps a transient
# RDS IAM-token blip from hard-downing all inference — but a grace window nobody
# is told about is just fail-open with extra steps. These alarms are the
# "observable" half of "bounded and observable".
#
# Metrics come from the gateway pod as CloudWatch EMF (src/shared/metrics.py,
# namespace BedrockGateway) via the cloudwatch-agent sidecar.
#
# Why treat_missing_data = "notBreaching" everywhere here: these metrics are
# emitted per budget check, so a genuinely idle gateway publishes nothing. The
# default ("missing") would leave the alarms in INSUFFICIENT_DATA, which reads
# as "fine" on a dashboard while telling you nothing. The app also emits an
# explicit 0 on every healthy check so the alarm has real datapoints to
# transition against.
# =============================================================================

# Grace window engaged: we are currently allowing requests we could not verify.
# Every second this is engaged is potentially uncapped spend, AND it is a
# countdown — when the window expires, all enforced paths start denying. This is
# the page-on-first-engage alarm.
resource "aws_cloudwatch_metric_alarm" "budget_check_grace_engaged" {
  alarm_name          = "${var.name_prefix}-budget-check-grace-engaged"
  alarm_description   = <<-EOT
    Budget enforcement cannot read the spend ledger and is allowing requests
    under its bounded grace window. Two things are true right now: spend is
    not being capped, and when the window expires every enforced inference
    path will start returning 503. Investigate RDS/IAM health immediately.

    Runbook: check gateway pod logs for "Budget check failed", then RDS
    availability and IAM token expiry. To deliberately restore fail-open
    behaviour: set /adp/${var.environment}/gateway/budget-fail-mode = open and
    `kubectl rollout restart deploy/bedrockgateway -n adp-gateway` (a
    ConfigMap-only apply does NOT change a running pod).
  EOT
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = var.grace_engaged_evaluation_periods
  metric_name         = "BudgetCheckFailOpenGrace"
  namespace           = var.metric_namespace
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    Environment = var.environment
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-budget-check-grace-engaged"
  })
}

# Unexpected exception class in the budget check. This is a CODE DEFECT, not an
# outage: it is deterministic, recurs on every request, and no grace window
# rescues it. The app deliberately fails OPEN on these so a bug cannot
# permanently down all inference — which means spend is uncapped until someone
# fixes the code. Hence a separate, high-severity alarm.
resource "aws_cloudwatch_metric_alarm" "budget_check_unexpected_fault" {
  alarm_name          = "${var.name_prefix}-budget-check-unexpected-fault"
  alarm_description   = <<-EOT
    The budget check raised an unexpected exception type (not a DB/IAM fault).
    This is a code defect. Enforcement is failing OPEN for these requests by
    design — a deterministic bug must not be able to down all inference — so
    spend is UNCAPPED until the defect is fixed. Treat as high severity.
  EOT
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "BudgetCheckFailure"
  namespace           = var.metric_namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  dimensions = {
    Environment = var.environment
    fault_class = "unexpected"
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-budget-check-unexpected-fault"
  })
}

# Grace window has expired and requests are now being denied. Distinct from the
# engaged alarm because the user-visible impact is different: this is a live
# inference outage on every enforced path, not a spend-exposure window.
resource "aws_cloudwatch_metric_alarm" "budget_check_denying" {
  alarm_name          = "${var.name_prefix}-budget-check-denying"
  alarm_description   = <<-EOT
    Budget enforcement is DENYING requests (503 budget_check_unavailable)
    because the spend ledger has been unreadable for longer than the grace
    window. This is a live inference outage on all enforced paths, for all
    tenants — including tenants with no budget configured. Fix the ledger read
    (RDS/IAM), or roll back to fail-open per the runbook in the
    grace-engaged alarm description.
  EOT
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "BudgetCheckFailure"
  namespace           = var.metric_namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  dimensions = {
    Environment = var.environment
    fault_class = "infrastructure"
    outcome     = "denied"
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-budget-check-denying"
  })
}

# The person-level cap layer (Issue #4630) is CONTAINED: a fault in it skips the
# person cap and leaves every other verdict standing, deliberately outside the
# grace window (escalation there fails closed for the whole check, and a
# person-layer-only fault — e.g. its migration missing in an environment — must
# never be able to down all inference). Containment without a page is how hard
# person caps go silently unenforced platform-wide and get discovered from a
# bill, so the skip emits a DEDICATED metric this alarm watches. Dimensioned on
# Environment only, deliberately: it must fire for every fault class.
resource "aws_cloudwatch_metric_alarm" "person_budget_layer_skipped" {
  alarm_name          = "${var.name_prefix}-person-budget-layer-skipped"
  alarm_description   = <<-EOT
    The person-level budget layer faulted and was SKIPPED. Every other budget
    verdict (org hierarchy, run/chain caps) still applies, but person-level
    caps — including hard ones users believe are stopping their agents — are
    NOT being enforced while this fires. Common cause: the person_budget_configs
    migration (034) has not run in this environment. Fix the fault; there is no
    grace window on this path by design.
  EOT
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "PersonBudgetLayerSkipped"
  namespace           = var.metric_namespace
  period              = 60
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions

  dimensions = {
    Environment = var.environment
  }

  tags = merge(var.common_tags, {
    Name = "${var.name_prefix}-person-budget-layer-skipped"
  })
}
