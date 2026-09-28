variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "chat_session_sweeper_dry_run" {
  description = "Log intended chat session cleanup without deleting data; keep true for the initial observation window"
  type        = bool
  default     = true
}

variable "github_org" {
  description = "GitHub organization for optional legacy ARC integration; empty when GitHub is not configured"
  type        = string
  default     = ""
}

variable "github_repo" {
  description = "GitHub repo for repo-scoped runner registration. Requires the GitHub App to have 'Administration: Read and write' on that repo. Leave empty to use org-scoped."
  type        = string
  default     = ""
}

variable "runner_namespace" {
  description = "Kubernetes namespace for ARC runner pods"
  type        = string
  default     = "arc-runners"
}

variable "runner_role_name" {
  description = "Optional factory IRSA role name; upgrades retain the owned role or avoid legacy CodeBuild name collisions"
  type        = string
  default     = ""
}

variable "gateway_deployed" {
  description = "Set true when the Bedrock gateway module has been applied. Enables the agent-factory to read the gateway's Terraform state for the authorizer Lambda ARN."
  type        = bool
  default     = false
}

variable "github_app_dev_installation_id" {
  description = "GitHub App installation ID for the `dev` persona on the target org. The ARC runner scale set uses this to authenticate runner registration. Find it via: gh api /orgs/<org>/installations --jq '.installations[] | select(.app_slug==\"<org>-adp-agent-dev\") | .id'"
  type        = string
  default     = ""
}

variable "enable_github_apps" {
  description = "Whether GitHub Apps have been created and their secrets stored. When false, the secrets data-source lookups and ARC runner module are skipped (fresh deploy without GitHub Apps)."
  type        = bool
  default     = false
}

variable "runner_image" {
  description = "Container image for ARC runner pods (full URI override). When set, takes precedence over runner_image_repo/runner_image_tag. Leave empty to construct dynamically from caller identity + runner_image_repo + runner_image_tag."
  type        = string
  default     = ""
}

variable "runner_image_repo" {
  description = "ECR repository name for the ARC runner image. Used when runner_image is empty to construct the full URI from the deploying account's ECR."
  type        = string
  default     = "adp-arc-runner"
}

variable "runner_image_tag" {
  description = "Tag or tag@sha256 digest for the ARC runner image. Used when runner_image is empty."
  type        = string
  default     = "security27-high-a7611fce1@sha256:6b7b01d0a8e852467ca6a48771550beb14ac09edf213cd3a11ad2bdca4c920eb"
}

variable "enable_public_cfn_bucket" {
  description = "Whether to create the public S3 bucket for CloudFormation templates. Requires account-level S3 Block Public Access to be disabled. Set false in environments with account-level public access blocks."
  type        = bool
  default     = false
}

variable "enable_agent_context_rbac" {
  description = "Whether to create runner RBAC resources in the agent-context namespace. Set false when agent-context module is not deployed (namespace doesn't exist)."
  type        = bool
  default     = false
}

variable "seed_agent_registry" {
  description = "Whether to seed the agent_registry DDB table with the scaledjob-worker entry. Defaults true so a fresh deploy registers the worker role automatically (without it, every agent call 500s with UnregisteredServiceAccountError). The resource's lifecycle { ignore_changes = [item] } tolerates drift after creation, so re-applies are safe even where the item already exists outside state."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# Published WebSocket URL override
# -----------------------------------------------------------------------------
# The value of /adp/<env>/gateway/agent-ws-url is what the frontend build bakes
# into VITE_AGENT_WS_URL, so it is the browser's view of the WebSocket API — not
# necessarily the API's own invoke URL. Those differ as soon as the API is
# fronted by a custom domain, and the parameter is the only place that can say so:
# it is written from here, and a deployment that put the API behind a custom
# domain would otherwise have to overwrite another state's parameter to correct
# it.
#
# Two things this does NOT do, both of which fail as a browser-side error with no
# server-side signal:
#   - It does not create the custom domain or its API mapping. Set this only once
#     `wss://<host>` actually resolves and is mapped, or chat cannot connect.
#   - It does not update the CloudFront CSP. connect-src must list the new origin
#     (`https:` does not cover `wss:`), or the browser refuses the handshake.
#
# Note a custom domain with a root API mapping has no stage segment, so the value
# is `wss://ws.example.com`, not `wss://ws.example.com/<stage>`.

variable "agent_ws_public_url" {
  description = "Overrides the WebSocket URL published to /adp/<env>/gateway/agent-ws-url, which the frontend build reads into VITE_AGENT_WS_URL. Set to wss://<host> when the WebSocket API is fronted by a custom domain. Empty (default) publishes the API's own stage invoke URL."
  type        = string
  default     = ""

  validation {
    condition     = var.agent_ws_public_url == "" || startswith(var.agent_ws_public_url, "wss://")
    error_message = "agent_ws_public_url must start with wss:// — the frontend uses it as a WebSocket URL verbatim."
  }
}

variable "disable_execute_api_endpoint" {
  type        = bool
  description = "Disable the AWS-assigned execute-api hostname on the chat WebSocket API so it answers only on its published custom domain. A WEBSOCKET API supports neither a resource policy nor a WAF web ACL, so that hostname carries no network-layer restriction at all. Default false (the AWS default); set true wherever a custom domain such as ws.<zone> is in use."
  default     = false
}

variable "runner_transport_secret_arns" {
  type        = list(string)
  default     = []
  description = "Exact existing GitHub engine transport secret ARNs retained during the separately authorized cutover; validated by runner IAM."
}

variable "runner_transport_secret_kms_arns" {
  type        = list(string)
  default     = []
  description = "Exact KMS key ARNs encrypting transport secrets; required for decryption under the bounded ceiling. Validated by runner IAM."
}

variable "runner_gateway_execution_arns" {
  type        = list(string)
  default     = null
  description = "Reviewed exact gateway routes. Null derives the existing environment's API/stage from its operator-owned SSM endpoint when gateway_deployed; [] disables transport."
}

variable "gateway_intake_managed_policy" {
  description = "Use an identically scoped managed intake policy when gateway inline-policy quota is exhausted."
  type        = bool
  default     = false
}

variable "arc_controller_image" {
  description = "Digest-pinned maintained ARC controller image override. Empty selects the verified security candidate in this account's adp-arc-controller repository; publish it before applying Helm."
  type        = string
  default     = ""

  validation {
    condition     = var.arc_controller_image == "" || can(regex("^[^@]+@sha256:[a-f0-9]{64}$", var.arc_controller_image))
    error_message = "ARC controller images must use an immutable SHA256 digest."
  }
}
