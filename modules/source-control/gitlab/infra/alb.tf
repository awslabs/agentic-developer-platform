# =============================================================================
# GitLab CE Infrastructure — Internal Application Load Balancer
# =============================================================================
# Internal ALB in private subnets. Terminates TLS and forwards HTTP to GitLab.
# =============================================================================

# -----------------------------------------------------------------------------
# Internal ALB
# -----------------------------------------------------------------------------

resource "aws_lb" "gitlab" {
  name               = "${local.name_prefix}-alb"
  internal           = true
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = local.private_subnets

  enable_deletion_protection = var.environment == "prod"

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-alb"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# Target Group (HTTP:80 → GitLab instance)
# -----------------------------------------------------------------------------

resource "aws_lb_target_group" "gitlab" {
  name     = "${local.name_prefix}-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = local.vpc_id

  target_type = "instance"

  health_check {
    enabled             = true
    healthy_threshold   = 3
    interval            = 30
    matcher             = "200"
    path                = "/-/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 10
    unhealthy_threshold = 3
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-tg"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# Target Group Attachment
# -----------------------------------------------------------------------------

resource "aws_lb_target_group_attachment" "gitlab" {
  target_group_arn = aws_lb_target_group.gitlab.arn
  target_id        = aws_instance.gitlab.id
  port             = 80
}

# -----------------------------------------------------------------------------
# HTTPS Listener (443 → target group)
# -----------------------------------------------------------------------------

resource "aws_lb_listener" "https" {
  count = var.certificate_arn != "" ? 1 : 0

  load_balancer_arn = aws_lb.gitlab.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gitlab.arn
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-listener-https"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# HTTP Listener (80 → redirect to HTTPS, or forward if no cert)
# -----------------------------------------------------------------------------

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.gitlab.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = var.certificate_arn != "" ? "redirect" : "forward"
    target_group_arn = var.certificate_arn != "" ? null : aws_lb_target_group.gitlab.arn

    dynamic "redirect" {
      for_each = var.certificate_arn != "" ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-listener-http"
    Service = "load-balancer"
  })
}

# -----------------------------------------------------------------------------
# HTTP Listener Rule: forward /api/* to GitLab (bypass 301 redirect)
# -----------------------------------------------------------------------------
# Spike artifact: internal ALB, API traffic intentionally plain-HTTP inside the
# VPC. The 301 default action converts POST→GET (per RFC 7231 §6.4.2), breaking
# GitLab API clients (worker ack, E2E). This rule forwards /api/* directly so
# POST semantics are preserved. Remove when a private-CA cert replaces the
# self-signed one and all clients switch to https:// URLs (see gitlab.tfvars).
# -----------------------------------------------------------------------------

resource "aws_lb_listener_rule" "http_api_forward" {
  count = var.certificate_arn != "" ? 1 : 0

  listener_arn = aws_lb_listener.http.arn
  priority     = 10

  condition {
    path_pattern {
      values = ["/api/*"]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.gitlab.arn
  }

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-http-api-forward"
    Service = "load-balancer"
  })
}
