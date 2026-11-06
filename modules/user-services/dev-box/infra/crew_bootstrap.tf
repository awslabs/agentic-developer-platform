# Crew bootstrap modes (#8055).
#
#   image      (default) the Crew profile is baked into image_id by image/crew.pkr.hcl;
#              the launch template carries no user data. Nothing in this file applies.
#   user-data  the guest launches from the base AMI and installs the pinned Crew
#              bundle at first boot through EC2 user data rendered from
#              crew-bootstrap.sh.tftpl. The script downloads the bundle, the profile
#              manifest and the root-only helper scripts from the state bucket
#              (dev-box/image-inputs/<bundle-sha256>/, the prefix
#              scripts/build-dev-box-image.sh and the stage upload to), verifies every
#              SHA-256 before use, runs image/install_crew.py with the expected
#              revisions, installs the same helpers crew.pkr.hcl installs, runs a
#              readiness preflight and writes /run/adp/crew-bootstrap.json. The
#              controller (controller/identity.py) reads that receipt over SSM and
#              fails closed without it.
#
# User data carries only S3 object paths, digests and revisions (no secret, token,
# run id or owner data); the guest fetches with its instance profile over the VPC's
# S3 gateway endpoint. Trade-off: a first launch pays the download + install (a
# stopped box that starts again does not), and the running host is no longer
# byte-identical to an immutable image — README "Crew bootstrap modes".

variable "crew_bootstrap" {
  description = "How the Crew profile reaches the guest: image (baked into image_id) or user-data (installed at first boot from the pinned bundle)."
  type        = string
  default     = "image"
  validation {
    condition     = contains(["image", "user-data"], var.crew_bootstrap)
    error_message = "crew_bootstrap must be image or user-data."
  }
}
variable "crew_bundle_s3_uri" {
  description = "user-data mode: s3://<state-bucket>/dev-box/image-inputs/<bundle-sha256>/crew.tar.gz."
  type        = string
  default     = ""
  validation {
    condition     = var.crew_bundle_s3_uri == "" || can(regex("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/dev-box/image-inputs/[0-9a-f]{64}/crew\\.tar\\.gz$", var.crew_bundle_s3_uri))
    error_message = "crew_bundle_s3_uri must be s3://<bucket>/dev-box/image-inputs/<sha256>/crew.tar.gz."
  }
}
variable "crew_manifest_s3_uri" {
  description = "user-data mode: s3://<state-bucket>/dev-box/image-inputs/<bundle-sha256>/profile.json; empty derives it from crew_bundle_s3_uri."
  type        = string
  default     = ""
  validation {
    condition     = var.crew_manifest_s3_uri == "" || can(regex("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/dev-box/image-inputs/[0-9a-f]{64}/profile\\.json$", var.crew_manifest_s3_uri))
    error_message = "crew_manifest_s3_uri must be s3://<bucket>/dev-box/image-inputs/<sha256>/profile.json."
  }
}
variable "crew_bundle_sha256" {
  description = "user-data mode: SHA-256 of crew.tar.gz (also the S3 prefix the inputs live under)."
  type        = string
  default     = ""
  validation {
    condition     = var.crew_bundle_sha256 == "" || can(regex("^[0-9a-f]{64}$", var.crew_bundle_sha256))
    error_message = "crew_bundle_sha256 must be a lowercase hex SHA-256."
  }
}
variable "crew_source_revision" {
  description = "user-data mode: the full commit the Crew bundle was built from (profile.json source_revision); the bootstrap passes it as --expected-source-revision."
  type        = string
  default     = ""
  validation {
    condition     = var.crew_source_revision == "" || can(regex("^[0-9a-f]{40}$", var.crew_source_revision))
    error_message = "crew_source_revision must be a full 40-hex commit."
  }
}

locals {
  crew_user_data_bootstrap = var.crew_bootstrap == "user-data"
  crew_bootstrap_bucket    = try(regex("^s3://([^/]+)/", var.crew_bundle_s3_uri)[0], "")
  crew_bootstrap_prefix    = "s3://${local.crew_bootstrap_bucket}/dev-box/image-inputs/${var.crew_bundle_sha256}"
  crew_manifest_s3_uri     = var.crew_manifest_s3_uri != "" ? var.crew_manifest_s3_uri : "${local.crew_bootstrap_prefix}/profile.json"
  # The root-only helpers crew.pkr.hcl installs next to the profile. The stage uploads
  # them to <prefix>/adp/<name>; user data pins each one to the digest of the
  # checked-out source, so the guest only ever runs the reviewed revision.
  crew_bootstrap_helpers = {
    "install_crew.py"           = filesha256("${path.module}/../image/install_crew.py")
    "write_enrollment.py"       = filesha256("${path.module}/../image/write_enrollment.py")
    "write_host_certificate.py" = filesha256("${path.module}/../image/write_host_certificate.py")
    "verify_crew_ready.py"      = filesha256("${path.module}/../image/verify_crew_ready.py")
    "restore_crew_host.py"      = filesha256("${path.module}/../image/restore_crew_host.py")
    "checkpoint_crew_host.py"   = filesha256("${path.module}/../image/checkpoint_crew_host.py")
    "crew_checkpoint.py"        = filesha256("${path.module}/../../../agent-factory/agent/scripts/crew_checkpoint.py")
  }
  crew_bootstrap_inputs_complete = (var.crew_bundle_s3_uri != "" && var.crew_bundle_sha256 != ""
    && var.crew_source_revision != "" && var.crew_profile_revision != ""
    && strcontains(var.crew_bundle_s3_uri, "/dev-box/image-inputs/${var.crew_bundle_sha256}/")
  && startswith(local.crew_manifest_s3_uri, "${local.crew_bootstrap_prefix}/"))
  crew_bootstrap_user_data = local.crew_user_data_bootstrap ? base64encode(templatefile("${path.module}/crew-bootstrap.sh.tftpl", {
    bundle_s3_uri    = var.crew_bundle_s3_uri
    manifest_s3_uri  = local.crew_manifest_s3_uri
    helper_s3_prefix = "${local.crew_bootstrap_prefix}/adp"
    helper_pins      = join(" ", [for name in sort(keys(local.crew_bootstrap_helpers)) : "${name}=${local.crew_bootstrap_helpers[name]}"])
    bundle_sha256    = var.crew_bundle_sha256
    profile_revision = var.crew_profile_revision
    source_revision  = var.crew_source_revision
    region           = var.region
  })) : null
}

# The platform VPC reaches S3 through a gateway endpoint (a prefix list, not a
# security group), so the guest group needs one more 443 egress in user-data mode.
data "aws_prefix_list" "s3" {
  count = local.crew_user_data_bootstrap ? 1 : 0
  name  = "com.amazonaws.${var.region}.s3"
}

resource "aws_vpc_security_group_egress_rule" "crew_bootstrap_s3" {
  count             = local.crew_user_data_bootstrap ? 1 : 0
  security_group_id = aws_security_group.guest.id
  prefix_list_id    = data.aws_prefix_list.s3[0].id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  description       = "Crew bootstrap inputs (S3 gateway endpoint)"
}

# Read-only, prefix-scoped: the bucket is SSE-S3 (platform/scripts/bootstrap.sh), so
# no KMS grant is needed; the digest pins in user data make the fetch content-addressed.
resource "aws_iam_role_policy" "guest_crew_bootstrap" {
  count = local.crew_user_data_bootstrap ? 1 : 0
  role  = aws_iam_role.guest.id
  name  = "crew-bootstrap-inputs-read"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = ["arn:aws:s3:::${local.crew_bootstrap_bucket}/dev-box/image-inputs/*"]
    }]
  })
}

output "crew_bootstrap" { value = var.crew_bootstrap }
output "crew_bundle_s3_uri" { value = local.crew_user_data_bootstrap ? var.crew_bundle_s3_uri : "" }
output "crew_bundle_sha256" { value = local.crew_user_data_bootstrap ? var.crew_bundle_sha256 : "" }
output "crew_source_revision" { value = local.crew_user_data_bootstrap ? var.crew_source_revision : "" }
output "crew_bootstrap_s3_prefix_list_id" { value = local.crew_user_data_bootstrap ? data.aws_prefix_list.s3[0].id : "" }
output "crew_bootstrap_helper_sha256" { value = local.crew_user_data_bootstrap ? local.crew_bootstrap_helpers : {} }
