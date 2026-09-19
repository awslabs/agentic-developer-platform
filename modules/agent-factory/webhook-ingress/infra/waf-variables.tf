# =============================================================================
# Webhook WAF source restriction and logging
# =============================================================================
# Defaults preserve prior behaviour: default-Allow with a rate limit only,
# and no logging. Supplying the three IP-set ARNs together is what flips the ACL
# to default-Block.

variable "github_hooks_ipv4_ip_set_arn" {
  type        = string
  description = "ARN of a REGIONAL WAFv2 IP set holding GitHub's published hooks IPv4 ranges. Supply together with the IPv6 and internal-callers ARNs to restrict the stage. All empty (default) leaves it default-Allow; a partial configuration fails planning."
  default     = ""
  nullable    = false
}

variable "github_hooks_ipv6_ip_set_arn" {
  type        = string
  description = "ARN of a REGIONAL WAFv2 IP set holding GitHub's published hooks IPv6 ranges. Required alongside the IPv4 set: GitHub delivers over both families, and a v4-only allowlist silently drops v6 deliveries."
  default     = ""
  nullable    = false
}

variable "internal_callers_ip_set_arn" {
  type        = string
  description = "ARN of a REGIONAL WAFv2 IP set holding the addresses this deployment's own callers present — normally the NAT egress EIPs. Required when restricting: the ACL covers the whole stage, and the stage serves POST /agent/trigger from the NAT address as well as POST /github from GitHub. Omitting it would flip the default to Block and break agent chaining."
  default     = ""
  nullable    = false
}

variable "gitlab_webhook_ip_set_arns" {
  type        = list(string)
  description = "REGIONAL WAFv2 IP sets covering the GitLab server's outbound IPv4/IPv6 addresses. Required when GitLab and WAF source restriction are both enabled because the ACL also covers POST /gitlab. May reuse the internal-callers set if GitLab uses the same NAT. These sets join the stage-wide allowlist; route authentication still applies."
  default     = []
  nullable    = false

  validation {
    condition     = alltrue([for arn in var.gitlab_webhook_ip_set_arns : try(trimspace(arn) != "", false)])
    error_message = "gitlab_webhook_ip_set_arns must not contain null or blank entries."
  }
}

variable "enable_waf_logging" {
  type        = bool
  description = "Create an encrypted aws-waf-logs-* CloudWatch group and attach it to the webhook web ACL. Authentication headers are redacted and request sampling is disabled while logging is enabled. Default false preserves prior logging and sampling behaviour."
  default     = false
  nullable    = false
}

variable "waf_log_retention_days" {
  type        = number
  description = "Retention for the webhook WAF log group when enable_waf_logging is true."
  default     = 30
  nullable    = false

  validation {
    condition     = contains([0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.waf_log_retention_days)
    error_message = "waf_log_retention_days must be a supported CloudWatch Logs retention period (0 means never expire)."
  }
}
