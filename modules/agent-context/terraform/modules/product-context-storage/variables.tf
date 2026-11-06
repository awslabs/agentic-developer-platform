variable "name_prefix" { type = string }
variable "bucket_name" { type = string }
variable "gateway_role_arn" { type = string }
variable "gateway_role_name" { type = string }
variable "alias_prefix" {
  type    = string
  default = "adp-context"
  validation {
    condition     = can(regex("^[a-zA-Z0-9/_-]+$", var.alias_prefix))
    error_message = "Use a KMS alias prefix without the leading alias/."
  }
}
variable "managed_product_keys" {
  description = "Optional dedicated product keys. Map labels are Terraform identities; scope is the tenant/product pair."
  type        = map(object({ tenant_id = string, product_id = string }))
  default     = {}
  validation {
    condition = length(distinct([for v in values(var.managed_product_keys) : "${v.tenant_id}/${v.product_id}"])) == length(var.managed_product_keys) && alltrue([
      for v in values(var.managed_product_keys) : can(regex("^[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}$", v.tenant_id)) && can(regex("^[A-Za-z0-9][A-Za-z0-9._@:-]{0,127}$", v.product_id))
    ])
    error_message = "Each managed key needs a unique, safe tenant/product scope."
  }
}
variable "upload_origins" {
  type        = list(string)
  description = "Exact dashboard HTTPS origins allowed to upload directly to S3."
  default     = []
  validation {
    condition     = alltrue([for origin in var.upload_origins : can(regex("^https://[a-zA-Z0-9.-]+(:[0-9]+)?$", origin))])
    error_message = "Use exact HTTPS origins without wildcards, credentials or paths."
  }
}
variable "deny_gateway_direct_access" {
  type        = bool
  default     = false
  description = "Enable after all Gateway replicas use product sessions. Prevents the original Gateway role from accessing managed Product prefixes directly."
}
variable "tags" {
  type    = map(string)
  default = {}
}
