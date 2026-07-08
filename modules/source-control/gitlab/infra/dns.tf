# =============================================================================
# GitLab CE Infrastructure — DNS (Route53 Private Hosted Zone Record)
# =============================================================================

data "aws_route53_zone" "private" {
  name         = var.route53_zone_name
  private_zone = true
}

resource "aws_route53_record" "gitlab" {
  zone_id = data.aws_route53_zone.private.zone_id
  name    = var.gitlab_domain
  type    = "A"

  alias {
    name                   = aws_lb.gitlab.dns_name
    zone_id                = aws_lb.gitlab.zone_id
    evaluate_target_health = true
  }
}
