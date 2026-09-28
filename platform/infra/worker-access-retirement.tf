# The source role may remain trusted by customers, but must no longer be an
# operator in this environment after the controlled worker migration.
variable "agent_legacy_worker_admin_retired" {
  description = "Remove the legacy worker from this Terraform-owned cluster-admin set after drain. Apply each owning cluster state and verify all external/aws-auth/creator grants before confirming task-source isolation."
  type        = bool
  default     = false
}

variable "agent_authority_legacy_workers_drained" {
  description = "Release assertion that old workers have completed before retiring their platform Kubernetes access."
  type        = bool
  default     = false
}

resource "terraform_data" "worker_access_retirement" {
  input = var.agent_legacy_worker_admin_retired
  lifecycle {
    precondition {
      condition     = !var.agent_legacy_worker_admin_retired || var.agent_authority_legacy_workers_drained
      error_message = "Drain legacy workers before removing their Terraform-owned Kubernetes access."
    }
  }
}
