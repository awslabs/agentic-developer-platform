# Dedicated webhook Lambda code deployment profile (S14 follow-up, #6057).
# Grants exactly UpdateFunctionCode for inventoried functions and archive
# read/write for one S3 prefix. No IAM, EKS, configuration, or invoke access.
# Empty targets produce no role; non-empty targets require every listed
# function's actual execution role to appear in the admitted boundary map.

variable "webhook_code_role_boundaries" {
  type        = map(string)
  default     = {}
  description = "Independent exact webhook execution-role => boundary inventory; never enables generic deployment or EKS admission."
  validation {
    condition = alltrue([for role, boundary in var.webhook_code_role_boundaries :
      can(regex("^arn:aws:iam::[0-9]{12}:role/(adp-|bedrockgw-)[A-Za-z0-9_-]+$", role)) &&
      can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9_/-]+$", boundary)) &&
      !can(regex("trusted-|operator", role))
    ])
    error_message = "Supply exact webhook execution roles and reviewed boundary ARNs, never automation identities."
  }
}

variable "webhook_code_lambda_targets" {
  type        = map(string)
  default     = {}
  description = "Exact Lambda function ARN => expected execution-role ARN. Every execution role must be in webhook_code_role_boundaries. Empty leaves the profile unbound."
  validation {
    condition = alltrue([for fn, role in var.webhook_code_lambda_targets :
      can(regex("^arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+$", fn)) &&
      can(regex("^arn:aws:iam::[0-9]{12}:role/(adp-|bedrockgw-)[A-Za-z0-9_-]+$", role)) &&
      !can(regex("trusted-|operator", role))
    ])
    error_message = "Supply exact Lambda function ARNs and their bounded execution-role ARNs."
  }
}

variable "webhook_code_archive_prefix" {
  type        = string
  default     = ""
  description = "Exact S3 key prefix for webhook code archives (e.g. lambda-artifacts/webhook-ingress). Empty when profile is unbound."
  validation {
    condition     = var.webhook_code_archive_prefix == "" || can(regex("^[a-z0-9][a-z0-9/_-]+$", var.webhook_code_archive_prefix))
    error_message = "Archive prefix must be a valid S3 key prefix without wildcards."
  }
}

locals {
  webhook_code_enabled       = length(var.webhook_code_lambda_targets) > 0 && var.webhook_code_archive_prefix != ""
  webhook_code_function_arns = keys(var.webhook_code_lambda_targets)
  webhook_code_role_arns     = values(var.webhook_code_lambda_targets)
  webhook_code_state_bucket  = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}"
}

resource "aws_iam_role" "webhook_code" {
  count                = local.webhook_code_enabled ? 1 : 0
  name                 = "${var.name_prefix}-trusted-webhook-code"
  max_session_duration = 3600

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRoleWithWebIdentity",
      Principal = { Federated = data.aws_iam_openid_connect_provider.github.arn }
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:${var.repository}:environment:adp-webhook-code-${var.environment}"
      } }
    }]
  })

  lifecycle {
    precondition {
      condition     = data.external.webhook_code_admission[0].result.verified == "true"
      error_message = "Live Lambda execution-role and boundary admission must succeed before creating deployment trust."
    }
    precondition {
      condition     = alltrue([for role in var.webhook_code_lambda_targets : contains(keys(var.webhook_code_role_boundaries), role)])
      error_message = "Every webhook Lambda execution role must be admitted in webhook_code_role_boundaries with a reviewed ceiling."
    }
  }
}

resource "aws_iam_role_policy" "webhook_code_lambda" {
  count = local.webhook_code_enabled ? 1 : 0
  role  = aws_iam_role.webhook_code[0].id
  name  = "reviewed-webhook-code-lambda"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadAndUpdateAdmittedFunctions"
        Effect   = "Allow"
        Action   = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"]
        Resource = sort(local.webhook_code_function_arns)
      },
      {
        Sid      = "ReadWriteCodeArchive"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"]
        Resource = "${local.webhook_code_state_bucket}/${var.webhook_code_archive_prefix}/*"
      },
      {
        Sid       = "ListCodeArchive"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = local.webhook_code_state_bucket
        Condition = { StringLike = { "s3:prefix" = "${var.webhook_code_archive_prefix}/*" } }
      },
      { Sid = "Identity", Effect = "Allow", Action = ["sts:GetCallerIdentity"], Resource = "*" },
    ]
  })
}

resource "aws_iam_role_policy" "webhook_code_ceiling" {
  count = local.webhook_code_enabled ? 1 : 0
  role  = aws_iam_role.webhook_code[0].id
  name  = "webhook-code-explicit-ceiling"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyOutsideCodeDeployment"
        Effect    = "Deny"
        NotAction = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode", "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:ListBucket", "sts:GetCallerIdentity"]
        Resource  = "*"
      },
      {
        Sid      = "DenyAllIAM"
        Effect   = "Deny"
        Action   = ["iam:*"]
        Resource = "*"
      },
      {
        Sid      = "DenyAllEKS"
        Effect   = "Deny"
        Action   = ["eks:*"]
        Resource = "*"
      },
      {
        Sid      = "DenyRoleChaining"
        Effect   = "Deny"
        Action   = ["sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity"]
        Resource = "*"
      },
      {
        Sid    = "DenyUnlistedLambdaMutations"
        Effect = "Deny"
        Action = [
          "lambda:CreateFunction", "lambda:DeleteFunction",
          "lambda:UpdateFunctionConfiguration", "lambda:InvokeFunction",
          "lambda:AddPermission", "lambda:RemovePermission",
          "lambda:PutFunctionEventInvokeConfig", "lambda:UpdateEventSourceMapping",
          "lambda:CreateEventSourceMapping", "lambda:DeleteEventSourceMapping",
          "lambda:PutFunctionConcurrency", "lambda:PublishLayerVersion",
          "lambda:TagResource", "lambda:UntagResource",
        ]
        Resource = "*"
      },
      {
        Sid         = "DenyOtherLambdaTargets"
        Effect      = "Deny"
        Action      = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"]
        NotResource = sort(local.webhook_code_function_arns)
      },
      {
        Sid         = "DenyOtherS3Prefixes"
        Effect      = "Deny"
        Action      = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObject"]
        NotResource = "${local.webhook_code_state_bucket}/${var.webhook_code_archive_prefix}/*"
      },
      {
        Sid         = "DenyOtherBucketListing"
        Effect      = "Deny"
        Action      = ["s3:ListBucket"]
        NotResource = local.webhook_code_state_bucket
      },
      {
        Sid       = "DenyUnapprovedPrefixListing"
        Effect    = "Deny"
        Action    = ["s3:ListBucket"]
        Resource  = "*"
        Condition = { StringNotLike = { "s3:prefix" = "${var.webhook_code_archive_prefix}/*" } }
      },
      {
        Sid      = "DenyTrustedAutomationState"
        Effect   = "Deny"
        Action   = ["s3:*"]
        Resource = "${local.webhook_code_state_bucket}/${var.environment}/trusted-automation/*"
      },
    ]
  })
}

# Verify each admitted Lambda function's actual execution role is in the
# boundary map. Uses the existing verifier with clusters=[] so no cluster
# identities are required or checked.
data "external" "webhook_code_admission" {
  count   = local.webhook_code_enabled ? 1 : 0
  program = ["python3", "${path.module}/verify-workload-inventory.py", "--webhook-code"]
  query = {
    inventory = jsonencode({
      account_id                  = data.aws_caller_identity.current.account_id
      webhook_code_lambda_targets = var.webhook_code_lambda_targets
      webhook_code_archive_prefix = var.webhook_code_archive_prefix
      deployment_manifest         = jsondecode(file("${path.module}/webhook-code-manifest.json"))
      deployment_role_boundaries  = var.webhook_code_role_boundaries
    })
  }
}

output "webhook_code_role_arn" {
  value = local.webhook_code_enabled ? aws_iam_role.webhook_code[0].arn : ""
}
