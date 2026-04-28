# =============================================================================
# Phase 6: VPC Peering + Internal ALB + Secrets Manager API Token
# =============================================================================
# One-directional peering: ADP VPC -> Threat Research VPC on port 443 only.
# Hard invariant #1: No route from Threat Research -> ADP VPC.
# =============================================================================

# ---------------------------------------------------------------------------
# Secrets Manager — CAPE API token
# ---------------------------------------------------------------------------

resource "random_password" "cape_api_token" {
  length  = 44
  special = false
}

resource "aws_secretsmanager_secret" "cape_api_token" {
  name        = "adp/cape/api-token"
  description = "API token for CAPE sandbox REST API (issue #225)"

  tags = {
    Name = "adp-cape-api-token"
  }
}

resource "aws_secretsmanager_secret_version" "cape_api_token" {
  secret_id     = aws_secretsmanager_secret.cape_api_token.id
  secret_string = random_password.cape_api_token.result
}

# ---------------------------------------------------------------------------
# Self-signed TLS certificate for the internal ALB (dev only)
# ---------------------------------------------------------------------------

resource "tls_private_key" "cape_alb" {
  algorithm = "RSA"
  rsa_bits  = 2048
}

resource "tls_self_signed_cert" "cape_alb" {
  private_key_pem = tls_private_key.cape_alb.private_key_pem

  subject {
    common_name  = "cape-internal.sandbox.local"
    organization = "ADP Cyber Sandbox"
  }

  validity_period_hours = 8760 # 1 year

  allowed_uses = [
    "key_encipherment",
    "digital_signature",
    "server_auth",
  ]
}

resource "aws_acm_certificate" "cape_alb" {
  private_key      = tls_private_key.cape_alb.private_key_pem
  certificate_body = tls_self_signed_cert.cape_alb.cert_pem

  tags = {
    Name = "${local.name_prefix}-alb-cert"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Internal ALB — fronts CAPE API over HTTPS
# ---------------------------------------------------------------------------

resource "aws_security_group" "cape_alb" {
  name        = "${local.name_prefix}-sg-cape-alb"
  description = "Internal ALB for CAPE API"
  vpc_id      = aws_vpc.threat_research.id

  # Accept HTTPS from the ADP VPC pod CIDR (via peering)
  ingress {
    description = "HTTPS from ADP VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.adp_vpc_id != "" ? [var.adp_vpc_cidr] : []
  }

  egress {
    description = "To CAPE host on port 8000"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = var.private_subnet_cidrs
  }

  tags = {
    Name = "${local.name_prefix}-sg-cape-alb"
  }
}

# Allow ALB health checks to reach the CAPE host
resource "aws_security_group_rule" "cape_host_from_alb" {
  type                     = "ingress"
  from_port                = 8000
  to_port                  = 8000
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.cape_alb.id
  security_group_id        = aws_security_group.cape_host.id
  description              = "CAPE API from internal ALB"
}

resource "aws_lb" "cape_internal" {
  name               = "${local.name_prefix}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.cape_alb.id]
  subnets            = aws_subnet.private[*].id

  tags = {
    Name = "${local.name_prefix}-alb"
  }
}

resource "aws_lb_target_group" "cape_api" {
  name     = "${local.name_prefix}-tg-cape"
  port     = 8000
  protocol = "HTTP"
  vpc_id   = aws_vpc.threat_research.id

  health_check {
    path                = "/apiv2/cuckoo/status/"
    port                = "traffic-port"
    protocol            = "HTTP"
    healthy_threshold   = 2
    unhealthy_threshold = 5
    timeout             = 10
    interval            = 30
    matcher             = "200-299"
  }

  tags = {
    Name = "${local.name_prefix}-tg-cape"
  }
}

resource "aws_lb_target_group_attachment" "cape_host" {
  target_group_arn = aws_lb_target_group.cape_api.arn
  target_id        = aws_instance.cape_host.id
  port             = 8000
}

resource "aws_lb_listener" "cape_https" {
  load_balancer_arn = aws_lb.cape_internal.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate.cape_alb.arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.cape_api.arn
  }
}

# ---------------------------------------------------------------------------
# VPC Peering — ADP VPC -> Threat Research VPC (conditional)
# ---------------------------------------------------------------------------

resource "aws_vpc_peering_connection" "adp_to_threat_research" {
  count = var.adp_vpc_id != "" ? 1 : 0

  vpc_id      = var.adp_vpc_id
  peer_vpc_id = aws_vpc.threat_research.id
  auto_accept = true # Same account

  tags = {
    Name = "${local.name_prefix}-peer-adp-to-threat"
    Side = "requester"
  }
}

# Route from ADP VPC private subnets -> Threat Research VPC (CAPE ALB)
# Only the private subnet CIDR where the ALB lives, not the full VPC.
resource "aws_route" "adp_to_threat_research" {
  count = var.adp_vpc_id != "" ? length(var.adp_private_route_table_ids) : 0

  route_table_id            = var.adp_private_route_table_ids[count.index]
  destination_cidr_block    = var.vpc_cidr
  vpc_peering_connection_id = aws_vpc_peering_connection.adp_to_threat_research[0].id
}

# IMPORTANT: No reverse route. Hard invariant #1.
# The Threat Research VPC has NO route to the ADP VPC.
# Analysis VMs and the CAPE host cannot reach ADP resources.
