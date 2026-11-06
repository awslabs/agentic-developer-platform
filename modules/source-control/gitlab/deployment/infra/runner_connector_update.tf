variable "runner_connector_update" {
  description = "Exact existing manager SSH/cache rules for reviewed cluster-node source correction; null grants nothing."
  type = object({
    group_id                  = number
    security_group_id         = string
    security_group_rule_id    = string
    cache_security_group_id   = string
    cache_manager_rule_id     = string
    manager_security_group_id = string
  })
  default = null
  validation {
    condition = var.runner_connector_update == null ? true : (var.runner_activation == null ? false : (
      var.runner_connector_update.group_id == var.runner_activation.group_id &&
      can(regex("^sg-[0-9a-f]{8,17}$", var.runner_connector_update.security_group_id)) &&
      can(regex("^sgr-[0-9a-f]{8,17}$", var.runner_connector_update.security_group_rule_id)) &&
      can(regex("^sg-[0-9a-f]{8,17}$", var.runner_connector_update.cache_security_group_id)) &&
      can(regex("^sgr-[0-9a-f]{8,17}$", var.runner_connector_update.cache_manager_rule_id)) &&
      can(regex("^sg-[0-9a-f]{8,17}$", var.runner_connector_update.manager_security_group_id)) &&
      !contains([var.runner_connector_update.security_group_id, var.runner_connector_update.cache_security_group_id], var.runner_connector_update.manager_security_group_id)
    ))
    error_message = "Bind the activation group, exact job/cache rules and their distinct manager source group."
  }
}

resource "aws_iam_role_policy" "runner_connector_update" {
  count = var.runner_connector_update == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-connector-update"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "CorrectBoundJobConnector"
      Effect = "Allow"
      Action = "ec2:ModifySecurityGroupRules"
      Resource = [
        "arn:aws:ec2:${var.aws_region}:${var.account_id}:security-group/${var.runner_connector_update.security_group_id}",
        "arn:aws:ec2:${var.aws_region}:${var.account_id}:security-group-rule/${var.runner_connector_update.security_group_rule_id}",
        "arn:aws:ec2:${var.aws_region}:${var.account_id}:security-group/${var.runner_connector_update.cache_security_group_id}",
        "arn:aws:ec2:${var.aws_region}:${var.account_id}:security-group-rule/${var.runner_connector_update.cache_manager_rule_id}",
        # EC2 authorizes the referenced group as well as the target group/rule.
        # Keep rule ARNs restricted to the two existing destination rules.
        "arn:aws:ec2:${var.aws_region}:${var.account_id}:security-group/${var.runner_connector_update.manager_security_group_id}",
      ]
      Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
    }]
  })
}
