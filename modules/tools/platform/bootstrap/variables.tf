variable "aws_account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be the confirmed 12-digit target account."
  }
}

variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "environment" {
  type    = string
  default = "dev"
  validation {
    condition     = contains(["dev", "prod"], var.environment)
    error_message = "environment must be dev or prod."
  }
}

variable "tools_facade_role_arn" {
  description = "The dedicated tools façade role permitted to invoke this gateway."
  type        = string
  validation {
    condition     = var.tools_facade_role_arn == "arn:aws:iam::${var.aws_account_id}:role/adp-${var.environment}-role-tools-facade"
    error_message = "Supply the dedicated tools façade IAM role ARN in the target account."
  }
}

variable "tools_browser_consumer_role_arn" {
  description = "Optional reviewed browser service role that may verify, never sign, queued tools assertions."
  type        = string
  default     = ""
  validation {
    condition     = var.tools_browser_consumer_role_arn == "" || can(regex("^arn:aws:iam::${var.aws_account_id}:role/[a-z0-9-]+-browser-service$", var.tools_browser_consumer_role_arn))
    error_message = "Use only the same-account Browser service role ARN."
  }
}

variable "interceptor_lambda_arn" {
  description = "Optional interceptor Lambda ARN; configured by the interceptor release when available."
  type        = string
  default     = ""
  validation {
    condition     = var.interceptor_lambda_arn == "" || (var.interceptor_execution_role_arn != "" && can(regex("^arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$", var.interceptor_lambda_arn)))
    error_message = "Supply an interceptor Lambda function ARN in the target account and region."
  }
}

variable "interceptor_execution_role_arn" {
  description = "Execution role granted verification only on the assertion signing key; required with an interceptor."
  type        = string
  default     = ""
  validation {
    condition = var.interceptor_execution_role_arn == "" || (
      can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", var.interceptor_execution_role_arn)) &&
      startswith(var.interceptor_execution_role_arn, "arn:aws:iam::${var.aws_account_id}:role/")
    )
    error_message = "Supply an interceptor execution role in the target account."
  }
}

variable "cognito_user_pool_id" {
  description = "Existing ADP login pool; retained for the later interceptor, not an AWS_IAM authorizer."
  type        = string
  default     = ""
}

variable "discovery_url" {
  description = "Existing ADP login discovery endpoint; the AWS_IAM gateway does not create Cognito resources."
  type        = string
  default     = ""
  validation {
    condition     = var.discovery_url == "" || can(regex("^https://[^/]+/[^?#]+$", var.discovery_url))
    error_message = "discovery_url must be an HTTPS discovery endpoint."
  }
}

variable "kms_key_alias" {
  type    = string
  default = ""
  validation {
    condition     = var.kms_key_alias == "" || can(regex("^[a-zA-Z0-9/_-]{1,120}$", var.kms_key_alias))
    error_message = "Supply a KMS alias suffix without the alias/ prefix."
  }
}

variable "targets" {
  description = "Explicit Lambda targets; an empty list creates only the gateway."
  type = list(object({
    name             = string
    lambda_arn       = string
    tool_name        = string
    tool_description = string
    input_properties = optional(list(object({
      name        = string
      type        = string
      description = string
      required    = bool
    })), [])
  }))
  default = []
  validation {
    condition     = length(distinct([for target in var.targets : target.name])) == length(var.targets)
    error_message = "Target names must be unique."
  }
  validation {
    condition = alltrue([for target in var.targets :
      can(regex("^[A-Za-z][A-Za-z0-9_-]{1,63}$", target.name)) &&
      can(regex("^arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+(:[A-Za-z0-9_-]+)?$", target.lambda_arn)) &&
      startswith(target.lambda_arn, "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:")
    ])
    error_message = "Every target needs a valid name and a specific Lambda function ARN."
  }
  validation {
    condition = alltrue([for target in var.targets :
      contains(["websearch", "code-interpreter", "browser"], target.name) || length(target.input_properties) > 0
    ])
    error_message = "Custom Lambda targets must declare a nonempty tool input schema."
  }
}

variable "knowledge_door_endpoint" {
  description = "Private AWS_IAM API endpoint for the knowledge mediator; empty disables knowledge tools. Never point this directly at Door."
  type        = string
  default     = ""
  validation {
    condition     = var.knowledge_door_endpoint == "" || can(regex("^https://[A-Za-z0-9-]+\\.execute-api\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9_-]+/internal/v1/tools/knowledge/mcp$", var.knowledge_door_endpoint))
    error_message = "Supply the private execute-api knowledge mediator endpoint without credentials, a query or fragment."
  }
}

variable "knowledge_door_vpc_id" {
  description = "VPC containing the private knowledge mediator API endpoint."
  type        = string
  default     = ""
}

variable "knowledge_door_subnet_ids" {
  description = "Subnets for AgentCore's managed VPC Lattice connection to the private mediator edge."
  type        = list(string)
  default     = []
}

variable "knowledge_door_security_group_ids" {
  description = "Security groups allowing the private Lattice connection to reach the mediator API endpoint."
  type        = list(string)
  default     = []
}

variable "knowledge_door_routing_domain" {
  description = "Private execute-api VPC endpoint DNS routing domain; must preserve the API hostname and TLS."
  type        = string
  default     = ""
}

variable "credential_target_url" {
  description = "Private HTTPS base URL for the authenticated ADP broker API; empty disables the credential target."
  type        = string
  default     = ""
  validation {
    condition = var.credential_target_url == "" ? var.credential_target_api_arn == "" : (
      can(regex("^https://[a-z0-9]+\\.execute-api\\.${var.aws_region}\\.amazonaws\\.com/[A-Za-z0-9_-]+$", var.credential_target_url)) &&
      var.credential_target_url == format("https://%s.execute-api.%s.amazonaws.com/%s", try(split("/", split(":", var.credential_target_api_arn)[5])[0], ""), var.aws_region, try(split("/", var.credential_target_api_arn)[1], ""))
    )
    error_message = "Use the same-account, same-region broker API host and stage granted to the gateway role."
  }
}

variable "credential_target_api_arn" {
  description = "API Gateway execution ARN and stage hosting only the four broker routes."
  type        = string
  default     = ""
  validation {
    condition = var.credential_target_api_arn == "" || (
      startswith(var.credential_target_api_arn, "arn:aws:execute-api:${var.aws_region}:${var.aws_account_id}:") &&
      can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9]+/[A-Za-z0-9_-]+$", var.credential_target_api_arn))
    )
    error_message = "Supply the same-account, same-region API execution ARN and stage with the broker URL."
  }
}

variable "credential_broker_role_arn" {
  description = "Existing ADP gateway service role allowed to verify signed principal assertions."
  type        = string
  default     = ""
  validation {
    condition = var.credential_target_url == "" ? var.credential_broker_role_arn == "" : (
      startswith(var.credential_broker_role_arn, "arn:aws:iam::${var.aws_account_id}:role/") &&
      can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", var.credential_broker_role_arn))
    )
    error_message = "Supply the existing same-account ADP gateway role when enabling the credential target."
  }
}

variable "gateway_arn" {
  description = "Exact pre-existing AgentCore gateway ARN, reviewed for the bootstrap façade caller policy."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:gateway/[A-Za-z0-9_-]+$", var.gateway_arn))
    error_message = "Supply the reviewed gateway ARN from bootstrap output."
  }
}

variable "enable_interceptor" {
  description = "Opt in to the placeholder interceptor function under reviewed bootstrap authority; no gateway attachment or alias is created."
  type        = bool
  default     = false
  validation {
    condition = !var.enable_interceptor || (
      var.eks_oidc_provider_arn != "" &&
      (var.interceptor_execution_role_arn == "" || var.interceptor_execution_role_arn == "arn:aws:iam::${var.aws_account_id}:role/adp-tools-${var.environment}-interceptor") &&
      (var.interceptor_lambda_arn == "" || (var.interceptor_execution_role_arn != "" && var.interceptor_lambda_arn == "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:adp-tools-${var.environment}-interceptor:live"))
    )
    error_message = "The managed interceptor needs the façade identity and cannot share external interceptor ownership."
  }
}

variable "shared_tools_function_name" {
  description = "Name of the shared AgentCore tools Lambda (web search, code interpreter, browser) that the gateway exposes as Lambda targets; deployed by modules/tools/agentcore/infra as \"<name>-shared\"."
  type        = string
  default     = ""
}

variable "interceptor_reserved_concurrent_executions" {
  description = "Managed interceptor concurrency; -1 preserves the shared account pool. The reviewed dev capacity overlay sets 10."
  type        = number
  default     = -1
  nullable    = false
  validation {
    condition = (
      var.interceptor_reserved_concurrent_executions == -1 ||
      (var.interceptor_reserved_concurrent_executions >= 1 &&
      floor(var.interceptor_reserved_concurrent_executions) == var.interceptor_reserved_concurrent_executions)
    )
    error_message = "Choose -1 for unreserved capacity or a positive integer; zero would disable the interceptor."
  }
}

variable "gateway_internal_api_execution_arn" {
  description = "Execution ARN of the ADP gateway's API Gateway stage (arn:aws:execute-api:<region>:<account>:<api-id>/<stage>) whose /internal/v1/tools/authorize route the façade must invoke."
  type        = string
  default     = ""
  validation {
    condition     = var.gateway_internal_api_execution_arn == "" || can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+/[A-Za-z0-9_-]+$", var.gateway_internal_api_execution_arn))
    error_message = "Provide the API Gateway stage execution ARN (…:<api-id>/<stage>) or leave empty."
  }
}
