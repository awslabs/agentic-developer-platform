# =============================================================================
# Phase 2: Ephemeral Build Host
# =============================================================================
# c8i.4xlarge Ubuntu 22.04 with nested virt, 300 GB gp3 root.
# In an ADP dev private subnet with NAT egress + SSM VPC endpoints.
# Controlled by build_host_enabled — set to false after build completes.
# Auto-teardown: CloudWatch alarm terminates if CPU < 5% for 4 hours.
#
# ARCHITECTURE: User-data is fully self-contained. After installing packages,
# it downloads build-pipeline.sh from S3 and executes the build. The workflow
# only needs to poll S3 for the output artifact — no SSM send-command required.
# This eliminates the chronic SSM agent instability on fresh Ubuntu instances.
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
    # Self-contained build: install packages, download inputs from S3, run build.
    # The workflow polls S3 for the output — no SSM send-command needed.
    set -x
    exec > /var/log/builder-userdata.log 2>&1

    echo "=== Image Builder user-data starting at $(date -u) ==="

    export HOME=/root
    export AWS_REGION=${var.aws_region}
    export ENVIRONMENT=${var.environment}
    export ASSETS_BUCKET=${var.assets_bucket_name}
    BUILD_DATE=$(date -u +%Y-%m-%d)

    # ── Phase 1: Install packages ──────────────────────────────────────
    # On Ubuntu 22.04, qemu-kvm is a meta-package that may be unavailable
    # on fresh AMIs due to apt cache staleness. Install qemu-system-x86
    # directly (provides /usr/bin/qemu-system-x86_64 and KVM support).
    for attempt in 1 2 3; do
      apt-get update -y && \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --fix-broken \
        qemu-utils \
        qemu-system-x86 \
        libvirt-daemon-system \
        libvirt-clients \
        virtinst \
        cloud-image-utils \
        genisoimage \
        awscli \
        jq \
        cpu-checker && break
      echo "apt-get failed (attempt $attempt/3), retrying in 15s..."
      sleep 15
    done

    # Fallback: try qemu-kvm meta-package if qemu-system-x86_64 not found
    if ! command -v qemu-system-x86_64 >/dev/null 2>&1; then
      DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-kvm || true
    fi

    # Verify critical binaries and packages
    MISSING=""
    command -v qemu-system-x86_64 >/dev/null 2>&1 || MISSING="$MISSING qemu-kvm"
    for pkg in qemu-utils libvirt-daemon-system genisoimage jq awscli; do
      dpkg -s "$pkg" >/dev/null 2>&1 || MISSING="$MISSING $pkg"
    done
    if [ -n "$MISSING" ]; then
      echo "ERROR: Missing critical packages:$MISSING"
      echo "FAILED: missing packages:$MISSING" | \
        aws s3 cp - "s3://$ASSETS_BUCKET/windows-build-inputs/BUILD_FAILED_$BUILD_DATE" --region "$AWS_REGION" || true
      exit 1
    fi

    # Verify nested virtualization
    kvm-ok || echo "WARNING: KVM not available — nested virt may not work"

    # Enable and start libvirtd
    systemctl enable libvirtd
    systemctl start libvirtd
    virsh net-start default 2>/dev/null || true
    virsh net-autostart default 2>/dev/null || true

    echo "BUILDER_READY=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /var/run/builder-ready
    echo "=== Packages installed, starting build ==="

    # ── Phase 2: Download build inputs and run ─────────────────────────
    WORKDIR=/opt/windows-build
    mkdir -p "$WORKDIR"

    # Download build-pipeline.sh (must have been staged to S3 by workflow)
    for dl_attempt in 1 2 3 4 5; do
      if aws s3 cp "s3://$ASSETS_BUCKET/windows-build-inputs/build-pipeline.sh" \
        "$WORKDIR/build-pipeline.sh" --region "$AWS_REGION"; then
        break
      fi
      echo "S3 download failed (attempt $dl_attempt/5), waiting 30s..."
      sleep 30
    done

    if [ ! -f "$WORKDIR/build-pipeline.sh" ]; then
      echo "ERROR: Could not download build-pipeline.sh from S3"
      echo "FAILED: s3_download" | \
        aws s3 cp - "s3://$ASSETS_BUCKET/windows-build-inputs/BUILD_FAILED_$BUILD_DATE" --region "$AWS_REGION" || true
      exit 1
    fi

    chmod +x "$WORKDIR/build-pipeline.sh"
    echo "=== Running build-pipeline.sh ==="
    bash "$WORKDIR/build-pipeline.sh" 2>&1 | tee /var/log/build-pipeline.log
    BUILD_EXIT=$?

    if [ $BUILD_EXIT -ne 0 ]; then
      echo "ERROR: build-pipeline.sh exited with code $BUILD_EXIT"
      tail -50 /var/log/build-pipeline.log | \
        aws s3 cp - "s3://$ASSETS_BUCKET/windows-build-inputs/BUILD_FAILED_$BUILD_DATE" --region "$AWS_REGION" || true
    fi

    echo "=== Image Builder user-data complete at $(date -u), exit=$BUILD_EXIT ==="
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
