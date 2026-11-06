terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.65.0" }
  }
}

# This root owns only the temporary policy/attachment. The original registrar,
# trust, provider enrollment, workspace and all platform resources keep owners.
variable "permit" { type = any }

data "aws_caller_identity" "current" {}
data "aws_iam_role" "registrar" { name = basename(var.permit.registrar.role_arn) }

locals {
  q        = var.permit.qualification
  a        = var.permit.authority
  cluster  = local.q.cluster_arn
  entry    = "${replace(local.cluster, ":cluster/", ":access-entry/")}/role/${local.a.account_id}/${basename(local.a.role_arn)}/*"
  username = "sp-cleanup-view:${substr(trimprefix(var.permit.permit_id, "sp-cleanup:"), 0, 24)}:{{SessionName}}"
  view     = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSAdminViewPolicy"
  tags = {
    OrgId                     = local.a.org_id
    WorkspaceId               = local.a.workspace_id
    superplane-cleanup-permit = var.permit.permit_id
    superplane-generation     = local.q.generation
  }
  time  = { DateLessThan = { "aws:CurrentTime" = var.permit.expires_at }, DateGreaterThanEquals = { "aws:CurrentTime" = var.permit.created_at } }
  owned = { for k, v in local.tags : "aws:ResourceTag/${k}" => v }
  policy = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid = "CreateOnlyTheGroupFreeVerifier", Effect = "Allow", Action = ["eks:CreateAccessEntry"], Resource = local.cluster
        Condition = merge(local.time, {
          StringEquals = merge({ for k, v in local.tags : "aws:RequestTag/${k}" => v }, {
            "eks:principalArn" = local.a.role_arn, "eks:accessEntryType" = "STANDARD", "eks:username" = local.username
          })
          "ForAllValues:StringEquals" = { "aws:TagKeys" = keys(local.tags) }
          Null                        = { "eks:kubernetesGroups" = "true" }
        })
      },
      {
        Sid       = "ReadOnlyVerifierPolicy", Effect = "Allow", Action = ["eks:AssociateAccessPolicy"], Resource = local.entry
        Condition = merge(local.time, { StringEquals = merge(local.owned, { "eks:policyArn" = local.view, "eks:accessScope" = "cluster", "eks:username" = local.username }), Null = { "eks:kubernetesGroups" = "true" } })
      },
      {
        Sid       = "RemoveOnlyVerifierPolicy", Effect = "Allow", Action = ["eks:DisassociateAccessPolicy"], Resource = local.entry
        Condition = merge(local.time, { StringEquals = merge(local.owned, { "eks:policyArn" = local.view }) })
      },
      {
        Sid       = "RemoveOnlyVerifierEntry", Effect = "Allow", Action = ["eks:DeleteAccessEntry"], Resource = local.entry
        Condition = merge(local.time, { StringEquals = local.owned })
      },
      {
        Sid = "TagOnlyVerifierCreation", Effect = "Allow", Action = ["eks:TagResource"], Resource = local.entry
        Condition = merge(local.time, {
          StringEquals                = { for k, v in local.tags : "aws:RequestTag/${k}" => v }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = keys(local.tags) }
        })
      },
      {
        Sid       = "ObserveVerifierPolicy", Effect = "Allow", Action = ["eks:ListAssociatedAccessPolicies"], Resource = local.entry
        Condition = local.time
      }
    ]
  }
}

resource "terraform_data" "guard" {
  input = var.permit.permit_id
  lifecycle {
    precondition {
      condition = (
        var.permit.kind == "closed-bootstrap-cleanup-v1" && var.permit.status == "active" &&
        var.permit.verifier_policy_arn == local.view &&
        data.aws_caller_identity.current.account_id == local.a.account_id &&
        split(":", data.aws_caller_identity.current.user_id)[0] == var.permit.owner_role_id &&
        data.aws_iam_role.registrar.arn == var.permit.registrar.role_arn &&
        data.aws_iam_role.registrar.unique_id == var.permit.registrar.role_id &&
        timecmp(var.permit.expires_at, timestamp()) > 0 &&
        timecmp(var.permit.expires_at, timeadd(var.permit.created_at, "1h")) <= 0 &&
        var.permit.operation.state == "unknown" && var.permit.operation.budget_state == "retained" &&
        local.a.org_id == local.q.org_id && local.a.workspace_id == local.q.workspace_id &&
        startswith(local.cluster, "arn:aws:eks:${local.a.region}:${local.a.account_id}:cluster/") &&
        startswith(local.a.role_arn, "arn:aws:iam::${local.a.account_id}:role/") &&
        contains([for g in local.q.retained_plan.grants : try(g.principal_arn, "") if g.key == "registrar-entry"], var.permit.registrar.role_arn)
      )
      error_message = "Cleanup permit/account/owner/registrar/expiry differs. Prepare a fresh reviewed permit; never replay an expired plan."
    }
  }
}

resource "aws_iam_policy" "verifier" {
  name       = "sp-closed-cleanup-${substr(trimprefix(var.permit.permit_id, "sp-cleanup:"), 0, 24)}"
  policy     = jsonencode(local.policy)
  tags       = merge(local.tags, { DomainApp = "superplane", ManagedBy = "closed-bootstrap-cleanup" })
  depends_on = [terraform_data.guard]
}

resource "aws_iam_role_policy_attachment" "verifier" {
  role       = data.aws_iam_role.registrar.name
  policy_arn = aws_iam_policy.verifier.arn
}

output "registrar_policy" { value = local.policy }
output "policy_arn" { value = aws_iam_policy.verifier.arn }
