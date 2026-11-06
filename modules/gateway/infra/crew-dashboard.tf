# Crew dashboard edge, ALB mode (#7205): a dedicated public ALB with an ACM
# certificate and Route 53 alias on crew_dashboard_hostname. The existing gateway
# ALB is internal; this ALB's 300-second idle timeout keeps long Crew streams open
# without relying on keepalives. Every block here is gated on
# local.crew_dashboard_alb, so crew_dashboard_edge = "cloudfront" (#7902,
# crew-dashboard-cloudfront.tf) creates none of it. Keep Crew disabled until the
# Crew proxy authenticates every request on this hostname.

locals {
  crew_dashboard_alb        = var.crew_dashboard_enabled && var.crew_dashboard_edge == "alb"
  crew_dashboard_cloudfront = var.crew_dashboard_enabled && var.crew_dashboard_edge == "cloudfront"

  # Origin the gateway's Crew proxy serves (CREW_DASHBOARD_ORIGIN): the operator's
  # hostname in ALB mode, the distribution's own name in CloudFront mode. The
  # conditional is lazy, so the count-0 branch is never evaluated.
  crew_dashboard_origin = local.crew_dashboard_cloudfront ? "https://${one(aws_cloudfront_distribution.crew_dashboard[*].domain_name)}" : "https://${var.crew_dashboard_hostname}"
}
data "aws_route53_zone" "crew_dashboard" {
  count   = local.crew_dashboard_alb ? 1 : 0
  zone_id = var.crew_dashboard_zone_id
}

resource "aws_acm_certificate" "crew_dashboard" {
  count             = local.crew_dashboard_alb ? 1 : 0
  domain_name       = var.crew_dashboard_hostname
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = var.crew_dashboard_hostname != "" && var.crew_dashboard_zone_id != "" && var.aws_region == "us-east-1" && !data.aws_route53_zone.crew_dashboard[0].private_zone
      error_message = "Crew hosting requires a hostname, a public Route 53 zone, and an us-east-1 gateway."
    }
    precondition {
      condition     = endswith(lower(var.crew_dashboard_hostname), ".${trimsuffix(lower(data.aws_route53_zone.crew_dashboard[0].name), ".")}")
      error_message = "The Crew hostname must be a subdomain of the selected Route 53 zone."
    }
    precondition {
      condition     = var.frontend_domain_name != "" && (lower(var.frontend_domain_name) == trimsuffix(lower(data.aws_route53_zone.crew_dashboard[0].name), ".") || endswith(lower(var.frontend_domain_name), ".${trimsuffix(lower(data.aws_route53_zone.crew_dashboard[0].name), ".")}"))
      error_message = "Crew hosting requires a frontend custom domain in the same zone so SameSite=Lax dashboard cookies work."
    }
  }
}

resource "aws_route53_record" "crew_dashboard_validation" {
  count           = local.crew_dashboard_alb ? 1 : 0
  zone_id         = var.crew_dashboard_zone_id
  name            = tolist(aws_acm_certificate.crew_dashboard[0].domain_validation_options)[0].resource_record_name
  type            = tolist(aws_acm_certificate.crew_dashboard[0].domain_validation_options)[0].resource_record_type
  ttl             = 60
  records         = [tolist(aws_acm_certificate.crew_dashboard[0].domain_validation_options)[0].resource_record_value]
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "crew_dashboard" {
  count                   = local.crew_dashboard_alb ? 1 : 0
  certificate_arn         = aws_acm_certificate.crew_dashboard[0].arn
  validation_record_fqdns = [aws_route53_record.crew_dashboard_validation[0].fqdn]
}

resource "kubernetes_manifest" "crew_dashboard_params" {
  count = local.crew_dashboard_alb ? 1 : 0

  manifest = {
    apiVersion = "eks.amazonaws.com/v1"
    kind       = "IngressClassParams"
    metadata = {
      name = "crew-dashboard"
    }
    spec = {
      scheme = "internet-facing"
      loadBalancerAttributes = {
        "idle_timeout.timeout_seconds"              = "300"
        "routing.http.preserve_host_header.enabled" = "true"
      }
    }
  }
}

resource "kubernetes_ingress_class_v1" "crew_dashboard" {
  count = local.crew_dashboard_alb ? 1 : 0

  metadata {
    name = "crew-dashboard"
  }

  spec {
    controller = "eks.amazonaws.com/alb"
    parameters {
      api_group = "eks.amazonaws.com"
      kind      = "IngressClassParams"
      name      = "crew-dashboard"
    }
  }

  depends_on = [kubernetes_manifest.crew_dashboard_params]
}

resource "kubernetes_ingress_v1" "crew_dashboard" {
  count                  = local.crew_dashboard_alb ? 1 : 0
  wait_for_load_balancer = true

  metadata {
    name      = "crew-dashboard"
    namespace = "adp-gateway"
    annotations = {
      "alb.ingress.kubernetes.io/target-type"      = "ip"
      "alb.ingress.kubernetes.io/listen-ports"     = jsonencode([{ HTTP = 80 }, { HTTPS = 443 }])
      "alb.ingress.kubernetes.io/certificate-arn"  = aws_acm_certificate_validation.crew_dashboard[0].certificate_arn
      "alb.ingress.kubernetes.io/ssl-redirect"     = "443"
      "alb.ingress.kubernetes.io/healthcheck-path" = "/health"
    }
  }

  spec {
    ingress_class_name = kubernetes_ingress_class_v1.crew_dashboard[0].metadata[0].name
    rule {
      host = var.crew_dashboard_hostname
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = "bedrockgateway"
              port {
                number = 80
              }
            }
          }
        }
      }
    }
  }
}

data "aws_lb" "crew_dashboard" {
  count      = local.crew_dashboard_alb ? 1 : 0
  name       = regex("^([^.]+)-[0-9]+\\.", kubernetes_ingress_v1.crew_dashboard[0].status[0].load_balancer[0].ingress[0].hostname)[0]
  depends_on = [kubernetes_ingress_v1.crew_dashboard]
}

resource "aws_route53_record" "crew_dashboard" {
  count   = local.crew_dashboard_alb ? 1 : 0
  zone_id = var.crew_dashboard_zone_id
  name    = var.crew_dashboard_hostname
  type    = "A"

  alias {
    name                   = data.aws_lb.crew_dashboard[0].dns_name
    zone_id                = data.aws_lb.crew_dashboard[0].zone_id
    evaluate_target_health = false
  }
}

resource "aws_ssm_parameter" "crew_dashboard_origin" {
  count = var.crew_dashboard_enabled ? 1 : 0

  name  = "/adp/${var.environment}/gateway/crew-dashboard-origin"
  type  = "String"
  value = local.crew_dashboard_origin

  depends_on = [aws_route53_record.crew_dashboard, aws_cloudfront_distribution.crew_dashboard]
}
