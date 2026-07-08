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

  user_data = templatefile("${path.module}/user_data.sh", {
    gitlab_external_url = "http://${var.gitlab_domain}"
    backup_bucket_name  = var.backup_enabled ? aws_s3_bucket.backup[0].id : ""
    backup_script = var.backup_enabled ? templatefile("${path.module}/templates/backup_script.sh.tpl", {
      bucket_name = aws_s3_bucket.backup[0].id
    }) : ""
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
