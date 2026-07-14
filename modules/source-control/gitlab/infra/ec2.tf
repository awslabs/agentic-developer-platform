# =============================================================================
# GitLab CE Infrastructure — EC2 Instance
# =============================================================================

resource "aws_instance" "gitlab" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = local.private_subnets[0]
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.gitlab.name
  key_name               = var.key_name != "" ? var.key_name : null

  # GitLab is stateful (repos/DB on the root volume, delete_on_termination):
  # replacing the instance on user_data edits would destroy all data. With
  # false, the provider updates user_data via stop/start (~2-3 min outage,
  # volume preserved); the new user_data only executes on a from-scratch
  # rebuild, so config changes must be applied manually via gitlab-ctl
  # reconfigure (see issue #3588 Deployment notes).
  user_data_replace_on_change = false

  user_data = templatefile("${path.module}/user_data.sh", {
    gitlab_external_url = var.cloudfront_domain != "" ? "https://${var.cloudfront_domain}/gitlab" : "http://${var.gitlab_domain}"
    backup_bucket_name  = var.backup_enabled ? aws_s3_bucket.backup[0].id : ""
    backup_script = var.backup_enabled ? templatefile("${path.module}/templates/backup_script.sh.tpl", {
      bucket_name = aws_s3_bucket.backup[0].id
    }) : ""
    gitlab_domain     = var.gitlab_domain
    cloudfront_domain = var.cloudfront_domain
    environment       = var.environment
    aws_region        = var.aws_region
  })

  root_block_device {
    volume_size           = var.root_volume_size
    volume_type           = var.root_volume_type
    encrypted             = true
    delete_on_termination = true

    tags = merge(local.common_tags, {
      Name    = "${local.name_prefix}-root"
      Service = "storage"
    })
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 only
    http_put_response_hop_limit = 1
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-instance"
    Service = "compute"
  })

  lifecycle {
    ignore_changes = [ami] # Don't replace instance on AMI update
  }
}
