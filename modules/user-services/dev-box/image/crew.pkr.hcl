packer {
  required_plugins {
    amazon = {
      version = "~> 1.3"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

variable "source_ami" {
  type = string
  validation {
    condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.source_ami))
    error_message = "Pin the approved private Dev Box base image."
  }
}

variable "source_revision" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.source_revision))
    error_message = "Pin the full source commit."
  }
}

variable "image_revision" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", var.image_revision))
    error_message = "Pin a versioned Crew image revision."
  }
}

variable "profile_revision" {
  type = string
  validation {
    condition     = can(regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", var.profile_revision))
    error_message = "Pin the approved Crew profile."
  }
}

variable "crew_bundle_s3_uri" {
  description = "The approved bundle under the state bucket's digest prefix. The build instance fetches it itself (S3 gateway endpoint, instance profile) instead of Packer streaming ~1 GB through the Session Manager tunnel into tmpfs."
  type        = string
  validation {
    condition     = can(regex("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/dev-box/image-inputs/[0-9a-f]{64}/crew\\.tar\\.gz$", var.crew_bundle_s3_uri))
    error_message = "Pin crew_bundle_s3_uri to s3://<bucket>/dev-box/image-inputs/<sha256>/crew.tar.gz."
  }
}
variable "crew_bundle_sha256" {
  description = "Expected SHA-256 of the bundle (profile.json bundle_sha256); verified on the instance before install."
  type        = string
  validation {
    condition     = can(regex("^[0-9a-f]{64}$", var.crew_bundle_sha256))
    error_message = "Pin the bundle digest."
  }
}
variable "crew_manifest_path" { type = string }
variable "region" { type = string }
variable "subnet_id" { type = string }
variable "build_instance_profile" { type = string }
variable "kms_key_id" { type = string }
variable "security_group_id" {
  description = "Security group for the temporary build instance; empty lets Packer create one. The self-managed build passes the module's SSM-only builder group."
  type        = string
  default     = ""
}

source "amazon-ebs" "crew" {
  source_ami                  = var.source_ami
  instance_type               = "t3.small"
  region                      = var.region
  subnet_id                   = var.subnet_id
  associate_public_ip_address = false
  # The platform account enforces IMDSv2 (httpTokensEnforced); the build instance and
  # the resulting AMI both require session tokens, which the guest controller expects.
  imds_support = "v2.0"
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  ssh_interface               = "session_manager"
  ssh_username                = "ec2-user"
  iam_instance_profile        = var.build_instance_profile
  security_group_ids          = var.security_group_id == "" ? null : [var.security_group_id]
  encrypt_boot                = true
  kms_key_id                  = var.kms_key_id
  ami_name                    = "adp-crew-${var.image_revision}-${var.source_revision}"
  ami_description             = "Pinned Crew profile on approved private Dev Box base"
  tags = {
    "adp:component"        = "dev-box"
    "adp:source-revision"  = var.source_revision
    "adp:image-revision"   = var.image_revision
    "adp:profile-revision" = var.profile_revision
  }
}

build {
  sources = ["source.amazon-ebs.crew"]

  provisioner "file" {
    source      = "${path.root}/install_crew.py"
    destination = "/tmp/adp-install-crew.py"
  }

  provisioner "file" {
    source      = "${path.root}/write_enrollment.py"
    destination = "/tmp/adp-write-enrollment.py"
  }

  provisioner "file" {
    source      = "${path.root}/write_host_certificate.py"
    destination = "/tmp/adp-write-host-certificate.py"
  }

  provisioner "file" {
    source      = "${path.root}/verify_crew_ready.py"
    destination = "/tmp/adp-verify-crew-ready.py"
  }

  provisioner "file" {
    source      = "${path.root}/restore_crew_host.py"
    destination = "/tmp/adp-restore-crew-host.py"
  }

  provisioner "file" {
    source      = "${path.root}/checkpoint_crew_host.py"
    destination = "/tmp/adp-checkpoint-crew-host.py"
  }

  provisioner "file" {
    source      = "${path.root}/../../../agent-factory/agent/scripts/crew_checkpoint.py"
    destination = "/tmp/adp-crew-checkpoint.py"
  }

  provisioner "file" {
    source      = var.crew_manifest_path
    destination = "/tmp/adp-crew-profile.json"
  }

  # The bundle is fetched by the instance into disk-backed /var/tmp (AL2023's
  # /tmp is RAM-sized tmpfs) and checked against the pinned digest before the
  # installer runs; the installer re-checks it against the manifest.
  provisioner "shell" {
    inline = [
      "sudo install -d -m 0700 -o root -g root /var/tmp/adp-crew-inputs",
      "sudo aws s3 cp --region ${var.region} --only-show-errors ${var.crew_bundle_s3_uri} /var/tmp/adp-crew-inputs/crew.tar.gz",
      "echo '${var.crew_bundle_sha256}  /var/tmp/adp-crew-inputs/crew.tar.gz' | sudo sha256sum -c --quiet -",
      # Same interpreter choice as infra/crew-bootstrap.sh.tftpl: the bundle's relocatable
      # CPython 3.12 when it carries one, else AL2023's system python3 (3.9).
      "sudo install -d -m 0700 -o root -g root /var/tmp/adp-crew-inputs/python-stage",
      "if sudo tar -tzf /var/tmp/adp-crew-inputs/crew.tar.gz | grep -Eqx 'python/bin/python3(\\.[0-9]+)?'; then sudo tar -xzf /var/tmp/adp-crew-inputs/crew.tar.gz -C /var/tmp/adp-crew-inputs/python-stage --no-same-owner python; fi",
      "GUEST_PYTHON=python3; if sudo test -x /var/tmp/adp-crew-inputs/python-stage/python/bin/python3; then GUEST_PYTHON=/var/tmp/adp-crew-inputs/python-stage/python/bin/python3; fi",
      "echo \"crew image: helpers run on $GUEST_PYTHON\"",
      "sudo \"$GUEST_PYTHON\" /tmp/adp-install-crew.py --manifest /tmp/adp-crew-profile.json --bundle /var/tmp/adp-crew-inputs/crew.tar.gz --expected-source-revision ${var.source_revision} --expected-profile-revision ${var.profile_revision}",
      "openssl version >/dev/null",
      "sudo install -m 0700 -o root -g root /tmp/adp-write-enrollment.py /opt/adp/crew/write_enrollment.py",
      "sudo install -m 0700 -o root -g root /tmp/adp-write-host-certificate.py /opt/adp/crew/write_host_certificate.py",
      "sudo install -m 0700 -o root -g root /tmp/adp-verify-crew-ready.py /opt/adp/crew/verify_crew_ready.py",
      "sudo install -m 0700 -o root -g root /tmp/adp-restore-crew-host.py /opt/adp/crew/restore_crew_host.py",
      "sudo install -m 0700 -o root -g root /tmp/adp-checkpoint-crew-host.py /opt/adp/crew/checkpoint_crew_host.py",
      "sudo install -m 0600 -o root -g root /tmp/adp-crew-checkpoint.py /opt/adp/crew/crew_checkpoint.py",
      # The units and the controller's SSM commands run the installed helpers with the
      # system python3; prove they import there before the image is published.
      "sudo python3 -I /opt/adp/crew/verify_crew_ready.py --help >/dev/null",
      "sudo python3 -I -c 'import sys; sys.path.insert(0, \"/opt/adp/crew\"); import checkpoint_crew_host, restore_crew_host, write_enrollment, write_host_certificate'",
      "sudo unlink /tmp/adp-install-crew.py",
      "sudo unlink /tmp/adp-write-enrollment.py",
      "sudo unlink /tmp/adp-write-host-certificate.py",
      "sudo unlink /tmp/adp-verify-crew-ready.py",
      "sudo unlink /tmp/adp-restore-crew-host.py",
      "sudo unlink /tmp/adp-checkpoint-crew-host.py",
      "sudo unlink /tmp/adp-crew-checkpoint.py",
      "sudo unlink /tmp/adp-crew-profile.json",
      "sudo rm -rf /var/tmp/adp-crew-inputs"
    ]
  }

  post-processor "manifest" {
    output = "crew-image-manifest.json"
  }
}
