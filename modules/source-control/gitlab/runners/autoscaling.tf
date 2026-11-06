data "aws_ami" "job" {
  filter {
    name   = "image-id"
    values = [var.host_ami_id]
  }
}

locals {
  job_root_device = one([for mapping in data.aws_ami.job.block_device_mappings : mapping
    if mapping.device_name == data.aws_ami.job.root_device_name
  ])
}

resource "aws_launch_template" "job" {
  for_each      = local.group_bindings
  name          = "adp-${var.environment}-gitlab-job-${each.key}"
  image_id      = var.host_ami_id
  instance_type = var.host_instance_type
  user_data = base64encode(templatefile("${path.module}/templates/job-network-activate.sh.tftpl", {
    host_sha256           = filesha256("${path.module}/templates/job-user-data.sh.tftpl")
    image_verifier_sha256 = filesha256("${path.module}/bootstrap/verify_images.py")
    job_image             = var.job_image_digest
    helper_image          = try(local.release_lock.helper_image, "")
    gitlab_ip             = try(data.aws_network_interface.endpoint["${each.key}/gitlab"].private_ip, "0.0.0.0")
    cache_ip              = try(data.aws_network_interface.endpoint["${each.key}/cache"].private_ip, "0.0.0.0")
    cache_host            = "s3.${var.aws_region}.amazonaws.com"
    gitlab_host           = local.private_gitlab_hostname
  }))
  depends_on = [terraform_data.release_ready]

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [aws_security_group.job[each.key].id]
    delete_on_termination       = true
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_protocol_ipv6          = "disabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }
  block_device_mappings {
    device_name = data.aws_ami.job.root_device_name
    ebs {
      # Set the exact approved AMI size explicitly: omitting this computed
      # field can retain a smaller size from an existing launch template.
      volume_size           = tonumber(local.job_root_device.ebs.volume_size)
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }
  tag_specifications {
    resource_type = "instance"
    tags = {
      Component   = "gitlab-job"
      Environment = var.environment
      GroupId     = each.key
    }
  }
  lifecycle {
    precondition {
      condition     = can(regex("^ami-[0-9a-f]{8,17}$", var.host_ami_id))
      error_message = "A reviewed immutable host AMI ID is required; metadata reads must use that exact ID."
    }
  }
}

resource "aws_autoscaling_group" "job" {
  for_each                  = local.group_bindings
  name                      = "adp-${var.environment}-gitlab-job-${each.key}"
  min_size                  = 0
  max_size                  = 2
  desired_capacity          = 0
  protect_from_scale_in     = true
  suspended_processes       = ["AZRebalance"]
  vpc_zone_identifier       = sort(tolist(local.job_subnet_ids))
  health_check_type         = "EC2"
  health_check_grace_period = 60

  launch_template {
    id      = aws_launch_template.job[each.key].id
    version = aws_launch_template.job[each.key].latest_version
  }
  dynamic "tag" {
    for_each = { Component = "gitlab-job", Environment = var.environment, GroupId = each.key, LaunchTemplateId = aws_launch_template.job[each.key].id }
    content {
      key                 = tag.key
      value               = tag.value
      propagate_at_launch = true
    }
  }
  lifecycle {
    ignore_changes = [desired_capacity]
  }
}
