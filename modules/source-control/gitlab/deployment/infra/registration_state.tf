variable "registration_state_access" {
  description = "Permit only the original GitLab state, platform state read and existing DynamoDB lock/checksum rows."
  type        = bool
  default     = false
}

locals {
  registration_state_bucket_arn = "arn:aws:s3:::adp-terraform-state-${var.account_id}"
  registration_state_key        = "${var.environment}/modules/gitlab/terraform.tfstate"
  registration_lock_id          = "adp-terraform-state-${var.account_id}/${local.registration_state_key}"
  registration_lock_table_arn   = "arn:aws:dynamodb:${var.aws_region}:${var.account_id}:table/adp-terraform-locks"
}

resource "aws_iam_role_policy" "registration_state" {
  count = var.registration_state_access ? 1 : 0
  name  = "adp-${var.environment}-gitlab-registration-state"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DiscoverOriginalBackend", Effect = "Allow", Action = "s3:ListBucket"
        Resource = local.registration_state_bucket_arn
        # Terraform enumerates workspace names even when using default. Listing
        # that prefix grants no object reads in other workspaces/modules.
        Condition = { StringEquals = { "s3:prefix" = [local.registration_state_key, "env:/"] } }
      },
      {
        Sid = "ReadOriginalStates", Effect = "Allow", Action = "s3:GetObject"
        Resource = [
          "${local.registration_state_bucket_arn}/${local.registration_state_key}",
          "${local.registration_state_bucket_arn}/${var.environment}/platform/terraform.tfstate",
        ]
      },
      {
        Sid      = "WriteOnlyGitlabState", Effect = "Allow", Action = "s3:PutObject"
        Resource = "${local.registration_state_bucket_arn}/${local.registration_state_key}"
      },
      {
        Sid      = "DescribeExistingLockTable", Effect = "Allow", Action = "dynamodb:DescribeTable"
        Resource = local.registration_lock_table_arn
      },
      {
        Sid      = "UseOnlyGitlabLockAndChecksum", Effect = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource = local.registration_lock_table_arn
        Condition = {
          "ForAllValues:StringEquals" = {
            "dynamodb:LeadingKeys" = [local.registration_lock_id, "${local.registration_lock_id}-md5"]
          }
          Null = { "dynamodb:LeadingKeys" = "false" }
        }
      },
    ]
  })
}

variable "registration_input_versions" {
  description = "Exact immutable S3 object/version pairs for the reviewed plan, variables and approved root."
  type        = map(string)
  default     = {}
  validation {
    condition = length(var.registration_input_versions) == 0 || (
      length(var.registration_input_versions) == 3 &&
      toset([for arn in keys(var.registration_input_versions) : basename(arn)]) == toset(["tfplan", "variables.tfvars.json", "approved-root.pem"]) && alltrue([
        for arn, version in var.registration_input_versions :
        can(regex("^arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-registration/inputs/[0-9a-f]{64}/(tfplan|variables[.]tfvars[.]json|approved-root[.]pem)$", arn)) &&
        can(regex("^[A-Za-z0-9_.~%-]{1,256}$", version)) && version != "null"
      ])
    )
    error_message = "Bind exactly three content-addressed input objects and real immutable version IDs in the deployment evidence bucket."
  }
}

resource "aws_iam_role_policy" "registration_inputs" {
  count = length(var.registration_input_versions) == 0 ? 0 : 1
  name  = "adp-${var.environment}-gitlab-registration-inputs"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [for arn, version in var.registration_input_versions : {
      Effect    = "Allow", Action = "s3:GetObjectVersion", Resource = arn
      Condition = { StringEquals = { "s3:VersionId" = version } }
    }]
  })
}
