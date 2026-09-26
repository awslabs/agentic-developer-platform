variable "lane" {
  description = "Reviewed dedicated lane identity and existing private installation resources. No inherited account/network defaults."
  type = object({
    name                     = string
    account_id               = string
    region                   = string
    vpc_id                   = string
    build_subnet_ids         = set(string)
    build_security_group_id  = string
    helper_subnet_id         = string
    helper_security_group_id = string
    helper_ami_id            = string
    helper_instance_type     = string
    source_snapshot_ids      = set(string)
    kms_key_arn              = string
    input_bucket_name        = string
    output_bucket_name       = string
    environment_image        = string
    compute_type             = string
    retention_days           = number
    timeout_minutes          = number
    dispatcher_role_arn      = string
  })
  validation {
    condition     = var.lane.input_bucket_name != var.lane.output_bucket_name && can(regex("^[a-z][a-z0-9-]{2,40}$", var.lane.name)) && can(regex("^[0-9]{12}$", var.lane.account_id)) && can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.lane.region))
    error_message = "Explicit canonical lane name, account and region are required."
  }
  validation {
    condition     = can(regex("@sha256:[a-f0-9]{64}$", var.lane.environment_image)) && var.lane.timeout_minutes >= 20 && var.lane.timeout_minutes <= 180 && var.lane.retention_days >= 30 && length(var.lane.build_subnet_ids) > 0 && length(var.lane.source_snapshot_ids) > 0
    error_message = "Pin the environment image digest and bounded timeout/retention; supply network and source snapshots."
  }
  validation {
    condition     = startswith(var.lane.kms_key_arn, "arn:aws:kms:${var.lane.region}:${var.lane.account_id}:key/") && startswith(var.lane.dispatcher_role_arn, "arn:aws:iam::${var.lane.account_id}:role/")
    error_message = "KMS key and trusted dispatcher must belong to the explicit account/region (commercial AWS lane)."
  }
}

# Input/output buckets deliberately have separate write authorities.
