variable "native_credentials_enabled" {
  description = "Provision private native-human credential signing and runtime configuration. This does not provision users, credentials, entitlements or consent."
  type        = bool
  default     = false
}

module "native_credentials" {
  source           = "./modules/native-credentials"
  enabled          = var.native_credentials_enabled
  environment      = var.environment
  account_id       = data.aws_caller_identity.current.account_id
  gateway_role_arn = local.gateway_service_irsa_role_arn
  issuer_origin    = aws_ssm_parameter.frontend_url.value
  approval_origin  = aws_ssm_parameter.frontend_url.value
  tags             = local.common_tags
}
