output "repository_name" { value = module.publication.repository_name }
output "repository_arn" { value = module.publication.repository_arn }
output "repository_url" { value = module.publication.repository_url }
output "build_project_name" { value = module.publication.build_project_name }
output "build_project_arn" { value = module.publication.build_project_arn }
output "build_role_arn" { value = module.publication.build_role_arn }
output "source_prefix" { value = module.publication.source_prefix }
output "build_log_group" { value = module.publication.build_log_group }
output "foundation_check_project_name" { value = module.publication.foundation_check_project_name }
output "foundation_check_project_arn" { value = module.publication.foundation_check_project_arn }
output "foundation_check_role_arn" { value = module.publication.foundation_check_role_arn }
output "foundation_check_source_prefix" { value = module.publication.foundation_check_source_prefix }
output "foundation_check_artifact_prefix" { value = module.publication.foundation_check_artifact_prefix }
output "foundation_check_log_group" { value = module.publication.foundation_check_log_group }

output "namespace" { value = module.foundation.namespace }
output "runtime_role_arn" { value = module.foundation.runtime_role_arn }
output "runtime_verifier_group" { value = module.foundation.runtime_verifier_group }
output "verified_cluster_arn" { value = module.foundation.verified_cluster_arn }
output "private_edge" {
  description = "Private adapter ingress observation for the subsequent prepared-route release."
  value       = module.foundation.private_edge
}

output "observation_runtime" {
  value = module.foundation.observation_runtime
}
