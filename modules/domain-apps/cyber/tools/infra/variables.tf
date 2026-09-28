variable "enabled" {
  type    = bool
  default = false
}
variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Supply the confirmed AWS account ID."
  }
}
variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "name" {
  type    = string
  default = "adp-cyber-tools"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,48}$", var.name))
    error_message = "Use a short lowercase service name."
  }
}
variable "image_uri" {
  description = "Existing ECR image pinned by sha256; never a tag or placeholder."
  type        = string
  default     = ""
  validation {
    condition     = var.image_uri == "" || can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[^@:]+@sha256:[a-f0-9]{64}$", var.image_uri))
    error_message = "image_uri must be an immutable ECR digest URI."
  }
}
variable "rest_api_id" {
  type    = string
  default = ""
}
variable "tools_parent_resource_id" {
  description = "ID of the existing /tools resource in the shared REST API; this stack creates only /tools/cyber."
  type        = string
  default     = ""
}
variable "api_execution_arn" {
  description = "Existing REST API execution ARN without a stage suffix."
  type        = string
  default     = ""
  validation {
    condition     = var.api_execution_arn == "" || can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+$", var.api_execution_arn))
    error_message = "Use an explicit api_execution_arn without wildcards."
  }
}
variable "stage_name" {
  type    = string
  default = ""
  validation {
    condition     = var.stage_name == "" || can(regex("^[A-Za-z0-9_-]+$", var.stage_name))
    error_message = "Use an explicit stage_name without wildcards."
  }
}
variable "worker_role_arns" {
  description = "Explicit existing IAM roles allowed to POST this route. Policies attach only to these roles."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.worker_role_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", arn))])
    error_message = "Worker identities must be explicit IAM role ARNs, without wildcards."
  }
}
variable "authority_endpoint" {
  description = "HTTPS platform endpoint ending /internal/v1/agent/task; only generic tool-authorize/artifact calls."
  type        = string
  default     = ""
  validation {
    condition     = var.authority_endpoint == "" || can(regex("^https://[^/?#]+(/[^?#]*)?/internal/v1/agent/task$", var.authority_endpoint))
    error_message = "Use the HTTPS Task authority base endpoint, with no query or fragment."
  }
}
variable "authority_invoke_arns" {
  description = "Exactly the stage-qualified POST tool-authorize and artifact execute-api ARNs."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.authority_invoke_arns : can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+/[^/*]+/POST/internal/v1/agent/task/(tool-authorize|artifact)$", arn))])
    error_message = "Only explicit stage-qualified generic authorization/artifact routes are permitted."
  }
}
variable "sample_bucket_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.sample_bucket_arn == "" || can(regex("^arn:aws:s3:::[a-z0-9.-]+$", var.sample_bucket_arn))
    error_message = "Supply an explicit S3 bucket ARN."
  }
}
variable "queue_arns" {
  description = "Existing triage/static FIFO queue ARNs; queue policy owners must separately permit this Lambda role."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.queue_arns : can(regex("^arn:aws:sqs:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_.-]+$", arn))])
    error_message = "Use exact queue_arns without wildcards."
  }
}
variable "results_table_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.results_table_arn == "" || can(regex("^arn:aws:dynamodb:[a-z0-9-]+:[0-9]{12}:table/[A-Za-z0-9_.-]+$", var.results_table_arn))
    error_message = "Use an explicit results_table_arn without wildcards."
  }
}
variable "secret_arns" {
  description = "Exact CAPE/VT secret ARNs, if configured. Never secret values."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.secret_arns : can(regex("^arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+$", arn))])
    error_message = "Use exact secret_arns without wildcards."
  }
}
variable "kms_key_arns" {
  description = "Explicit keys required by configured sample/results/secrets/queues."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.kms_key_arns : can(regex("^arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]+$", arn))])
    error_message = "Use exact kms_key_arns without wildcards."
  }
}
variable "backend_environment" {
  description = "Non-secret backend settings: queue URLs, result table name, CAPE/browser endpoint, secret ARNs."
  type        = map(string)
  default     = {}
  validation {
    condition = alltrue([for key in keys(var.backend_environment) : contains([
      "CYBER_TRIAGE_QUEUE", "CYBER_STATIC_QUEUE", "CYBER_RESULTS_TABLE", "CYBER_CAPE_ALB", "CYBER_CAPE_TOKEN_SECRET",
      "CYBER_VT_TOKEN_SECRET", "TASK_CYBER_BROWSER_ENDPOINT",
      "CYBER_CC_DATABASE", "CYBER_CC_TABLE", "CYBER_CC_WORKGROUP", "CYBER_CC_CRAWLS", "CYBER_CC_REGION",
      "CYBER_CC_QUEUE_SECONDS", "CYBER_CC_EXECUTION_SECONDS"
    ], key)])
    error_message = "Only named non-secret backend configuration is permitted."
  }
}
variable "subnet_ids" {
  type    = set(string)
  default = []
}
variable "security_group_ids" {
  type    = set(string)
  default = []
}
variable "capability_enabled" {
  description = "Admit new cyber operations; keep false until shared API publication and verification are complete. Cleanup remains available."
  type        = bool
  default     = false
}
variable "code_interpreter_enabled" {
  description = "Admit Task Code Interpreter operations after a SANDBOX resource and grants are qualified. Disabled by default."
  type        = bool
  default     = false
}
variable "code_interpreter_identifier" {
  description = "Identifier of an operator-owned, network-isolated AgentCore Code Interpreter."
  type        = string
  default     = ""
  validation {
    condition     = var.code_interpreter_identifier == "" || can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10}$", var.code_interpreter_identifier))
    error_message = "Supply a single Code Interpreter identifier."
  }
}
variable "code_interpreter_arn" {
  description = "Exact ARN of the same AgentCore Code Interpreter; qualify its SANDBOX configuration before enabling."
  type        = string
  default     = ""
  validation {
    condition     = var.code_interpreter_arn == "" || can(regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:code-interpreter-custom/[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10}$", var.code_interpreter_arn))
    error_message = "Supply an exact Code Interpreter ARN, not a wildcard."
  }
}
variable "vpc_id" {
  description = "Optional VPC for a service-owned security group with no ingress and HTTPS egress."
  type        = string
  default     = ""
  validation {
    condition     = var.vpc_id == "" || can(regex("^vpc-[a-f0-9]+$", var.vpc_id))
    error_message = "Use an existing VPC ID."
  }
}
variable "endpoint_security_group_ids" {
  description = "Existing private AWS endpoint security groups; add only HTTPS ingress from this service-owned Lambda group."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for id in var.endpoint_security_group_ids : can(regex("^sg-[a-f0-9]+$", id))])
    error_message = "Use explicit endpoint security group IDs."
  }
}

variable "common_crawl_policy" {
  description = "Scoped Athena/Glue/S3 IAM statements from the existing Common Crawl stack. Empty keeps archive tooling unavailable."
  type        = string
  default     = ""
}
