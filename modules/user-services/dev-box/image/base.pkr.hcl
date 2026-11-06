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
    error_message = "Pin an approved Linux base AMI ID."
  }
}

variable "source_revision" {
  type = string
  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.source_revision))
    error_message = "Pin a full source commit."
  }
}

variable "image_revision" { type = string }
variable "region" { type = string }
variable "subnet_id" { type = string }
variable "build_instance_profile" { type = string }
variable "kms_key_id" { type = string }
variable "security_group_id" {
  description = "Security group for the temporary build instance; empty lets Packer create one. The self-managed build passes the module's SSM-only builder group."
  type        = string
  default     = ""
}

source "amazon-ebs" "dev_box" {
  source_ami                   = var.source_ami
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
  ami_name                    = "adp-dev-box-${var.image_revision}-${var.source_revision}"
  ami_description             = "Clean Dev Box Linux base, source ${var.source_revision}"
  tags = {
    "adp:component"       = "dev-box"
    "adp:source-revision" = var.source_revision
    "adp:image-revision"  = var.image_revision
  }
}

build {
  sources = ["source.amazon-ebs.dev_box"]
  provisioner "file" {
    source      = "${path.root}/prepare_ssh.sh"
    destination = "/tmp/adp-prepare-ssh.sh"
  }
  provisioner "file" {
    source      = "${path.root}/prepare_shell.sh"
    destination = "/tmp/adp-prepare-shell.sh"
  }
  provisioner "file" {
    source      = "${path.root}/publish_shell_model.py"
    destination = "/tmp/adp-publish-shell-model.py"
  }
  provisioner "shell" {
    inline = [
      "sudo bash /tmp/adp-prepare-ssh.sh",
      "sudo install -d -m 0755 -o root -g root /opt/adp/crew",
      "sudo install -m 0700 -o root -g root /tmp/adp-publish-shell-model.py /opt/adp/crew/publish_shell_model.py",
      "sudo bash /tmp/adp-prepare-shell.sh",
      "rm -f /tmp/adp-prepare-ssh.sh /tmp/adp-prepare-shell.sh /tmp/adp-publish-shell-model.py",
    ]
  }
  post-processor "manifest" {
    output = "image-manifest.json"
  }
}
