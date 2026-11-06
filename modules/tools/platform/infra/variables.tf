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

variable "interceptor_lambda_arn" {
  description = "Optional interceptor Lambda ARN; configured by the interceptor release when available."
  type        = string
  default     = ""
  validation {
    condition     = var.interceptor_lambda_arn == "" || (var.interceptor_lambda_arn == "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:adp-tools-${var.environment}-interceptor:live" && var.interceptor_execution_role_arn == "arn:aws:iam::${var.aws_account_id}:role/adp-tools-${var.environment}-interceptor")
    error_message = "Supply the reviewed managed interceptor live alias and execution role in the target account and region."
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
    error_message = "Broker URL must name the same-account, same-region execute-api host and stage granted to the gateway role."
  }
}

variable "credential_target_api_arn" {
  description = "Exact API Gateway execution ARN and stage authorized for the optional broker target."
  type        = string
  default     = ""
  validation {
    condition = var.credential_target_api_arn == "" || can(regex(
      "^arn:aws:execute-api:${var.aws_region}:${var.aws_account_id}:[a-z0-9]+/[A-Za-z0-9_-]+$",
      var.credential_target_api_arn
    ))
    error_message = "Broker target needs a same-account, same-region API stage ARN."
  }
}

variable "gateway_role_arn" {
  description = "Existing gateway execution role from approved bootstrap state."
  type        = string
  validation {
    condition     = var.gateway_role_arn == "arn:aws:iam::${var.aws_account_id}:role/adp-tools-${var.environment}-gateway"
    error_message = "Gateway role must match the bootstrapped tools role."
  }
}

variable "gateway_kms_key_arn" {
  description = "Existing gateway encryption key from approved bootstrap state."
  type        = string
  validation {
    condition     = can(regex("^arn:aws:kms:${var.aws_region}:${var.aws_account_id}:key/[a-f0-9-]+$", var.gateway_kms_key_arn))
    error_message = "Supply the bootstrapped gateway key ARN."
  }
}

variable "assertion_signing_key_arn" {
  description = "Existing assertion verification key from approved bootstrap state."
  type        = string
  default     = ""
  validation {
    condition     = var.assertion_signing_key_arn == "" || can(regex("^arn:aws:kms:${var.aws_region}:${var.aws_account_id}:key/[a-f0-9-]+$", var.assertion_signing_key_arn))
    error_message = "Supply the bootstrapped assertion signing key ARN."
  }
}

variable "shared_tools_function_name" {
  description = "Name of the shared AgentCore tools Lambda (web search, code interpreter, browser) that the gateway exposes as Lambda targets; deployed by modules/tools/agentcore/infra as \"<name>-shared\"."
  type        = string
  default     = ""
}

variable "oauth_provider_secret_ids" {
  description = "Secrets Manager IDs containing client_id and client_secret JSON keys for GitHub, Atlassian, and Slack OAuth applications. Register each provider independently; leave unconfigured providers out of the map."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for provider in keys(var.oauth_provider_secret_ids) : contains(["github", "atlassian", "slack"], provider)])
    error_message = "Only GitHub, Atlassian and Slack OAuth provider secret IDs are supported."
  }
  validation {
    condition     = alltrue([for secret_id in values(var.oauth_provider_secret_ids) : length(trimspace(secret_id)) > 0])
    error_message = "OAuth provider secret IDs must not be empty."
  }
}

variable "oauth_provider_credentials_version" {
  description = "Increase when OAuth client credentials rotate so write-only values are resubmitted to AgentCore Identity."
  type        = number
  default     = 1
  validation {
    condition     = var.oauth_provider_credentials_version >= 1 && floor(var.oauth_provider_credentials_version) == var.oauth_provider_credentials_version
    error_message = "OAuth provider credentials version must be a positive integer."
  }
}

variable "oauth_return_url" {
  description = "Browser-accessible HTTPS gateway return route registered on the OAuth workload identity. Required when provider secrets are configured."
  type        = string
  default     = ""
  validation {
    condition     = var.oauth_return_url == "" || can(regex("^https://[A-Za-z0-9.-]+/(api/)?auth/connections/oauth/return$", var.oauth_return_url))
    error_message = "OAuth return URL must be a browser-accessible HTTPS gateway return route without query or fragment."
  }
}

variable "slack_user_oauth" {
  description = "Use Slack's standard user-only OAuth flow for a new Terraform-owned app. Keep false for an existing native provider until a reviewed migration."
  type        = bool
  default     = false
}

variable "external_oauth_provider_arns" {
  description = "Existing providers managed by Connections. References permit private targets without adopting or changing provider ownership."
  type        = map(string)
  default     = {}
  validation {
    condition = alltrue([for vendor, arn in var.external_oauth_provider_arns :
      contains(["github", "atlassian", "slack"], vendor) &&
      !contains(keys(var.oauth_provider_secret_ids), vendor) &&
      arn == "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-${vendor}-user"
    ])
    error_message = "External OAuth references must be the exact providers in this environment and must not also be managed by this Terraform state."
  }
}

variable "github_mediator_endpoint" {
  description = "Protected IAM private-edge endpoint for the owner-bound GitHub user read; empty disables its gateway target."
  type        = string
  default     = ""
  validation {
    condition     = var.github_mediator_endpoint == "" || can(regex("^https://[A-Za-z0-9-]+\\.execute-api\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9_-]+/internal/v1/tools/github/mcp$", var.github_mediator_endpoint))
    error_message = "GitHub mediator must use the exact private IAM edge route."
  }
}

variable "slack_mediator_endpoint" {
  description = "Protected IAM private-edge endpoint for the owned Slack user reader; empty disables its gateway target."
  type        = string
  default     = ""
  validation {
    condition     = var.slack_mediator_endpoint == "" || can(regex("^https://[A-Za-z0-9-]+\\.execute-api\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9_-]+/internal/v1/tools/slack/mcp$", var.slack_mediator_endpoint))
    error_message = "Slack mediator must use the exact private IAM edge route."
  }
}
variable "jira_mediator_endpoint" {
  description = "Protected IAM private-edge endpoint for the owned Jira user reader; empty disables its gateway target."
  type        = string
  default     = ""
  validation {
    condition     = var.jira_mediator_endpoint == "" || can(regex("^https://[A-Za-z0-9-]+\\.execute-api\\.[a-z0-9-]+\\.amazonaws\\.com/[A-Za-z0-9_-]+/internal/v1/tools/jira/mcp$", var.jira_mediator_endpoint))
    error_message = "Jira mediator must use the exact private IAM edge route."
  }
}

variable "gateway_verifier_role_arns" {
  description = <<-EOT
    Additional IAM role ARNs in the target account permitted to invoke this gateway
    for read-only runtime verification (tools/list) — the environment connection
    roles the orchestration engine assumes when it verifies an operations
    deployment. The environment's operator connection role
    (arn:aws:iam::<account>:role/ADP-Agent-<environment>-platform, see
    BG_PLATFORM_OPERATOR_ROLE_ARNS) is always admitted. Tool invocation remains
    reserved to the façade: the interceptor refuses any request without a façade
    assertion, so admission here grants discovery, not execution.
  EOT
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.gateway_verifier_role_arns : can(regex("^arn:aws(-[a-z]+)*:iam::${var.aws_account_id}:role/[A-Za-z0-9+=,.@_/-]{1,512}$", arn))])
    error_message = "gateway_verifier_role_arns must be IAM role ARNs in the target account."
  }
}
