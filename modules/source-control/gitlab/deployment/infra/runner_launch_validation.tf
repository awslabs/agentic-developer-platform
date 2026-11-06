variable "runner_launch_validation" {
  description = "Exact job resources for Auto Scaling's RunInstances permission validation when advancing a template version."
  type = object({
    image_id          = string
    security_group_id = string
    subnet_ids        = set(string)
  })
  default = null
  validation {
    condition = var.runner_launch_validation == null ? true : (
      var.runner_image_update != null &&
      can(regex("^ami-[0-9a-f]{8,17}$", var.runner_launch_validation.image_id)) &&
      can(regex("^sg-[0-9a-f]{8,17}$", var.runner_launch_validation.security_group_id)) &&
      length(var.runner_launch_validation.subnet_ids) > 0 &&
      alltrue([for id in var.runner_launch_validation.subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", id))])
    )
    error_message = "Bind launch validation to the reviewed template, exact AMI, security group and private subnets."
  }
}

# UpdateAutoScalingGroup performs an EC2 RunInstances DryRun with the caller's
# permissions. These grants also permit an equivalent direct job-host launch;
# they are restricted to the reviewed template and its exact input resources.
# No profile is present, so PassRole is unnecessary.
resource "aws_iam_role_policy" "runner_launch_validation" {
  count = var.runner_launch_validation == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-runner-launch-validation"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "BoundJobLaunchInputs"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = concat([
          "arn:aws:ec2:${var.aws_region}:${var.account_id}:launch-template/${var.runner_image_update.launch_template_id}",
          "arn:aws:ec2:${var.aws_region}::image/${var.runner_launch_validation.image_id}",
          "arn:aws:ec2:${var.aws_region}:${var.account_id}:security-group/${var.runner_launch_validation.security_group_id}",
          "arn:aws:ec2:${var.aws_region}:${var.account_id}:network-interface/*",
          "arn:aws:ec2:${var.aws_region}:${var.account_id}:volume/*",
        ], [for id in sort(tolist(var.runner_launch_validation.subnet_ids)) : "arn:aws:ec2:${var.aws_region}:${var.account_id}:subnet/${id}"])
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.aws_region }
          ArnEquals    = { "ec2:LaunchTemplate" = "arn:aws:ec2:${var.aws_region}:${var.account_id}:launch-template/${var.runner_image_update.launch_template_id}" }
        }
      },
      {
        Sid      = "BoundNoProfileJobInstance"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:${var.aws_region}:${var.account_id}:instance/*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = var.aws_region, "ec2:InstanceType" = "t3.large", "ec2:MetadataHttpTokens" = "required" }
          ArnEquals    = { "ec2:LaunchTemplate" = "arn:aws:ec2:${var.aws_region}:${var.account_id}:launch-template/${var.runner_image_update.launch_template_id}" }
          Null         = { "ec2:InstanceProfile" = "true" }
        }
      },
      {
        Sid      = "BoundJobLaunchTags"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "arn:aws:ec2:${var.aws_region}:${var.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "aws:RequestedRegion"        = var.aws_region
            "ec2:CreateAction"           = "RunInstances"
            "aws:RequestTag/Component"   = "gitlab-job"
            "aws:RequestTag/Environment" = var.environment
            "aws:RequestTag/GroupId"     = tostring(var.runner_image_update.group_id)
          }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Component", "Environment", "GroupId", "LaunchTemplateId"] }
        }
      },
    ]
  })
}
