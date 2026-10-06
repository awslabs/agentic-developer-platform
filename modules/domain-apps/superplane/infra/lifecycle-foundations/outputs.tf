output "actor_role_names" {
  value = { for actor, role in aws_iam_role.actor : actor => role.name }
}
output "actor_role_identities" {
  value = { for actor, role in aws_iam_role.actor : actor => { arn = role.arn, role_id = role.unique_id } }
}
output "kms_key_arn" {
  value = aws_kms_key.retained.arn
}
output "kms_key_policy" {
  value = jsondecode(aws_kms_key.retained.policy)
}
output "cluster_name" {
  value = local.cluster_name
}
output "state_key_convention" {
  value = "${var.environment}/modules/superplane-lifecycle-foundations/v1/${var.org_id}/${var.workspace_id}/terraform.tfstate"
}
output "provider_identity_requirements" {
  description = "Review against the selected provider's own identity policy; this module does not modify that policy."
  value = {
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["sts:AssumeRole", "iam:GetRole"], Resource = [for role in aws_iam_role.actor : role.arn] },
      { Effect = "Allow", Action = ["kms:DescribeKey", "kms:CreateGrant"], Resource = aws_kms_key.retained.arn }
    ]
  }
}
output "retained_owner_inventory" {
  value = {
    owner                         = "superplane-lifecycle-foundations"
    org_id                        = var.org_id
    workspace_id                  = var.workspace_id
    actor_role_arns               = [for role in aws_iam_role.actor : role.arn]
    kms_key_arn                   = aws_kms_key.retained.arn
    kms_alias_arn                 = aws_kms_alias.retained.arn
    survives_workspace_retirement = true
  }
}
