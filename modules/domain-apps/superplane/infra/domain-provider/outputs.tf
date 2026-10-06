output "provider_role_arn" { value = aws_iam_role.provider.arn }
output "provider_role_id" { value = aws_iam_role.provider.unique_id }
output "child_boundary_arn" { value = aws_iam_policy.child_boundary.arn }
output "child_boundary_document" { value = local.child_boundary }
output "execution_policy_document" { value = local.execution_policy }
output "external_id_secret_arn" { value = aws_secretsmanager_secret.external_id.arn }
output "external_id_secret_version" { value = aws_secretsmanager_secret_version.external_id.version_id }
output "execution_policy_configured" {
  description = "Only policy preparation state; not enrollment, authority admission, workspace readiness or live acceptance."
  value       = var.execution != null
}
output "workspace_runtime_variables" { value = { workspace_role_permissions_boundary_arn = aws_iam_policy.child_boundary.arn } }
output "state_key_convention" { value = "${var.environment}/modules/superplane-domain-provider/v1/${var.org_id}/${var.workspace_id}/terraform.tfstate" }

output "managed_policy_arns" { value = sort([for policy in aws_iam_policy.execution : policy.arn]) }
output "managed_policy_documents" { value = { for name, policy in aws_iam_policy.execution : name => jsondecode(policy.policy) } }
