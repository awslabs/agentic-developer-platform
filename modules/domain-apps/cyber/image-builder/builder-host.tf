# =============================================================================
# Phase 2: Ephemeral Build Host
# =============================================================================
# c8i.4xlarge Ubuntu 22.04 with nested virt, 300 GB gp3 root.
# In an ADP dev private subnet with NAT egress.
# Controlled by build_host_enabled — set to false after build completes.
# Auto-teardown: CloudWatch alarm terminates if CPU < 5% for 4 hours.
# =============================================================================

# ---------------------------------------------------------------------------
# AMI lookup — Ubuntu 22.04 LTS
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "ubuntu_ami" {
  count = var.build_host_enabled ? 1 : 0
  name  = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
}

# ---------------------------------------------------------------------------
# Security Group
# ---------------------------------------------------------------------------

resource "aws_security_group" "builder" {
  count = var.build_host_enabled ? 1 : 0

  name        = "${local.name_prefix}-sg-builder"
  description = "Build host SG - no inbound, full outbound for package downloads"
  vpc_id      = local.vpc_id

  egress {
    description = "All outbound (download ISO, packages, upload to S3)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name      = "${local.name_prefix}-sg-builder"
    Ephemeral = "true"
  }
}

# ---------------------------------------------------------------------------
# IAM Role — minimal: SSM + S3 PutObject on assets bucket only
# ---------------------------------------------------------------------------

resource "aws_iam_role" "builder" {
  count = var.build_host_enabled ? 1 : 0

  name = "${local.name_prefix}-builder-role"

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
    Name      = "${local.name_prefix}-builder-role"
    Ephemeral = "true"
  }
}

resource "aws_iam_role_policy_attachment" "builder_ssm" {
  count = var.build_host_enabled ? 1 : 0

  role       = aws_iam_role.builder[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "builder_s3" {
  count = var.build_host_enabled ? 1 : 0

  name = "${local.name_prefix}-builder-s3"
  role = aws_iam_role.builder[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWriteAssets"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.cape_assets.arn,
          "${aws_s3_bucket.cape_assets.arn}/*"
        ]
      },
      {
        Sid    = "SSMWindowsPassword"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:PutParameter",
          "ssm:DeleteParameter"
        ]
        Resource = [
          "arn:aws:ssm:${var.aws_region}:${var.account_id}:parameter/adp/${var.environment}/cape/builder-windows-password"
        ]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "builder" {
  count = var.build_host_enabled ? 1 : 0

  name = "${local.name_prefix}-builder-profile"
  role = aws_iam_role.builder[0].name
}

# ---------------------------------------------------------------------------
# EC2 Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "builder" {
  count = var.build_host_enabled ? 1 : 0

  ami           = data.aws_ssm_parameter.ubuntu_ami[0].value
  instance_type = var.build_instance_type
  subnet_id     = local.subnet_id

  iam_instance_profile   = aws_iam_instance_profile.builder[0].name
  vpc_security_group_ids = [aws_security_group.builder[0].id]

  associate_public_ip_address = false

  root_block_device {
    volume_size           = var.build_root_volume_size
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${local.name_prefix}-builder-root"
    }
  }

  # Nested virtualization must be enabled at launch for c8i/m8i/r8i
  # (disabled by default). Without this, /proc/cpuinfo has no vmx and KVM
  # can't run, forcing a fallback to .metal instances at ~5x the cost.
  cpu_options {
    nested_virtualization = "enabled"
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data_base64 = base64encode(<<-USERDATA
    #!/bin/bash
    # NOTE: Do NOT use "set -e" here. If apt-get fails transiently (mirror
    # issues, NAT saturation), the script must still reach the builder-ready
    # marker so the workflow probe can detect that user-data ran at all.
    # Critical failures are caught by explicit package verification below.
    set -x
    exec > /var/log/builder-userdata.log 2>&1

    echo "=== Image Builder user-data starting at $(date -u) ==="

    # Hold SSM agent snap to prevent auto-refresh during apt-get.
    snap refresh --hold=2h amazon-ssm-agent 2>/dev/null || true

    # Verify SSM is running (it should be from AMI). Do NOT restart it —
    # restarting causes 5-15 min deregistration from the SSM service,
    # making the instance unreachable. Observed in runs 25161042857,
    # 25161866663 where SSM went down at ~45s and never recovered.
    systemctl is-active snap.amazon-ssm-agent.amazon-ssm-agent.service >/dev/null 2>&1 || \
      systemctl is-active amazon-ssm-agent >/dev/null 2>&1 || {
        echo "WARNING: SSM agent not running, attempting start..."
        systemctl start snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || \
          systemctl start amazon-ssm-agent 2>/dev/null || true
      }

    # Rate-limit apt downloads to preserve NAT bandwidth for SSM heartbeats.
    # Without this, 500MB+ of package downloads saturates the NAT gateway,
    # causing SSM heartbeat timeouts and 13+ min outages.
    cat > /etc/apt/apt.conf.d/99-rate-limit << 'APT_CONF'
    Acquire::http::Dl-Limit "50000";
    Acquire::https::Dl-Limit "50000";
    APT_CONF

    # Install build packages with retry
    for attempt in 1 2 3; do
      apt-get update -y && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y \
        qemu-utils \
        qemu-kvm \
        libvirt-daemon-system \
        virtinst \
        cloud-image-utils \
        genisoimage \
        awscli \
        jq \
        cpu-checker && break
      echo "apt-get failed (attempt $attempt/3), retrying in 10s..."
      sleep 10
    done

    # Remove rate limit after install (Packer needs full speed for ISO download)
    rm -f /etc/apt/apt.conf.d/99-rate-limit

    # Verify critical packages — if these are missing, the build WILL fail
    MISSING=""
    for pkg in qemu-kvm qemu-utils libvirt-daemon-system genisoimage jq; do
      dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
    done
    if [ -n "$MISSING" ]; then
      echo "ERROR: Missing critical packages:$MISSING"
      echo "BUILDER_FAILED=missing_packages:$MISSING" > /var/run/builder-ready
      exit 1
    fi

    # Verify nested virtualization
    kvm-ok || echo "WARNING: KVM not available — nested virt may not work"

    # Enable and start libvirtd
    systemctl enable libvirtd
    systemctl start libvirtd

    # Ensure default network is active
    virsh net-start default 2>/dev/null || true
    virsh net-autostart default 2>/dev/null || true

    # Write bootstrap marker — signals user-data is complete and build host ready
    echo "BUILDER_READY=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /var/run/builder-ready

    echo "=== Image Builder user-data complete at $(date -u) ==="
  USERDATA
  )

  tags = {
    Name      = "${local.name_prefix}-builder"
    Ephemeral = "true"
    Purpose   = "windows-qcow2-build"
  }

  lifecycle {
    ignore_changes = [ami]
  }
}

# ---------------------------------------------------------------------------
# Auto-teardown: CloudWatch alarm → SNS → Lambda → terminate
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "builder_idle" {
  count = var.build_host_enabled ? 1 : 0

  alarm_name        = "${local.name_prefix}-builder-idle"
  alarm_description = "Terminate build host if CPU < ${var.idle_cpu_threshold}% for ${var.idle_period_seconds / 3600}h"

  namespace           = "AWS/EC2"
  metric_name         = "CPUUtilization"
  statistic           = "Average"
  period              = 300
  evaluation_periods  = var.idle_period_seconds / 300
  threshold           = var.idle_cpu_threshold
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    InstanceId = aws_instance.builder[0].id
  }

  alarm_actions = [
    "arn:aws:automate:${var.aws_region}:ec2:terminate"
  ]

  tags = {
    Name      = "${local.name_prefix}-builder-idle-alarm"
    Ephemeral = "true"
  }
}
