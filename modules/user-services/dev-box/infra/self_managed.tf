# Self-managed, platform-account additions to the Dev Box foundation (#7998).
#
# main.tf is the connection-scoped foundation the ADP-managed workflow
# (.github/workflows/dev-box-deploy.yml) applies into a customer account with
# every identity supplied from outside. When the module is deployed into the
# platform account by scripts/deploy-dev-box.sh (deploy-all.sh --dev-box on),
# the pieces a customer connection would otherwise bring are declared here:
#
#   * the controller runtime identity — an IRSA role for the controller
#     Deployment (k8s/controller.yaml) that may assume the module's fenced
#     controller role and connect to the gateway database with IAM auth; it is
#     the default controller_principal_arn when none is supplied,
#   * the admission policy and template values the gateway seeds through
#     `python -m src.cloud_desktop.seed` (never raw SQL), exported as outputs so
#     the stage passes exactly what Terraform applied,
#   * SSM parameters under /adp/<env>/dev-box/* that both gateway renderers
#     (gateway-deploy.yml and deploy-all.sh Step 4) read into
#     BG_DEV_BOX_DEPLOYMENT_ID and the deployment-wide BG_DEV_BOX_MAX_DEPLOYMENT_*
#     bounds, and that the stage reads back for the release manifest.
#
# Everything is optional for the ADP-managed path: with controller_principal_arn
# set and the defaults below, this file adds only the SSM parameters.

variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "environment must be a short lowercase name (dev, staging, prod)."
  }
}

variable "stage_enabled" {
  description = "Read by deploy-all.sh (not by Terraform): when the environment's dev-box.tfvars sets this true the Dev Box stage runs by default; --dev-box on|off overrides it."
  type        = bool
  default     = false
}

# --- Controller runtime identity -------------------------------------------
variable "eks_oidc_provider_arn" {
  description = "EKS cluster OIDC provider ARN (platform output eks_oidc_provider_arn); required when controller_principal_arn is empty."
  type        = string
  default     = ""
  validation {
    condition     = var.eks_oidc_provider_arn == "" || can(regex("^arn:aws:iam::[0-9]{12}:oidc-provider/oidc\\.eks\\.[a-z0-9-]+\\.amazonaws\\.com/id/[A-Z0-9]+$", var.eks_oidc_provider_arn))
    error_message = "Supply the EKS cluster's OIDC provider ARN."
  }
}
variable "controller_namespace" {
  description = "Namespace of the controller Deployment (k8s/controller.yaml)."
  type        = string
  default     = "adp-gateway"
}
variable "controller_service_account" {
  description = "Service account of the controller Deployment; its IRSA role becomes the controller principal."
  type        = string
  default     = "dev-box-controller"
}
variable "rds_resource_id" {
  description = "Gateway RDS DbiResourceId (gateway infra module.rds.db_instance_resource_id); scopes the controller's rds-db:connect grant. Empty skips the grant."
  type        = string
  default     = ""
}
variable "rds_username" {
  description = "Gateway database user the controller connects as with IAM auth (gateway tfvars rds_username)."
  type        = string
  default     = "bgadmin"
}

# --- Admission policy and template (seeded through the gateway) ------------
variable "policy_org_id" {
  description = "Organization that receives the enabled dev_box_policies row (its organizations.id, e.g. aws-e)."
  type        = string
  default     = ""
}
variable "allowed_sizes" {
  description = "Instance sizes the policy admits; defaults to [instance_size]."
  type        = list(string)
  default     = []
}
variable "max_disk_gb" {
  description = "Largest template disk the policy admits; defaults to workspace_disk_gb."
  type        = number
  default     = 0
}
variable "max_live_boxes" {
  type    = number
  default = 2
}
variable "max_running_boxes" {
  type    = number
  default = 1
}
variable "max_storage_gb" {
  description = "Organization and deployment storage ceiling in GB; defaults to max_live_boxes * max_disk_gb * 2 (a retained disk per box)."
  type        = number
  default     = 0
}
variable "max_running_hours" {
  type    = number
  default = 400
}
variable "budget_usd" {
  description = "Organization and deployment infrastructure budget in USD (decimal string)."
  type        = string
  default     = "250"
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,6})?$", var.budget_usd))
    error_message = "budget_usd must be a non-negative decimal string."
  }
}
variable "max_lease_minutes" {
  type    = number
  default = 600
}
variable "idle_stop_minutes" {
  description = "Idle shutdown in minutes; 0 disables idle stop."
  type        = number
  default     = 60
}
variable "instance_hour_rate_usd" {
  description = "Approved on-demand rate for instance_size (decimal string)."
  type        = string
  default     = "0.192"
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,6})?$", var.instance_hour_rate_usd))
    error_message = "instance_hour_rate_usd must be a non-negative decimal string."
  }
}
variable "instance_hour_rates_usd" {
  description = "Approved on-demand rate per fallback size (decimal strings); a size without an entry inherits instance_hour_rate_usd (#8423)."
  type        = map(string)
  default     = {}
  validation {
    condition     = alltrue([for rate in values(var.instance_hour_rates_usd) : can(regex("^[0-9]+(\\.[0-9]{1,6})?$", rate))])
    error_message = "instance_hour_rates_usd values must be non-negative decimal strings."
  }
}
variable "disk_gb_hour_rate_usd" {
  type    = string
  default = "0.00011"
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,6})?$", var.disk_gb_hour_rate_usd))
    error_message = "disk_gb_hour_rate_usd must be a non-negative decimal string."
  }
}
variable "snapshot_gb_hour_rate_usd" {
  type    = string
  default = "0.00007"
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,6})?$", var.snapshot_gb_hour_rate_usd))
    error_message = "snapshot_gb_hour_rate_usd must be a non-negative decimal string."
  }
}
variable "template_name" {
  type    = string
  default = "assistant"
}
variable "template_profile" {
  type    = string
  default = "crew"
}
variable "image_revision" {
  description = "Image revision tag of image_id (adp:image-revision); published by the image build to SSM and passed through by the stage."
  type        = string
  default     = ""
}
variable "crew_profile_revision" {
  description = "Approved Crew profile revision baked into image_id (adp:profile-revision); empty until a Crew image is built."
  type        = string
  default     = ""
}
variable "crew_model_id" {
  description = "Approved gateway model for the Crew host supervisor; required with gateway_execute_arn and a Crew profile."
  type        = string
  default     = ""
}

locals {
  # The gateway model route and the dashboard ingress need a pinned Crew profile
  # (worker.validate_release, release.create_manifest). Without one, Terraform
  # applies neither, so the manifest, runtime.py and the live IAM/SG agree.
  crew_pinned                  = var.crew_profile_revision != "" && var.crew_model_id != ""
  gateway_execute_arn          = local.crew_pinned ? var.gateway_execute_arn : ""
  dashboard_enabled            = var.dashboard_enabled && var.crew_profile_revision != ""
  self_managed_controller      = var.controller_principal_arn == ""
  controller_principal_arn     = local.self_managed_controller ? aws_iam_role.controller_runtime[0].arn : var.controller_principal_arn
  controller_oidc_issuer       = replace(var.eks_oidc_provider_arn, "/^arn:aws:iam::[0-9]{12}:oidc-provider\\//", "")
  controller_runtime_role_name = "adp-${var.environment}-dev-box-controller-runtime"
  ssm_prefix                   = "/adp/${var.environment}/dev-box"
  allowed_sizes                = length(var.allowed_sizes) > 0 ? var.allowed_sizes : [var.instance_size]
  instance_hour_rates          = { for size in local.fallback_sizes : size => lookup(var.instance_hour_rates_usd, size, var.instance_hour_rate_usd) }
  private_subnets              = [for id in local.launch_subnet_ids : { subnet_id = id, availability_zone = data.aws_subnet.launch[id].availability_zone }]
  max_disk_gb                  = var.max_disk_gb > 0 ? var.max_disk_gb : var.workspace_disk_gb
  max_storage_gb               = var.max_storage_gb > 0 ? var.max_storage_gb : var.max_live_boxes * local.max_disk_gb * 2
  policy_spec = {
    org_id                    = var.policy_org_id
    deployment_id             = var.deployment_id
    enabled                   = true
    max_live_boxes            = var.max_live_boxes
    max_running_boxes         = var.max_running_boxes
    allowed_sizes             = local.allowed_sizes
    max_disk_gb               = local.max_disk_gb
    max_storage_gb            = local.max_storage_gb
    max_running_hours         = var.max_running_hours
    budget_usd                = var.budget_usd
    max_lease_minutes         = var.max_lease_minutes
    idle_stop_minutes         = var.idle_stop_minutes > 0 ? var.idle_stop_minutes : null
    instance_hour_rate_usd    = var.instance_hour_rate_usd
    disk_gb_hour_rate_usd     = var.disk_gb_hour_rate_usd
    snapshot_gb_hour_rate_usd = var.snapshot_gb_hour_rate_usd
    template = {
      name           = var.template_name
      revision       = var.template_revision
      profile        = var.template_profile
      instance_size  = var.instance_size
      disk_gb        = var.workspace_disk_gb
      image_ref      = var.image_id
      tools_revision = var.source_revision
      approved       = true
    }
  }
  # Inputs scripts/self_managed_release.py turns into the release manifest
  # (release.create_manifest); the stage adds the source revision, the hashes and
  # the correlation at run time.
  release_context = {
    account_id                     = var.account_id
    environment                    = var.environment
    region                         = var.region
    deployment_id                  = var.deployment_id
    image_id                       = var.image_id
    image_revision                 = var.image_revision
    template_revision              = var.template_revision
    instance_size                  = var.instance_size
    workspace_disk_gb              = var.workspace_disk_gb
    allowed_sizes                  = local.allowed_sizes
    fallback_sizes                 = local.fallback_sizes
    instance_hour_rates_usd        = local.instance_hour_rates
    max_disk_gb                    = local.max_disk_gb
    private_subnet_id              = var.private_subnet_id
    private_subnets                = local.private_subnets
    gateway_security_group_id      = var.gateway_security_group_id
    ssm_endpoint_security_group_id = var.ssm_endpoint_security_group_id
    controller_principal_arn       = local.controller_principal_arn
    kms_key_arn                    = var.kms_key_arn
    dashboard_enabled              = local.dashboard_enabled
    run_table_arn                  = var.run_table_arn
    run_table_kms_key_arn          = var.run_table_kms_key_arn
    gateway_execute_arn            = local.gateway_execute_arn
    crew_profile_revision          = var.crew_profile_revision
    crew_model_id                  = var.crew_model_id
    crew_bootstrap                 = var.crew_bootstrap
    crew_bundle_s3_uri             = local.crew_user_data_bootstrap ? var.crew_bundle_s3_uri : ""
    crew_bundle_sha256             = local.crew_user_data_bootstrap ? var.crew_bundle_sha256 : ""
    crew_source_revision           = local.crew_user_data_bootstrap ? var.crew_source_revision : ""
  }
}

resource "aws_iam_role" "controller_runtime" {
  count = local.self_managed_controller ? 1 : 0
  name  = local.controller_runtime_role_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = var.eks_oidc_provider_arn }
      Condition = {
        StringEquals = {
          "${local.controller_oidc_issuer}:sub" = "system:serviceaccount:${var.controller_namespace}:${var.controller_service_account}"
          "${local.controller_oidc_issuer}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
  tags = merge(local.tags, { "adp:controller-namespace" = var.controller_namespace })
  lifecycle {
    precondition {
      condition     = var.eks_oidc_provider_arn != ""
      error_message = "A self-managed controller needs eks_oidc_provider_arn (or supply controller_principal_arn)."
    }
  }
}

# The owner's Crew host-lease mirror lives in the gateway's chat-context table
# (PK crew#<tenant>#<user> / SK executor). When a box is fenced on stop, the
# controller pod rewrites that item to crewLeaseState=fenced with the pod's OWN
# credentials (src/cloud_desktop/host_fencing.py -> src/crew/host_lease.py
# clear_host_lease_mirror), not through the assumed controller role, so the grant
# has to sit on this runtime role. Issue #9620: dev 2026-11-05 08:59Z logged
# AccessDeniedException on PutItem and the mirror kept the pre-stop lease. The
# shape mirrors the gateway's CrewHostLeaseWrite (agent-factory
# gateway-intake-access.tf) minus DeleteItem; the table is SSE-KMS with a customer
# key, so the write also needs that key through DynamoDB only. Both inputs empty
# keeps the policy exactly as before.
variable "chat_context_table_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.chat_context_table_arn == "" || can(regex("^arn:aws:dynamodb:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:table/[A-Za-z0-9_.-]+$", var.chat_context_table_arn))
    error_message = "chat_context_table_arn must be a DynamoDB table ARN or empty."
  }
}

variable "chat_context_kms_key_arn" {
  type    = string
  default = ""
  validation {
    condition     = var.chat_context_kms_key_arn == "" || can(regex("^arn:aws:kms:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:key/[0-9a-f-]{36}$", var.chat_context_kms_key_arn))
    error_message = "chat_context_kms_key_arn must be a KMS key ARN or empty."
  }
}

resource "aws_iam_role_policy" "controller_runtime" {
  count = local.self_managed_controller ? 1 : 0
  name  = "assume-fenced-controller"
  role  = aws_iam_role.controller_runtime[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{ Sid = "AssumeDevBoxController", Effect = "Allow", Action = ["sts:AssumeRole"], Resource = aws_iam_role.controller.arn }],
      var.rds_resource_id == "" ? [] : [{
        Sid      = "GatewayDatabaseIamAuth"
        Effect   = "Allow"
        Action   = ["rds-db:connect"]
        Resource = "arn:aws:rds-db:${var.region}:${var.account_id}:dbuser:${var.rds_resource_id}/${var.rds_username}"
      }],
      var.chat_context_table_arn == "" ? [] : [{
        Sid       = "CrewHostLeaseMirrorFence"
        Effect    = "Allow"
        Action    = ["dynamodb:PutItem"]
        Resource  = var.chat_context_table_arn
        Condition = { "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["crew#*"] } }
      }],
      var.chat_context_table_arn == "" || var.chat_context_kms_key_arn == "" ? [] : [{
        Sid       = "CrewHostLeaseMirrorKms"
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey"]
        Resource  = var.chat_context_kms_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "dynamodb.${var.region}.amazonaws.com" } }
      }]
    )
  })
  lifecycle {
    precondition {
      condition     = var.chat_context_table_arn == "" || startswith(var.chat_context_table_arn, "arn:aws:dynamodb:${var.region}:${var.account_id}:table/")
      error_message = "The chat-context table must belong to the approved account and region."
    }
    precondition {
      condition     = var.chat_context_kms_key_arn == "" || startswith(var.chat_context_kms_key_arn, "arn:aws:kms:${var.region}:${var.account_id}:key/")
      error_message = "The chat-context KMS key must belong to the approved account and region."
    }
  }
}

# --- SSM handoff ------------------------------------------------------------
# Read by the gateway renderers (parity test modules/gateway/tests/features/
# test_dev_box_flag_parity.py) and by scripts/deploy-dev-box.sh. An
# environment without the module has none of these; both renderers then render
# empty values and the gateway keeps Dev Box admission disabled.
resource "aws_ssm_parameter" "stage" {
  for_each = {
    "deployment-id"                = var.deployment_id
    "max-deployment-live-boxes"    = tostring(var.max_live_boxes)
    "max-deployment-running-boxes" = tostring(var.max_running_boxes)
    "max-deployment-storage-gb"    = tostring(local.max_storage_gb)
    "max-deployment-running-hours" = tostring(var.max_running_hours)
    "max-deployment-budget-usd"    = var.budget_usd
    "policy-org-id"                = var.policy_org_id != "" ? var.policy_org_id : " "
    "launch-template-id"           = aws_launch_template.guest.id
    "launch-template-version"      = tostring(aws_launch_template.guest.latest_version)
    "controller-role-arn"          = aws_iam_role.controller.arn
    "controller-principal-arn"     = local.controller_principal_arn
    "guest-role-arn"               = aws_iam_role.guest.arn
    "guest-security-group-id"      = aws_security_group.guest.id
    "guest-instance-profile-arn"   = aws_iam_instance_profile.guest.arn
    "run-table-arn"                = var.run_table_arn != "" ? var.run_table_arn : " "
    "run-table-kms-key-arn"        = var.run_table_kms_key_arn != "" ? var.run_table_kms_key_arn : " "
    "gateway-execute-arn"          = local.gateway_execute_arn != "" ? local.gateway_execute_arn : " "
    "instance-size"                = var.instance_size
    "workspace-disk-gb"            = tostring(var.workspace_disk_gb)
    "template-revision"            = var.template_revision
    "source-revision"              = var.source_revision
    "crew-model-id"                = var.crew_model_id != "" ? var.crew_model_id : " "
    "ssh-grant-role-arn"           = var.gateway_principal_arn == "" ? " " : aws_iam_role.ssh_grant[0].arn
    # Crew bootstrap mode (#8055): read by both gateway renderers into
    # BG_DEV_BOX_CREW_BOOTSTRAP and by deploy-dev-box.sh as the default for its next run.
    # The bundle digest and profile revision it consumes are script-owned keys the
    # builds publish (crew-bundle-sha256 / crew-bundle-revision from
    # image/bundle-buildspec.yml, profile-revision from image/buildspec.yml), never
    # declared here.
    "crew-bootstrap" = var.crew_bootstrap
  }
  name  = "${local.ssm_prefix}/${each.key}"
  type  = "String"
  value = each.value
  tags  = local.tags
}

output "environment" { value = var.environment }
output "deployment_id" { value = var.deployment_id }
output "account_id" { value = var.account_id }
output "region" { value = var.region }
output "instance_size" { value = var.instance_size }
output "kms_key_arn" { value = var.kms_key_arn }
output "private_subnet_id" { value = var.private_subnet_id }
output "gateway_security_group_id" { value = var.gateway_security_group_id }
output "ssm_endpoint_security_group_id" { value = var.ssm_endpoint_security_group_id }
output "gateway_execute_arn" { value = local.gateway_execute_arn }
output "dashboard_enabled" { value = local.dashboard_enabled }
output "crew_profile_revision" { value = var.crew_profile_revision }
output "crew_model_id" { value = var.crew_model_id }
output "image_revision" { value = var.image_revision }
output "controller_principal_arn" { value = local.controller_principal_arn }
output "controller_runtime_role_arn" { value = local.self_managed_controller ? aws_iam_role.controller_runtime[0].arn : "" }
output "controller_namespace" { value = var.controller_namespace }
output "controller_service_account" { value = var.controller_service_account }
output "policy_spec" { value = local.policy_spec }
output "release_context" { value = local.release_context }
output "ssm_prefix" { value = local.ssm_prefix }
