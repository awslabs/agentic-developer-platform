packer {
  required_version = "= 1.16.2"
  required_plugins {
    amazon = {
      version = "= 1.8.3"
      source  = "github.com/hashicorp/amazon"
    }
  }
}

# Values come from the private reviewed build configuration and image receipt.
variable "account_id" { type = string }
variable "region" { type = string }
variable "vpc_id" { type = string }
variable "subnet_id" { type = string }
variable "security_group_id" { type = string }
variable "source_ami" { type = string }
variable "instance_profile" { type = string }
variable "source_revision" { type = string }
variable "operation_id" { type = string }
variable "job_image" { type = string }
variable "helper_image" { type = string }
variable "bootstrap_sha256" { type = string }
variable "images_sha256" { type = string }
variable "image_verifier_sha256" { type = string }
variable "output_directory" { type = string }

locals {
  ownership = {
    Project        = "adp"
    Environment    = "dev"
    Module         = "gitlab-runner-image-builder"
    SourceRevision = var.source_revision
    OperationId    = var.operation_id
  }
}

source "amazon-ebs" "job_host" {
  region                      = var.region
  source_ami                  = var.source_ami
  instance_type               = "t3.large"
  vpc_id                      = var.vpc_id
  subnet_id                   = var.subnet_id
  security_group_id           = var.security_group_id
  associate_public_ip_address = false
  iam_instance_profile        = var.instance_profile
  ssh_username                = "ec2-user"
  ssh_interface               = "session_manager"
  ssh_timeout                 = "15m"
  temporary_key_pair_name     = "adp-gitlab-image-${var.operation_id}"
  ami_name                    = "adp-gitlab-job-${var.source_revision}-${var.operation_id}"
  ami_description             = "Reviewed ADP GitLab job host; live qualification is recorded separately"
  force_deregister            = false
  force_delete_snapshot       = false
  encrypt_boot                = true
  run_tags                    = local.ownership
  run_volume_tags             = local.ownership
  snapshot_tags               = local.ownership
  tags = merge(local.ownership, {
    "adp:gitlab-runner:bootstrap-sha256"      = var.bootstrap_sha256
    "adp:gitlab-runner:images-sha256"         = var.images_sha256
    "adp:gitlab-runner:image-verifier-sha256" = var.image_verifier_sha256
  })
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  launch_block_device_mappings {
    device_name           = "/dev/xvda"
    volume_type           = "gp3"
    volume_size           = 40
    encrypted             = true
    delete_on_termination = true
  }
}

build {
  sources = ["source.amazon-ebs.job_host"]
  provisioner "file" {
    source      = "${path.root}/../templates/job-user-data.sh.tftpl"
    destination = "/tmp/adp-job-host-source"
  }
  provisioner "file" {
    source      = "${path.root}/../bootstrap/verify_images.py"
    destination = "/tmp/adp-job-image-verifier"
  }
  provisioner "file" {
    source      = "${path.root}/scrub-builder.sh"
    destination = "/tmp/adp-image-builder-cleanup"
  }
  provisioner "file" {
    source      = "${path.root}/adp-image-builder-cleanup.service"
    destination = "/tmp/adp-image-builder-cleanup.service"
  }
  provisioner "file" {
    source      = "${path.root}/../../tests/acceptance/host_observation.py"
    destination = "/tmp/adp-host-observation.py"
  }
  provisioner "file" {
    source      = "${path.root}/../../tests/acceptance/host_journal.py"
    destination = "/tmp/adp-host-journal.py"
  }
  provisioner "file" {
    source      = "${path.root}/adp-job-observer.service"
    destination = "/tmp/adp-job-observer.service"
  }
  provisioner "shell" {
    script          = "${path.root}/prepare-job-host.sh"
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E bash '{{ .Path }}'"
    environment_vars = [
      "ADP_JOB_IMAGE=${var.job_image}",
      "ADP_HELPER_IMAGE=${var.helper_image}",
      "ADP_BUILD_ACCOUNT=${var.account_id}",
      "ADP_BUILD_REGION=${var.region}",
      "ADP_BOOTSTRAP_SHA256=${var.bootstrap_sha256}",
      "ADP_IMAGE_VERIFIER_SHA256=${var.image_verifier_sha256}",
    ]
  }
  provisioner "file" {
    direction   = "download"
    source      = "/tmp/adp-job-host-runtime.json"
    destination = "${var.output_directory}/host-runtime.json"
  }
  # Remove the temporary provisioning key only after the final download.
  provisioner "shell" {
    inline = ["sudo rm -f /home/ec2-user/.ssh/authorized_keys /root/.ssh/authorized_keys"]
  }
  post-processor "manifest" {
    output     = "${var.output_directory}/packer-manifest.json"
    strip_path = true
  }
}
