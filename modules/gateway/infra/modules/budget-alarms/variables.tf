variable "environment" {
  description = "Environment name (dev, test, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names"
  type        = string
}

variable "common_tags" {
  description = "Tags applied to all resources"
  type        = map(string)
  default     = {}
}

variable "metric_namespace" {
  description = <<-EOT
    CloudWatch namespace the gateway publishes app EMF metrics under.

    Must match src/shared/metrics.py NAMESPACE ("BedrockGateway"). An alarm
    pointed at a namespace nothing publishes to sits in INSUFFICIENT_DATA
    forever and never fires — which looks identical to "healthy".
  EOT
  type        = string
  default     = "BedrockGateway"
}

variable "alarm_actions" {
  description = <<-EOT
    SNS topic ARNs notified when the grace window engages.

    Empty means the alarm still evaluates and is visible in the console but
    pages nobody. Set this in any environment where uncapped spend matters.
  EOT
  type        = list(string)
  default     = []
}

variable "grace_engaged_evaluation_periods" {
  description = "Consecutive 60s periods of engaged grace before alarming. 1 = page on first engage."
  type        = number
  default     = 1
}
