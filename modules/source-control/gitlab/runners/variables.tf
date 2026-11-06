variable "environment" {
  type        = string
  description = "Environment containing the operator-registered group runners."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "Environment must be a lowercase, DNS-safe name."
  }
}

variable "groups" {
  description = "Verified tenant-parent group bindings from the isolated registrar; no token values."
  type = map(object({
    tenant_id        = string
    path             = string
    tags             = list(string)
    access_level     = string
    token_secret_arn = string
  }))

  validation {
    condition = length(var.groups) > 0 && length(var.groups) <= 4 && alltrue([
      for group_id, group in var.groups :
      can(regex("^[1-9][0-9]*$", group_id)) &&
      length(group.tenant_id) > 0 && length(group.tenant_id) <= 256 &&
      group.path == "tenant-${substr(sha256(group.tenant_id), 0, 24)}" &&
      length(group.tags) > 0 && length(group.tags) == length(distinct(group.tags)) &&
      contains(group.tags, "adp-group-${group_id}") &&
      alltrue([for tag in group.tags :
        can(regex("^[A-Za-z0-9_-]{1,64}$", tag)) &&
        (!startswith(tag, "adp-group-") || tag == "adp-group-${group_id}")
      ]) &&
      contains(["not_protected", "ref_protected"], group.access_level) &&
      can(regex("^arn:aws(-us-gov|-cn)?:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:adp/${var.environment}/gitlab-runner/${group_id}-[A-Za-z0-9]{6}$", group.token_secret_arn))
    ])
    error_message = "At most four groups (two t3.large hosts each: aggregate 16 vCPU/64 GiB) are allowed; each needs its canonical ID, tenant binding, tags, access level and exact token ARN."
  }
}

variable "aws_region" {
  type        = string
  description = "Region of the platform cluster and GitLab discovery parameters."

  validation {
    condition     = can(regex("^[a-z]{2}(-gov)?-[a-z]+-[1-9][0-9]*$", var.aws_region))
    error_message = "Use an AWS region name."
  }
}

variable "platform_state" {
  description = "Read-only location of the existing platform state; not a dependency on GitLab instance state."
  type = object({
    bucket = string
    key    = string
    region = string
  })

  validation {
    condition     = var.platform_state.bucket != "" && var.platform_state.key == "${var.environment}/platform/terraform.tfstate" && var.platform_state.region == var.aws_region
    error_message = "Platform state must use the selected environment and AWS region."
  }
}

variable "gitlab_private_zone" {
  type        = string
  description = "Private certificate DNS zone; the runner URL must remain inside it."

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?$", var.gitlab_private_zone))
    error_message = "Use a lowercase DNS zone without a trailing dot."
  }
}

variable "runner_chart_archive" {
  description = "Optional reviewed GitLab Runner 0.94.2 chart archive on the apply host; default is the bundled verified upstream archive."
  type        = string
  default     = ""
  validation {
    condition     = var.runner_chart_archive == "" || endswith(var.runner_chart_archive, ".tgz")
    error_message = "Use a local chart archive path."
  }
}

variable "host_ami_id" {
  description = "Reviewed private Docker host AMI ID for the selected region, checked against the release lock before deployment."
  type        = string
  default     = ""
}

variable "host_instance_type" {
  description = "Reviewed disposable instance type; aggregate quota validation follows with the release lock."
  type        = string
  default     = "t3.large"
  validation {
    condition     = contains(["t3.large"], var.host_instance_type)
    error_message = "Only reviewed host instance types are allowed."
  }
}

variable "job_image_digest" {
  description = "Approved immutable non-root build image; release lock validation is required before activation."
  type        = string
  default     = ""
}

variable "job_endpoints" {
  description = "Existing shared internal GitLab ALB interface; the private S3 cache endpoint is created in runner state."
  type = map(object({
    gitlab_eni_id = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for group_id, endpoints in var.job_endpoints :
      contains(keys(var.groups), group_id) &&
      can(regex("^eni-[0-9a-f]{8,17}$", endpoints.gitlab_eni_id))
    ])
    error_message = "Only a qualified private GitLab ALB interface may be provided."
  }
}

variable "release_lock_file" {
  description = "Private reviewed release-lock JSON, retained only for this apply. Empty uses the bundled source lock."
  type        = string
  default     = ""
}

variable "release_lock_sha256" {
  description = "Reviewed SHA-256 of the private release lock. Required when release_lock_file is set."
  type        = string
  default     = ""
}

variable "manager_activation_enabled" {
  description = "Keep false for the reviewed foundation stage; enable only after scoped IAM policies and runner credentials are ready."
  type        = bool
  default     = true
  validation {
    condition     = !var.manager_activation_enabled || var.runtime_iam_policies_enabled
    error_message = "Manager activation requires the bound runtime IAM policies."
  }
}

variable "runtime_iam_policies_enabled" {
  description = "Keep false while creating the capacity foundation; enable after its ASG and launch-template identifiers can bind exact policies."
  type        = bool
  default     = true
}
