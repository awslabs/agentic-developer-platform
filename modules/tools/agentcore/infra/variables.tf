variable "enabled" {
  type    = bool
  default = false
}
variable "browser_http_enabled" {
  type    = bool
  default = false
}
variable "browser_admission_enabled" {
  type    = bool
  default = false
}
variable "browser_session_seconds" {
  type    = number
  default = 600
  validation {
    condition     = var.browser_session_seconds >= 1 && var.browser_session_seconds <= 1800 && floor(var.browser_session_seconds) == var.browser_session_seconds
    error_message = "Browser session lease must be 1-1800 whole seconds."
  }
}
variable "browser_service_image" {
  type    = string
  default = ""
  validation {
    condition     = var.browser_service_image == "" || can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[^@:]+@sha256:[a-f0-9]{64}$", var.browser_service_image))
    error_message = "Pin the browser service image to an ECR digest."
  }
}
variable "browser_namespace" {
  type    = string
  default = ""
}
variable "browser_oidc_provider_arn" {
  type    = string
  default = ""
}
variable "browser_oidc_issuer" {
  type    = string
  default = ""
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
variable "subnet_ids" {
  type    = set(string)
  default = []
}
variable "security_group_ids" {
  type    = set(string)
  default = []
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
variable "websearch_gateway_url" {
  description = "Existing IAM-authorized AgentCore Gateway /mcp endpoint with a version-pinned web-search target."
  type        = string
  default     = ""
  validation {
    condition     = var.websearch_gateway_url == "" || can(regex("^https://[a-z0-9-]+\\.gateway\\.bedrock-agentcore\\.[a-z0-9-]+\\.amazonaws\\.com/mcp$", var.websearch_gateway_url))
    error_message = "Supply an AgentCore Gateway HTTPS /mcp endpoint."
  }
}
variable "websearch_gateway_arn" {
  description = "Exact existing gateway ARN for the Lambda caller policy."
  type        = string
  default     = ""
  validation {
    condition     = var.websearch_gateway_arn == "" || can(regex("^arn:aws:bedrock-agentcore:[a-z0-9-]+:[0-9]{12}:gateway/[a-zA-Z0-9-]+$", var.websearch_gateway_arn))
    error_message = "Supply the exact gateway ARN, no wildcard."
  }
}
variable "websearch_target" {
  description = "Name of the existing connectorId web-search target pinned to version 1.2.0."
  type        = string
  default     = ""
  validation {
    condition     = var.websearch_target == "" || can(regex("^[A-Za-z0-9_-]{1,64}$", var.websearch_target))
    error_message = "Supply a target name without wildcards."
  }
}
variable "websearch_enabled" {
  description = "Admit new paid search operations only after the target, permissions and worker routes are qualified."
  type        = bool
  default     = false
}
variable "websearch_create_gateway" {
  description = "Create a dedicated IAM AgentCore Gateway and pinned web-search target instead of reusing a qualified existing one."
  type        = bool
  default     = false
}
variable "websearch_target_includes" {
  type    = list(string)
  default = []
  validation {
    condition     = length(var.websearch_target_includes) <= 100 && alltrue([for domain in var.websearch_target_includes : can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", domain))])
    error_message = "Supply up to 100 lowercase domain names."
  }
}
variable "websearch_target_excludes" {
  type    = list(string)
  default = []
  validation {
    condition     = length(var.websearch_target_excludes) <= 100 && alltrue([for domain in var.websearch_target_excludes : can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", domain))])
    error_message = "Supply up to 100 lowercase domain names."
  }
}
