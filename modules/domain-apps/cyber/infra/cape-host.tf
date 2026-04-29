# =============================================================================
# Phase 2: CAPE EC2 Host
# =============================================================================
# c8i.4xlarge with nested virtualization, no public IP, SSM-only access.
# IAM role has minimal permissions per issue #225 hard invariants.
# =============================================================================

# ---------------------------------------------------------------------------
# AMI lookup — Ubuntu 22.04 LTS (Canonical)
# ---------------------------------------------------------------------------

data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id"
}

# ---------------------------------------------------------------------------
# Security Group
# ---------------------------------------------------------------------------

resource "aws_security_group" "cape_host" {
  name        = "${local.name_prefix}-sg-cape-host"
  description = "CAPE host SG - no direct inbound; ALB reaches via target group"
  vpc_id      = aws_vpc.threat_research.id

  # No inbound rules — traffic arrives only via the internal ALB (Phase 6).
  # ALB health checks use a separate SG rule added in alb.tf.

  # Outbound: broad during bootstrap (download CAPE, packages, Windows ISO).
  # Phase 7 hardening script tightens this to VPC endpoints + NTP only.
  egress {
    description = "All outbound (tightened post-bootstrap in Phase 7)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-sg-cape-host"
  }
}

# ---------------------------------------------------------------------------
# IAM Role — minimal permissions per hard invariant #3
# ---------------------------------------------------------------------------

resource "aws_iam_role" "cape_host" {
  name = "${local.name_prefix}-cape-host-role"

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
    Name = "${local.name_prefix}-cape-host-role"
  }
}

# SSM Session Manager (managed policy)
resource "aws_iam_role_policy_attachment" "cape_ssm" {
  role       = aws_iam_role.cape_host.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Inline policy — scoped to specific resources only
resource "aws_iam_role_policy" "cape_minimal" {
  name = "${local.name_prefix}-cape-minimal"
  role = aws_iam_role.cape_host.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadSamples"
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = [
          "arn:aws:s3:::${var.sample_bucket_name}-*/o/*/in/*"
        ]
      },
      {
        Sid    = "DynamoDBWriteResults"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem"
        ]
        Resource = [
          "arn:aws:dynamodb:${var.aws_region}:${var.account_id}:table/${var.analysis_results_table}"
        ]
      },
      {
        Sid    = "SecretsManagerReadToken"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = [
          "arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:adp/cape/api-token-*"
        ]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "cape_host" {
  name = "${local.name_prefix}-cape-host-profile"
  role = aws_iam_role.cape_host.name
}

# ---------------------------------------------------------------------------
# EC2 Instance
# ---------------------------------------------------------------------------

resource "aws_instance" "cape_host" {
  ami           = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type = var.cape_instance_type
  subnet_id     = aws_subnet.private[0].id

  iam_instance_profile   = aws_iam_instance_profile.cape_host.name
  vpc_security_group_ids = [aws_security_group.cape_host.id]

  # Hard invariant #4: No public IP
  associate_public_ip_address = false

  # Root volume — 200 GB gp3, encrypted
  root_block_device {
    volume_size           = var.cape_root_volume_size
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true

    tags = {
      Name = "${local.name_prefix}-cape-root"
    }
  }

  # Data volume — 100 GB gp3 for samples + reports (independently resizable)
  ebs_block_device {
    device_name           = "/dev/sdf"
    volume_size           = var.cape_data_volume_size
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = false

    tags = {
      Name = "${local.name_prefix}-cape-data"
    }
  }

  # Nested virtualization: c8i/m8i/r8i support nested virt, but it is DISABLED
  # by default. Must be opted in at launch via cpu_options, otherwise
  # /proc/cpuinfo on the instance shows no vmx flag and KVM cannot run.
  # Per AWS docs (amazon-ec2-nested-virtualization.html): post-launch toggling
  # requires a stopped instance, so we bake it into launch config.
  cpu_options {
    nested_virtualization = "enabled"
  }

  # Metadata options — IMDSv2 required
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  user_data = base64encode(<<-USERDATA
    #!/bin/bash
    set -euo pipefail

    # Minimal user-data: update packages, ensure SSM agent, tag ready.
    # CAPE installation happens via SSM send-command (Phase 3 bootstrap script)
    # to keep Terraform idempotent.

    apt-get update -y
    apt-get upgrade -y

    # SSM agent is pre-installed on Ubuntu AMIs from Canonical;
    # ensure it's running.
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent

    # Mount the data volume
    DATA_DEV="/dev/nvme1n1"
    if [ ! -e "$DATA_DEV" ]; then
      DATA_DEV="/dev/xvdf"
    fi
    if ! blkid "$DATA_DEV" | grep -q ext4; then
      mkfs.ext4 "$DATA_DEV"
    fi
    mkdir -p /opt/cape-data
    mount "$DATA_DEV" /opt/cape-data
    echo "$DATA_DEV /opt/cape-data ext4 defaults,nofail 0 2" >> /etc/fstab

    # Tag instance as ready for Phase 3 (IMDSv2 token required)
    IMDS_TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")
    INSTANCE_ID=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
    REGION=$(curl -s -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
    aws ec2 create-tags --resources "$INSTANCE_ID" --tags "Key=cape-setup,Value=ready-for-phase3" --region "$REGION" || true
  USERDATA
  )

  tags = {
    Name       = "${local.name_prefix}-cape-host"
    cape-setup = "provisioned"
  }

  lifecycle {
    ignore_changes = [ami]
  }
}
