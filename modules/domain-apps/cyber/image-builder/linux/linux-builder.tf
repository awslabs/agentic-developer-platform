# =============================================================================
# Linux Image Builder — Dedicated build host for Ubuntu CAPE qcow2
# =============================================================================
# Issue #250: Separate from the Windows builder host. Fresh c8i.4xlarge with
# nested virtualization, installs Packer + QEMU, builds Ubuntu cloud image.
#
# Usage:
#   terraform init -backend-config=<backend.tfvars>
#   terraform apply -var linux_build_host_enabled=true
#   # Wait for build (~20 min), then:
#   terraform apply -var linux_build_host_enabled=false
# =============================================================================

variable "linux_build_host_enabled" {
  type        = bool
  description = "Whether to create the Linux image build host. Set to false after build completes."
  default     = false
}

# ---------------------------------------------------------------------------
# AMI lookup — Ubuntu 22.04 LTS (same as Windows builder host OS)
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "ubuntu_ami_linux_builder" {
  count = var.linux_build_host_enabled ? 1 : 0
  name  = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
}

# ---------------------------------------------------------------------------
# Security Group — no inbound, full outbound
# ---------------------------------------------------------------------------

resource "aws_security_group" "linux_builder" {
  count = var.linux_build_host_enabled ? 1 : 0

  name        = "${local.name_prefix}-sg-linux-builder"
  description = "Linux build host SG - no inbound, full outbound for downloads"
  vpc_id      = var.vpc_id

  egress {
    description = "All outbound (download cloud image, packages, upload to S3)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name      = "${local.name_prefix}-sg-linux-builder"
    Ephemeral = "true"
  }
}

# ---------------------------------------------------------------------------
# IAM Role — SSM + S3 Put/Get on assets bucket
# ---------------------------------------------------------------------------

resource "aws_iam_role" "linux_builder" {
  count = var.linux_build_host_enabled ? 1 : 0

  name = "${local.name_prefix}-linux-builder-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    Name      = "${local.name_prefix}-linux-builder-role"
    Ephemeral = "true"
  }
}

resource "aws_iam_role_policy_attachment" "linux_builder_ssm" {
  count = var.linux_build_host_enabled ? 1 : 0

  role       = aws_iam_role.linux_builder[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "linux_builder_s3" {
  count = var.linux_build_host_enabled ? 1 : 0

  name = "${local.name_prefix}-linux-builder-s3"
  role = aws_iam_role.linux_builder[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWriteAssets"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.cape_assets.arn,
          "${aws_s3_bucket.cape_assets.arn}/*"
        ]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "linux_builder" {
  count = var.linux_build_host_enabled ? 1 : 0

  name = "${local.name_prefix}-linux-builder-profile"
  role = aws_iam_role.linux_builder[0].name
}

# ---------------------------------------------------------------------------
# EC2 Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "linux_builder" {
  count = var.linux_build_host_enabled ? 1 : 0

  ami           = data.aws_ssm_parameter.ubuntu_ami_linux_builder[0].value
  instance_type = var.build_instance_type
  subnet_id     = var.subnet_id

  iam_instance_profile   = aws_iam_instance_profile.linux_builder[0].name
  vpc_security_group_ids = [aws_security_group.linux_builder[0].id]

  associate_public_ip_address = false

  root_block_device {
    volume_size           = 100
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${local.name_prefix}-linux-builder-root"
    }
  }

  # Nested virtualization for KVM
  cpu_options {
    nested_virtualization = "enabled"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-USERDATA
    #!/bin/bash
    set -euo pipefail
    exec > /var/log/builder-userdata.log 2>&1

    echo "=== Linux Image Builder user-data starting ==="

    apt-get update -y
    apt-get install -y \
      qemu-utils \
      qemu-kvm \
      libvirt-daemon-system \
      virtinst \
      cloud-image-utils \
      genisoimage \
      awscli \
      jq \
      cpu-checker \
      python3 \
      unzip \
      curl

    # Install Packer
    PACKER_VERSION="1.10.3"
    curl -fsSL "https://releases.hashicorp.com/packer/$${PACKER_VERSION}/packer_$${PACKER_VERSION}_linux_amd64.zip" \
      -o /tmp/packer.zip
    unzip -o /tmp/packer.zip -d /usr/local/bin/
    chmod +x /usr/local/bin/packer
    rm -f /tmp/packer.zip

    # Ensure SSM agent is running (snap-based on Ubuntu 22.04)
    systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || true
    systemctl start snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || true

    # Verify nested virtualization
    kvm-ok || echo "WARNING: KVM not available"

    # Enable and start libvirtd
    systemctl enable libvirtd
    systemctl start libvirtd

    # Ensure default network is active
    virsh net-start default 2>/dev/null || true
    virsh net-autostart default 2>/dev/null || true

    # Write bootstrap marker
    echo "BUILDER_READY=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /var/run/builder-ready

    echo "=== Linux Image Builder user-data complete ==="
  USERDATA
  )

  tags = {
    Name      = "${local.name_prefix}-linux-builder"
    Ephemeral = "true"
    Purpose   = "ubuntu-qcow2-build"
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

# ---------------------------------------------------------------------------
# Auto-teardown: CloudWatch alarm terminates if idle 4h
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "linux_builder_idle" {
  count = var.linux_build_host_enabled ? 1 : 0

  alarm_name        = "${local.name_prefix}-linux-builder-idle"
  alarm_description = "Terminate Linux build host if CPU < ${var.idle_cpu_threshold}% for ${var.idle_period_seconds / 3600}h"

  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = var.idle_period_seconds / 300
  threshold           = var.idle_cpu_threshold
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.linux_builder[0].id
  }

  alarm_actions = [
    "arn:aws:automate:${var.aws_region}:ec2:terminate"
  ]

  tags = {
    Name      = "${local.name_prefix}-linux-builder-idle-alarm"
    Ephemeral = "true"
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "linux_builder_instance_id" {
  description = "Linux build host instance ID (empty if disabled)"
  value       = var.linux_build_host_enabled ? aws_instance.linux_builder[0].id : ""
}

output "linux_builder_private_ip" {
  description = "Linux build host private IP (empty if disabled)"
  value       = var.linux_build_host_enabled ? aws_instance.linux_builder[0].private_ip : ""
}
