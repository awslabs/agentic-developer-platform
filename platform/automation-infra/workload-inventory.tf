# This inventory is owned by the operator's independent automation state.
# A workload ceiling is reviewed/provisioned BEFORE its role is admitted here.
# Empty inventory permits no IAM writes or execution through existing services.
variable "deployment_role_boundaries" {
  type        = map(string)
  default     = {}
  description = "Exact workload role ARN => immutable, operator-reviewed permissions boundary ARN. Never deployment/operator identities."
  validation {
    condition = alltrue([for role, boundary in var.deployment_role_boundaries :
      can(regex("^arn:aws:iam::[0-9]{12}:role/(adp-|bedrockgw-)[A-Za-z0-9_-]+$", role)) &&
      can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9_/-]+$", boundary)) &&
      !can(regex("trusted-|operator", role))
    ])
    error_message = "Inventory exact workload roles and boundary policies; no wildcard or automation/operator roles."
  }
}
variable "deployment_execution_resources" {
  type        = set(string)
  default     = []
  description = "Exact Lambda function, CodeBuild project and EC2 instance ARNs whose execution roles have been inventoried and bounded by the operator."
  validation {
    condition = alltrue([for arn in var.deployment_execution_resources :
      can(regex("^arn:aws:(lambda|codebuild|ec2):[a-z0-9-]+:[0-9]{12}:(function:|project/|instance/)[A-Za-z0-9_-]+$", arn))
    ])
    error_message = "Execution targets must be exact Lambda functions, CodeBuild projects or EC2 instances."
  }
}
variable "deployment_managed_policy_arns" {
  type        = set(string)
  default     = []
  description = "Exact mutable workload policy ARNs. Boundary policies and automation policies are always excluded by explicit deny."
  validation {
    condition     = alltrue([for arn in var.deployment_managed_policy_arns : can(regex("^arn:aws:iam::[0-9]{12}:policy/(adp-|bedrockgw-)[A-Za-z0-9_-]+$", arn))])
    error_message = "Inventory exact ADP workload managed policies."
  }
}
resource "aws_iam_role_policy" "deployment_workload_policy_lifecycle" {
  count = length(var.deployment_managed_policy_arns) == 0 ? 0 : 1
  role  = aws_iam_role.deployment.id
  name  = "reviewed-workload-policy-lifecycle"
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["iam:CreatePolicy", "iam:DeletePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:TagPolicy", "iam:UntagPolicy"],
    Resource = sort(tolist(var.deployment_managed_policy_arns))
  }] })
}
resource "aws_iam_role_policy" "deployment_execution_ceiling" {
  role = aws_iam_role.deployment.id
  name = "no-uninventoried-execution"
  policy = jsonencode({ Version = "2012-10-17", Statement = concat([
    # Mutation of an existing executable is equivalent to passing its role.
    # PassRole alone cannot close this route, because updating code does not
    # necessarily perform a fresh PassRole check.
    {
      Effect      = "Deny", Action = ["lambda:CreateFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:InvokeFunction", "codebuild:CreateProject", "codebuild:UpdateProject", "codebuild:StartBuild", "ssm:SendCommand"],
      NotResource = concat(sort(tolist(var.deployment_execution_resources)), ["arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript"])
    },
    {
      Effect = "Deny", Action = ["eks:*"],
      NotResource = length(var.deployment_role_boundaries) == 0 ? ["arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/adp-no-admitted-workloads"] : flatten([for cluster in setunion(var.additional_cluster_names, toset([var.cluster_name])) : [
        "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/${cluster}",
        "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:access-entry/${cluster}/*",
        "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:addon/${cluster}/*",
      ]])
    },
    {
      Effect = "Deny", Action = ["s3:*"], Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}/${var.environment}/trusted-automation/*"
    },
    {
      Effect = "Deny", Action = ["s3:PutBucketPolicy", "s3:DeleteBucketPolicy"], Resource = "arn:aws:s3:::adp-terraform-state-${data.aws_caller_identity.current.account_id}"
    },
    { Effect = "Deny", Action = ["sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity"], Resource = "*" },
    { Effect = "Deny", Action = ["iam:PassRole"], NotResource = length(var.deployment_role_boundaries) > 0 ? keys(var.deployment_role_boundaries) : ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/adp-no-admitted-workloads"] },
  ], []) })
}

# Group by ceiling and chunk long inventories to respect the 6,144 byte managed
# policy and ten-attachment role limits. The ceiling itself is never mutable.
locals {
  bounded_role_groups = flatten([for boundary in distinct(values(var.deployment_role_boundaries)) : [for roles in chunklist([for role, ceiling in var.deployment_role_boundaries : role if ceiling == boundary], 10) : { boundary = boundary, roles = roles }]])
  bounded_role_policies = { for index, group in local.bounded_role_groups : tostring(index) => jsonencode({
    Version = "2012-10-17", Statement = [
      {
        Effect   = "Allow", Action = ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary"],
        Resource = group.roles, Condition = { ArnEquals = { "iam:PermissionsBoundary" = group.boundary } }
      },
      {
        Effect   = "Deny", Action = ["iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:PutRolePermissionsBoundary"],
        Resource = group.roles, Condition = { ArnNotEquals = { "iam:PermissionsBoundary" = group.boundary } }
      },
      { Effect = "Allow", Action = ["iam:DeleteRole", "iam:UpdateRole", "iam:DeleteRolePolicy", "iam:DetachRolePolicy", "iam:TagRole", "iam:UntagRole"], Resource = group.roles },
      {
        # PassRole has no PermissionsBoundary condition key.
        Effect    = "Allow", Action = ["iam:PassRole"], Resource = group.roles,
        Condition = { StringEquals = { "iam:PassedToService" = ["lambda.amazonaws.com", "codebuild.amazonaws.com", "ec2.amazonaws.com", "eks.amazonaws.com", "pods.eks.amazonaws.com", "events.amazonaws.com", "apigateway.amazonaws.com"] } }
      }
    ]
  }) }
}
resource "aws_iam_policy" "deployment_role_lifecycle" {
  for_each = local.bounded_role_policies
  name     = "${var.name_prefix}-deployment-role-lifecycle-${each.key}"
  policy   = each.value
  lifecycle {
    precondition {
      condition     = length(local.bounded_role_policies) <= 8 && length(each.value) <= 6144
      error_message = "The reviewed role inventory exceeds IAM policy quotas; consolidate shared ceilings or split deployment ownership."
    }
  }
}
resource "aws_iam_role_policy_attachment" "deployment_role_lifecycle" {
  for_each   = aws_iam_policy.deployment_role_lifecycle
  role       = aws_iam_role.deployment.name
  policy_arn = each.value.arn
}
# Admission requires existing roles. Onboarding a new role is an operator
# bootstrap operation; automation may subsequently reconcile its bounded policy
# and recreate it with exactly the same required ceiling.
data "aws_iam_role" "admitted_workload" {
  for_each = var.deployment_role_boundaries
  name     = element(split("/", each.key), 1)
  lifecycle {
    postcondition {
      condition     = self.arn == each.key && self.permissions_boundary == each.value
      error_message = "The operator must attach the reviewed ceiling before admitting a workload role to deployment/PassRole."
    }
  }
}

data "external" "workload_admission" {
  count   = length(var.deployment_role_boundaries) == 0 ? 0 : 1
  program = ["python3", "${path.module}/verify-workload-inventory.py", "--terraform"]
  query = {
    inventory = jsonencode({
      account_id                     = data.aws_caller_identity.current.account_id,
      deployment_role_boundaries     = var.deployment_role_boundaries,
      deployment_execution_resources = var.deployment_execution_resources,
      clusters                       = [for name in setunion(var.additional_cluster_names, toset([var.cluster_name])) : { name = name, region = var.aws_region }]
    })
  }
}

variable "deployment_instance_profile_arns" {
  type        = set(string)
  default     = []
  description = "Exact workload instance profiles managed by application Terraform. AddRoleToInstanceProfile also requires PassRole, already restricted to admitted bounded roles."
  validation {
    condition     = alltrue([for arn in var.deployment_instance_profile_arns : can(regex("^arn:aws:iam::[0-9]{12}:instance-profile/(adp-|bedrockgw-)[A-Za-z0-9_-]+$", arn))])
    error_message = "Inventory exact workload instance profiles without wildcards."
  }
}
resource "aws_iam_role_policy" "deployment_instance_profiles" {
  count = length(var.deployment_instance_profile_arns) == 0 ? 0 : 1
  name  = "reviewed-workload-instance-profiles"
  role  = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = ["iam:CreateInstanceProfile", "iam:DeleteInstanceProfile", "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile", "iam:TagInstanceProfile", "iam:UntagInstanceProfile"],
    Resource = sort(tolist(var.deployment_instance_profile_arns))
  }] })
}
