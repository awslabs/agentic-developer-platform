variable "atlassian_mcp_contract_revision" {
  type        = string
  description = "Revision of the independently verified Atlassian MCP provider plan; empty until qualification."
  default     = ""

  validation {
    condition     = (var.atlassian_mcp_contract_revision == "") == (var.atlassian_mcp_metadata_sha256 == "") && (var.atlassian_mcp_contract_revision == "" || can(regex("^[A-Za-z0-9_.-]{1,128}$", var.atlassian_mcp_contract_revision)))
    error_message = "The Atlassian MCP revision and digest must be supplied together with a bounded revision."
  }
}

variable "atlassian_mcp_metadata_sha256" {
  type        = string
  description = "Digest of verified provider metadata; empty until qualification."
  default     = ""

  validation {
    condition     = var.atlassian_mcp_metadata_sha256 == "" || can(regex("^[0-9a-f]{64}$", var.atlassian_mcp_metadata_sha256))
    error_message = "The Atlassian MCP metadata digest must be SHA-256."
  }
}

resource "aws_secretsmanager_secret" "atlassian_mcp_config" {
  name                    = "adp/${var.environment}/connections/atlassian-mcp/config"
  description             = "Protected, independently qualified Atlassian MCP provider plan"
  recovery_window_in_days = 30
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret" "atlassian_mcp_client" {
  name                    = "adp/${var.environment}/connections/atlassian-mcp/client"
  description             = "Deployment-owned Atlassian MCP DCR client state, never user tokens"
  recovery_window_in_days = 30
  tags                    = local.common_tags
}

resource "aws_secretsmanager_secret_version" "atlassian_mcp_client" {
  secret_id     = aws_secretsmanager_secret.atlassian_mcp_client.id
  secret_string = jsonencode({ state = "unregistered" })

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_ssm_parameter" "atlassian_mcp_config_ref" {
  name  = "/adp/${var.environment}/gateway/atlassian-mcp-config-ref"
  type  = "String"
  value = aws_secretsmanager_secret.atlassian_mcp_config.arn
}

resource "aws_ssm_parameter" "atlassian_mcp_contract_revision" {
  count = var.atlassian_mcp_contract_revision != "" && var.atlassian_mcp_metadata_sha256 != "" ? 1 : 0
  name  = "/adp/${var.environment}/gateway/atlassian-mcp-contract-revision"
  type  = "String"
  value = var.atlassian_mcp_contract_revision
}

resource "aws_ssm_parameter" "atlassian_mcp_metadata_sha256" {
  count = var.atlassian_mcp_contract_revision != "" && var.atlassian_mcp_metadata_sha256 != "" ? 1 : 0
  name  = "/adp/${var.environment}/gateway/atlassian-mcp-metadata-sha256"
  type  = "String"
  value = var.atlassian_mcp_metadata_sha256
}

output "atlassian_mcp_client_secret_arn" {
  description = "Reference to the protected DCR client state; registration updates it outside Terraform."
  value       = aws_secretsmanager_secret.atlassian_mcp_client.arn
}
