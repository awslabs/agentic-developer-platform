variable "gateway_principal_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.gateway_principal_arn == "" || can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", var.gateway_principal_arn))
    error_message = "Supply the exact gateway IAM role ARN for the selected deployment."
  }
}

resource "aws_iam_role" "ssh_grant" {
  count                = var.gateway_principal_arn == "" ? 0 : 1
  name_prefix          = "adp-dev-box-ssh-grant-"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = var.gateway_principal_arn }
    }]
  })
  tags = local.tags

  lifecycle {
    precondition {
      condition     = var.gateway_principal_arn == "" || (startswith(var.gateway_principal_arn, "arn:aws:iam::${var.account_id}:role/") && var.gateway_principal_arn != var.controller_principal_arn)
      error_message = "The SSH grant must trust only the gateway role in the approved account, not the controller."
    }
  }
}

resource "aws_iam_role_policy" "ssh_grant" {
  count = var.gateway_principal_arn == "" ? 0 : 1
  role  = aws_iam_role.ssh_grant[0].id
  name  = "tagged-box-ssh-only"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "StartTaggedBoxSession"
        Effect    = "Allow"
        Action    = ["ssm:StartSession"]
        Resource  = "${local.resource_prefix}:instance/*"
        Condition = local.owned
      },
      {
        Sid      = "UseSSHDocument"
        Effect   = "Allow"
        Action   = ["ssm:StartSession"]
        Resource = "arn:aws:ssm:${var.region}::document/AWS-StartSSHSession"
      },
      {
        Sid      = "PushOneOSUserKey"
        Effect   = "Allow"
        Action   = ["ec2-instance-connect:SendSSHPublicKey"]
        Resource = "${local.resource_prefix}:instance/*"
        Condition = merge(local.owned, {
          StringEquals = merge(local.owned.StringEquals, { "ec2:osuser" = "ec2-user" })
        })
      },
    ]
  })
}

# The gateway side of the grant, kept inside this optional module so enabling SSH
# never needs a gateway-infra apply (#8049): the selected gateway role (the last
# path segment of gateway_principal_arn) may assume the grant role and nothing
# else. src/cloud_desktop/ssh_access.py reads the grant ARN from
# BG_DEV_BOX_SSH_GRANT_ROLE_ARN, which both renderers render from SSM
# /adp/<env>/dev-box/ssh-grant-role-arn (self_managed.tf).
resource "aws_iam_role_policy" "gateway_ssh_grant" {
  count = var.gateway_principal_arn == "" ? 0 : 1
  role  = element(split("/", var.gateway_principal_arn), length(split("/", var.gateway_principal_arn)) - 1)
  name  = "adp-dev-box-assume-ssh-grant"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeDevBoxSshGrant"
      Effect   = "Allow"
      Action   = ["sts:AssumeRole"]
      Resource = aws_iam_role.ssh_grant[0].arn
    }]
  })
}

output "ssh_grant_role_arn" {
  value = var.gateway_principal_arn == "" ? "" : aws_iam_role.ssh_grant[0].arn
}
