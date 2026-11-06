variable "environment" { type = string }
variable "gateway_role_name" { type = string }
variable "gateway_transport_mode" {
  description = "Operator-selected attachment form; inline supports roles at the managed-policy quota."
  type        = string
  default     = "managed"
  validation {
    condition     = contains(["managed", "inline"], var.gateway_transport_mode)
    error_message = "Select managed or inline after inspecting the existing role's policy capacity."
  }
}
data "aws_partition" "current" {}
locals {
  prefix   = "arn:${data.aws_partition.current.partition}"
  services = ["security", "devops", "transform", "well_architected", "finops"]
}
# One managed attachment avoids consuming five inline policy slots in gateway.
resource "aws_iam_policy" "gateway_transport" {
  name = "adp-${var.environment}-l5-transport"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [for service in local.services : "${local.prefix}:sqs:${var.aws_region}:${var.aws_account_id}:adp-${var.environment}-l5${service == "security" ? "" : "-${replace(service, "_", "-")}"}-work"]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [for service in local.services : "${local.prefix}:ssm:${var.aws_region}:${var.aws_account_id}:parameter/adp/${var.environment}/l5-services/${service}/work-queue-url"]
      }
    ]
  })
}
resource "aws_iam_role_policy_attachment" "gateway_transport" {
  count      = var.gateway_transport_mode == "managed" ? 1 : 0
  role       = var.gateway_role_name
  policy_arn = aws_iam_policy.gateway_transport.arn
}
moved {
  from = aws_iam_role_policy_attachment.gateway_transport
  to   = aws_iam_role_policy_attachment.gateway_transport[0]
}

resource "aws_iam_role_policy" "gateway_transport" {
  count  = var.gateway_transport_mode == "inline" ? 1 : 0
  name   = "adp-${var.environment}-l5-transport"
  role   = var.gateway_role_name
  policy = aws_iam_policy.gateway_transport.policy
}
output "gateway_transport_policy_arn" { value = aws_iam_policy.gateway_transport.arn }
