variable "product_context_storage_enabled" {
  type        = bool
  default     = false
  description = "Provision product-restricted Context storage sessions and encryption. Requires migration 018 and the updated Gateway before use."
}
variable "product_context_managed_keys" {
  type    = map(object({ tenant_id = string, product_id = string }))
  default = {}
}
variable "product_context_upload_origins" {
  type    = list(string)
  default = []
}
variable "product_context_deny_gateway_direct_access" {
  type        = bool
  default     = false
  description = "Final cutover after all Gateway replicas use product sessions."
}

module "product_context_storage" {
  count                      = var.product_context_storage_enabled ? 1 : 0
  source                     = "./modules/product-context-storage"
  name_prefix                = local.name_prefix
  bucket_name                = local.bucket_name
  gateway_role_name          = data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name
  gateway_role_arn           = "arn:${data.aws_partition.ingestion.partition}:iam::${var.account_id}:role/${data.terraform_remote_state.platform.outputs.gateway_service_irsa_role_name}"
  alias_prefix               = "adp-context-${var.environment}"
  managed_product_keys       = var.product_context_managed_keys
  upload_origins             = var.product_context_upload_origins
  deny_gateway_direct_access = var.product_context_deny_gateway_direct_access
  tags                       = var.tags
}

output "product_context_managed_key_arns" {
  value = var.product_context_storage_enabled ? module.product_context_storage[0].managed_key_arns : {}
}
