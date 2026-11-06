variable "runner_image_update" {
  description = "Existing job launch template and ASG for reviewed image replacement; null grants nothing."
  type = object({
    group_id           = number
    launch_template_id = string
    asg_arn            = string
  })
  default = null
  validation {
    condition = var.runner_image_update == null ? true : (var.runner_activation == null ? false : (
      var.runner_image_update.group_id == var.runner_activation.group_id &&
      can(regex("^lt-[0-9a-f]{8,17}$", var.runner_image_update.launch_template_id)) &&
      can(regex("^arn:aws:autoscaling:${var.aws_region}:${var.account_id}:autoScalingGroup:[A-Za-z0-9-]+:autoScalingGroupName/adp-${var.environment}-gitlab-job-${var.runner_image_update.group_id}$", var.runner_image_update.asg_arn))
    ))
    error_message = "Bind the activation group and its exact existing launch template and ASG in this account."
  }
}

# Job hosts carry no instance profile. This grants neither PassRole nor direct
# instance control; the protected workflow pauses and drains before its plan.
resource "aws_iam_role_policy" "runner_image_update" {
  count = var.runner_image_update == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-image-update"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "VersionBoundJobTemplate"
        Effect    = "Allow"
        Action    = "ec2:CreateLaunchTemplateVersion"
        Resource  = "arn:aws:ec2:${var.aws_region}:${var.account_id}:launch-template/${var.runner_image_update.launch_template_id}"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Sid       = "AdvanceBoundJobGroup"
        Effect    = "Allow"
        Action    = "autoscaling:UpdateAutoScalingGroup"
        Resource  = var.runner_image_update.asg_arn
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
    ]
  })
}
