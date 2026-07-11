# =============================================================================
# GitLab CE Infrastructure — Security Groups
# =============================================================================
# Two security groups:
# 1. ALB SG — allows HTTPS ingress from VPC CIDR only (internal ALB)
# 2. Instance SG — allows HTTP ingress from ALB SG only
# =============================================================================

# -----------------------------------------------------------------------------
# ALB Security Group (internal — VPC CIDR only)
# -----------------------------------------------------------------------------

resource "aws_security_group" "alb" {
  name        = "${local.name_prefix}-alb-sg"
  description = "Security group for GitLab internal ALB - VPC ingress only"
  vpc_id      = local.vpc_id

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-alb-sg"
    Service = "load-balancer"
  })
}

resource "aws_security_group_rule" "alb_ingress_https" {
  type              = "ingress"
  description       = "HTTPS from VPC CIDR"
  from_port         = 443
  to_port           = 443
  protocol          = "tcp"
  cidr_blocks       = [local.vpc_cidr_block]
  security_group_id = aws_security_group.alb.id
}

resource "aws_security_group_rule" "alb_ingress_http" {
  type              = "ingress"
  description       = "HTTP from VPC CIDR (redirect to HTTPS)"
  from_port         = 80
  to_port           = 80
  protocol          = "tcp"
  cidr_blocks       = [local.vpc_cidr_block]
  security_group_id = aws_security_group.alb.id
}

# CloudFront VPC origin traffic is NOT matched by the VPC-CIDR rule above even
# though the CloudFront-managed ENIs live inside the VPC — it is only admitted
# by a rule referencing CloudFront's managed service SG (created automatically
# when the first VPC origin is provisioned in the VPC). Guarded by a variable
# because the service SG doesn't exist until a VPC origin does; enable after
# the gateway CloudFront VPC origin is live. See EPIC #3557 / orch #3622.
data "aws_security_group" "cloudfront_vpc_origins" {
  count  = var.enable_cloudfront_vpc_origin_ingress ? 1 : 0
  name   = "CloudFront-VPCOrigins-Service-SG"
  vpc_id = local.vpc_id
}

resource "aws_security_group_rule" "alb_ingress_cloudfront_vpc_origin" {
  count                    = var.enable_cloudfront_vpc_origin_ingress ? 1 : 0
  type                     = "ingress"
  description              = "HTTP from CloudFront VPC origin service SG (/gitlab/* behavior)"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  source_security_group_id = data.aws_security_group.cloudfront_vpc_origins[0].id
  security_group_id        = aws_security_group.alb.id
}

resource "aws_security_group_rule" "alb_egress_to_instance" {
  type                     = "egress"
  description              = "HTTP to GitLab instance"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.instance.id
  security_group_id        = aws_security_group.alb.id
}

# -----------------------------------------------------------------------------
# Instance Security Group (GitLab EC2)
# -----------------------------------------------------------------------------

resource "aws_security_group" "instance" {
  name        = "${local.name_prefix}-instance-sg"
  description = "Security group for GitLab CE instance - ALB ingress only"
  vpc_id      = local.vpc_id

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-instance-sg"
    Service = "compute"
  })
}

resource "aws_security_group_rule" "instance_ingress_http" {
  type                     = "ingress"
  description              = "HTTP from ALB"
  from_port                = 80
  to_port                  = 80
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.alb.id
  security_group_id        = aws_security_group.instance.id
}

resource "aws_security_group_rule" "instance_egress_all" {
  type              = "egress"
  description       = "All outbound (package installs, updates, S3 backups)"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.instance.id
}
