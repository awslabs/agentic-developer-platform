# Preparation is independent of admission. Normal Terraform applies can create
# the complete protected identity before any gateway/producer/worker is enabled.
# No dev-account plan script or -target list is needed to retain these resources.
variable "agent_authority_prepared" {
  description = "Provision protected worker IAM, registry, signing and RBAC prerequisites without switching running workers or producers. Keep true through activation and rollback."
  type        = bool
  default     = true
}

locals {
  agent_authority_provisioned = var.agent_authority_prepared || var.agent_authority_enabled
  # KEDA versions can treat the pause annotation's presence as the pause. Omit
  # it entirely when admissions are enabled rather than emitting "false".
  agent_worker_pause_annotation = var.agent_worker_admission_paused ? "      annotations:\n        autoscaling.keda.sh/paused: \"true\"" : ""
}

variable "agent_worker_admission_paused" {
  description = "Pause new KEDA worker Jobs during a staged Terraform rollout; existing Jobs are preserved."
  type        = bool
  default     = false
}

variable "agent_legacy_worker_admin_retired" {
  description = "After drain and verified Kubernetes source isolation, exclusively manage legacy role attachments as empty, removing out-of-band AdministratorAccess through Terraform."
  type        = bool
  default     = false
}

resource "aws_iam_role_policy_attachments_exclusive" "legacy_worker" {
  count       = var.agent_legacy_worker_admin_retired ? 1 : 0
  role_name   = aws_iam_role.agent_scaledjob.name
  policy_arns = []
  lifecycle {
    precondition {
      condition     = var.agent_authority_enabled && var.agent_authority_legacy_workers_drained && var.agent_task_source_isolation_confirmed
      error_message = "Retire legacy administrator attachments only after protected activation, legacy drain and verified Kubernetes source isolation."
    }
  }
}

variable "agent_authority_runtime_ready" {
  description = "Release assertion that immutable gateway/worker revisions support run-bound marker/Door/task/archive mediation and existing credential workflows; record canary evidence before enabling."
  type        = bool
  default     = false
}

variable "agent_authority_legacy_workers_drained" {
  description = "Release assertion that legacy workers have finished before gateway-wide authority authentication is enabled. This does not remove legacy IAM or Kubernetes access."
  type        = bool
  default     = false
}

resource "terraform_data" "worker_security_rollout" {
  input = {
    prepared = local.agent_authority_provisioned
    active   = var.agent_authority_enabled
    paused   = var.agent_worker_admission_paused
  }

  lifecycle {
    precondition {
      condition = !var.agent_authority_enabled || var.agent_worker_admission_paused || (
        var.agent_legacy_worker_admin_retired && var.agent_task_source_isolation_confirmed
      )
      error_message = "Keep new worker admissions paused until the legacy role is retired and its Kubernetes isolation is verified."
    }
    precondition {
      condition     = !var.agent_authority_enabled || var.agent_authority_runtime_ready
      error_message = "Protected workers require compatible immutable runtime revisions and marker/Door/task/archive/credential canary evidence (#5195). Preparation can apply with agent_authority_prepared=true and agent_authority_enabled=false."
    }
    precondition {
      condition     = !var.agent_authority_enabled || var.agent_authority_legacy_workers_drained
      error_message = "Finish legacy workers before activating gateway-wide authority authentication; changing only the new worker service account does not preserve legacy broker access."
    }
    precondition {
      condition = !var.agent_authority_enabled || (
        can(regex("@sha256:[0-9a-f]{64}$", var.agent_image)) &&
        contains(var.agent_authority_worker_image_digests, try(regex("sha256:[0-9a-f]{64}$", var.agent_image), ""))
      )
      error_message = "Protected worker launches must pin agent_image to an approved sha256 digest; an approved list does not make a mutable image tag safe."
    }
    precondition {
      condition     = !var.agent_task_source_isolation_confirmed || var.agent_authority_enabled
      error_message = "Task-source isolation cannot activate customer sessions during preparation."
    }
  }
}

output "worker_security_rollout" {
  description = "Terraform-owned worker preparation and activation state; release assertions do not substitute for live verification."
  value = {
    prepared             = local.agent_authority_provisioned
    active               = var.agent_authority_enabled
    admission_paused     = var.agent_worker_admission_paused
    legacy_admin_retired = var.agent_legacy_worker_admin_retired
    service_account      = local.agent_worker_sa_name
    worker_role_arn      = local.agent_worker_role_arn
    protected_role_arn   = local.agent_authority_provisioned ? aws_iam_role.agent_authority_worker[0].arn : null
  }
}
