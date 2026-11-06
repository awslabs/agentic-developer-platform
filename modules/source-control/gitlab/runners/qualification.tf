variable "qualification_run_id" {
  description = "Optional private qualification run UUID. Use a separate run-owned backend and newly created groups; never change this on a product stack."
  type        = string
  default     = null
  validation {
    condition     = var.qualification_run_id == null ? true : can(regex("^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", var.qualification_run_id))
    error_message = "Qualification requires its retained lowercase run UUID."
  }
}

locals {
  qualification_suffix = var.qualification_run_id == null ? "" : "-q-${substr(replace(var.qualification_run_id, "-", ""), 0, 16)}"
  qualification_tags   = var.qualification_run_id == null ? {} : { QualificationRunId = var.qualification_run_id }
}

resource "terraform_data" "qualification_scope" {
  count = var.qualification_run_id == null ? 0 : 1
  input = var.qualification_run_id
  lifecycle {
    precondition {
      condition = (
        var.deployment_role_arn == null &&
        !contains(keys(var.groups), "37") && !contains(keys(var.groups), "106")
      )
      error_message = "The qualification backend owns only new fixture groups. Preserve product group 37, fixture parent 106, and the existing EKS access entry."
    }
  }
}
