import json
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest


INFRA = pathlib.Path(__file__).resolve().parents[1]
REPO = INFRA.parents[2]

ALB_GUARD = r"(?m)^\s+count\s+= local\.crew_dashboard_alb \? 1 : 0$"
ENABLED_GUARD = r"(?m)^\s+count\s+= var\.crew_dashboard_enabled \? 1 : 0$"
CLOUDFRONT_GUARD = r"(?m)^\s+count\s+= local\.crew_dashboard_cloudfront \? 1 : 0$"


def _blocks(text, kinds=("resource", "data")):
    """(kind, type, name, body) for every top-level resource/data block."""
    pattern = r'^(%s) "([^"]+)" "([^"]+)" \{(.*?)(?=^(?:resource|data|locals|variable|output) |\Z)' % "|".join(kinds)
    return re.findall(pattern, text, re.MULTILINE | re.DOTALL)


class CrewDashboardRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crew = (INFRA / "crew-dashboard.tf").read_text()
        cls.variables = (INFRA / "variables.tf").read_text()

    def test_host_route_targets_gateway_and_publishes_origin(self):
        self.assertRegex(self.crew, r'resource "kubernetes_ingress_v1" "crew_dashboard" \{')
        self.assertIn("host = var.crew_dashboard_hostname", self.crew)
        self.assertIn('name = "bedrockgateway"', self.crew)
        self.assertIn('path      = "/"', self.crew)
        self.assertIn('name  = "/adp/${var.environment}/gateway/crew-dashboard-origin"', self.crew)
        self.assertIn("value = local.crew_dashboard_origin", self.crew)
        self.assertIn("depends_on = [aws_route53_record.crew_dashboard, aws_cloudfront_distribution.crew_dashboard]", self.crew)

    def test_crew_requires_same_site_frontend_custom_domain(self):
        self.assertIn('var.frontend_domain_name != ""', self.crew)
        self.assertIn('lower(var.frontend_domain_name) == trimsuffix(lower(data.aws_route53_zone.crew_dashboard[0].name), ".")', self.crew)
        self.assertIn(
            'endswith(lower(var.frontend_domain_name), ".${trimsuffix(lower(data.aws_route53_zone.crew_dashboard[0].name), ".")}")', self.crew
        )
        self.assertIn("SameSite=Lax dashboard cookies work", self.crew)

    def test_crew_host_cannot_fall_through_to_frontend(self):
        self.assertNotIn("module.cloudfront", self.crew)
        self.assertNotIn("module.s3_frontend", self.crew)
        self.assertNotIn("index.html", self.crew)
        self.assertRegex(self.crew, ALB_GUARD)
        self.assertRegex(
            self.variables,
            r'variable "crew_dashboard_enabled" \{[^}]*default\s+= false',
        )
        self.assertEqual(len(re.findall(r"\bhost\s+= var.crew_dashboard_hostname", self.crew)), 1)


class CrewDashboardHttpsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crew = (INFRA / "crew-dashboard.tf").read_text()

    def test_dns_validated_certificate_is_bound_to_https_listener(self):
        self.assertIn('validation_method = "DNS"', self.crew)
        self.assertIn("zone_id = var.crew_dashboard_zone_id", self.crew)
        self.assertIn("validation_record_fqdns = [aws_route53_record.crew_dashboard_validation[0].fqdn]", self.crew)
        self.assertIn("aws_acm_certificate_validation.crew_dashboard[0].certificate_arn", self.crew)
        self.assertIn("!data.aws_route53_zone.crew_dashboard[0].private_zone", self.crew)
        self.assertIn("endswith(lower(var.crew_dashboard_hostname)", self.crew)

    def test_crew_dns_alias_points_at_its_alb(self):
        self.assertIn("wait_for_load_balancer = true", self.crew)
        self.assertIn("kubernetes_ingress_v1.crew_dashboard[0].status[0].load_balancer[0].ingress[0].hostname", self.crew)
        self.assertIn("name                   = data.aws_lb.crew_dashboard[0].dns_name", self.crew)
        self.assertIn("zone_id                = data.aws_lb.crew_dashboard[0].zone_id", self.crew)

    @unittest.skipUnless(shutil.which("terraform"), "Terraform is required to evaluate the ALB lookup")
    def test_alb_lookup_removes_only_the_dns_suffix(self):
        lookup = re.search(
            r'data "aws_lb" "crew_dashboard" \{.*?^\s+name\s+= ([^\n]+)',
            self.crew,
            re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(lookup)
        hostname_reference = "kubernetes_ingress_v1.crew_dashboard[0].status[0].load_balancer[0].ingress[0].hostname"
        for name in ("k8s-adpgatew-crewdash-abc123def0", "crew-alb-42"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                hostname = f"{name}-1234567890.us-east-1.elb.amazonaws.com"
                expression = lookup.group(1).replace(hostname_reference, json.dumps(hostname))
                result = subprocess.run(
                    ["terraform", "console", "-no-color"],
                    input=expression + "\n",
                    text=True,
                    capture_output=True,
                    cwd=directory,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), name)

    def test_http_listener_cannot_serve_crew_without_tls(self):
        self.assertIn("jsonencode([{ HTTP = 80 }, { HTTPS = 443 }])", self.crew)
        self.assertIn('"alb.ingress.kubernetes.io/ssl-redirect"', self.crew)
        self.assertRegex(self.crew, r'"alb\.ingress\.kubernetes\.io/ssl-redirect"\s+= "443"')
        self.assertRegex(
            self.crew,
            r'"alb\.ingress\.kubernetes\.io/certificate-arn"\s+= aws_acm_certificate_validation\.crew_dashboard\[0\]\.certificate_arn',
        )


class CrewDashboardStreamingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crew = (INFRA / "crew-dashboard.tf").read_text()

    def test_streaming_uses_a_dedicated_alb_class(self):
        self.assertIn('kind       = "IngressClassParams"', self.crew)
        self.assertIn('scheme = "internet-facing"', self.crew)
        self.assertIn('controller = "eks.amazonaws.com/alb"', self.crew)
        self.assertIn("depends_on = [kubernetes_manifest.crew_dashboard_params]", self.crew)
        self.assertIn("ingress_class_name = kubernetes_ingress_class_v1.crew_dashboard[0].metadata[0].name", self.crew)
        self.assertNotIn("module.cloudfront", self.crew)

    def test_streaming_does_not_revert_to_sixty_second_idle_timeout(self):
        self.assertRegex(self.crew, r'"idle_timeout\.timeout_seconds"\s+= "300"')
        self.assertNotIn('"idle_timeout.timeout_seconds" = "60"', self.crew)
        self.assertNotIn("alb.ingress.kubernetes.io/load-balancer-attributes", self.crew)

    def test_gateway_forwards_host_cookies_and_websocket_upgrade_without_cache(self):
        self.assertIn('"routing.http.preserve_host_header.enabled" = "true"', self.crew)
        self.assertIn('"alb.ingress.kubernetes.io/target-type"', self.crew)
        self.assertIn('name = "bedrockgateway"', self.crew)
        self.assertNotIn("cache_policy_id", self.crew)
        self.assertNotIn("origin_request_policy_id", self.crew)
        self.assertNotIn('Connection = "close"', self.crew)


class CrewDashboardCompatibilityTests(unittest.TestCase):
    def test_disabled_by_default_disables_all_crew_resources(self):
        variables = (INFRA / "variables.tf").read_text()
        self.assertRegex(
            variables,
            r'variable "crew_dashboard_enabled" \{[^}]*default\s+= false',
        )
        crew = (INFRA / "crew-dashboard.tf").read_text()
        # Both mode locals derive from the master flag, so false disables every block
        # in either edge mode.
        self.assertRegex(crew, r'crew_dashboard_alb\s+= var\.crew_dashboard_enabled && var\.crew_dashboard_edge == "alb"')
        self.assertRegex(crew, r'crew_dashboard_cloudfront\s+= var\.crew_dashboard_enabled && var\.crew_dashboard_edge == "cloudfront"')
        blocks = _blocks(crew)
        self.assertEqual(len(blocks), 10)
        for kind, type_, name, body in blocks:
            guard = ENABLED_GUARD if (type_, name) == ("aws_ssm_parameter", "crew_dashboard_origin") else ALB_GUARD
            with self.subTest(block=f"{kind}.{type_}.{name}"):
                self.assertRegex(body, guard)
        cloudfront = (INFRA / "crew-dashboard-cloudfront.tf").read_text()
        for kind, type_, name, body in _blocks(cloudfront):
            with self.subTest(block=f"{kind}.{type_}.{name}"):
                self.assertRegex(body, CLOUDFRONT_GUARD)

    def test_existing_frontend_and_api_configuration_is_unchanged(self):
        gateway = (INFRA / "main.tf").read_text()
        cloudfront = (INFRA / "modules/cloudfront/main.tf").read_text()
        self.assertIn('module "cloudfront" {', gateway)
        self.assertNotIn("crew_dashboard", gateway)
        self.assertNotIn("crew_dashboard", cloudfront)
        self.assertIn('path_pattern     = "/api/*"', cloudfront)
        self.assertIn("target_origin_id = local.use_vpc_origin ? local.vpc_origin_id : local.alb_origin_id", cloudfront)
        self.assertIn("target_origin_id = local.s3_origin_id", cloudfront)

    def test_dev_tfvars_enables_the_cloudfront_edge_in_code_not_by_hand(self):
        """Dev turned the Crew dashboard on 2026-10-16 (owner go). It is declared here,
        as the CloudFront edge (no public zone), with checkpoints still off."""
        tfvars = (REPO / "environments/dev/modules/gateway.tfvars").read_text()
        active = "\n".join(line for line in tfvars.splitlines() if not line.lstrip().startswith("#"))
        self.assertRegex(active, r"(?m)^\s*crew_dashboard_enabled\s*=\s*true", msg="dev enables the Crew dashboard in tfvars")
        self.assertRegex(active, r'(?m)^\s*crew_dashboard_edge\s*=\s*"cloudfront"')
        self.assertNotRegex(active, r"^\s*crew_dashboard_hostname\s*=", msg="CloudFront mode must not set a hostname")
        self.assertNotRegex(active, r"^\s*crew_dashboard_zone_id\s*=")
        self.assertNotRegex(active, r"^\s*crew_checkpoints_enabled\s*=")
        self.assertIn("# crew_checkpoints_enabled = true", tfvars)


class CrewDashboardCloudFrontEdgeTests(unittest.TestCase):
    """crew_dashboard_edge = "cloudfront" (#7902): a zone-free edge on the existing VPC origin."""

    @classmethod
    def setUpClass(cls):
        cls.crew = (INFRA / "crew-dashboard.tf").read_text()
        cls.cloudfront = (INFRA / "crew-dashboard-cloudfront.tf").read_text()
        cls.variables = (INFRA / "variables.tf").read_text()

    def test_edge_variable_defaults_to_alb_and_rejects_other_values(self):
        block = re.search(r'variable "crew_dashboard_edge" \{.*?\n\}', self.variables, re.DOTALL).group(0)
        self.assertRegex(block, r'default\s+= "alb"')
        self.assertIn('contains(["alb", "cloudfront"], var.crew_dashboard_edge)', block)

    def test_cloudfront_mode_creates_no_certificate_dns_or_public_alb(self):
        # Every ALB-only block (ACM, Route 53, IngressClassParams, IngressClass, Ingress,
        # ALB lookup) is guarded by the ALB-mode local, which is false in CloudFront mode.
        alb_only = [
            (type_, name)
            for kind, type_, name, body in _blocks(self.crew)
            if type_
            in {
                "aws_route53_zone",
                "aws_acm_certificate",
                "aws_route53_record",
                "aws_acm_certificate_validation",
                "kubernetes_manifest",
                "kubernetes_ingress_class_v1",
                "kubernetes_ingress_v1",
                "aws_lb",
            }
        ]
        self.assertEqual(len(alb_only), 9)
        for kind, type_, name, body in _blocks(self.crew):
            if (type_, name) in alb_only:
                with self.subTest(block=f"{type_}.{name}"):
                    self.assertRegex(body, ALB_GUARD)
        for forbidden in ("aws_acm_certificate", "aws_route53", "kubernetes_ingress", "kubernetes_manifest", 'aws_lb"'):
            self.assertNotIn(forbidden, self.cloudfront)

    def test_cloudfront_mode_reuses_the_frontend_vpc_origin_without_caching(self):
        self.assertRegex(self.cloudfront, r'resource "aws_cloudfront_distribution" "crew_dashboard" \{')
        self.assertIn("vpc_origin_id            = module.cloudfront.vpc_origin_id", self.cloudfront)
        self.assertIn("domain_name = var.internal_alb_dns", self.cloudfront)
        self.assertRegex(self.cloudfront, r"origin_read_timeout\s+= 60")
        self.assertRegex(self.cloudfront, r"origin_keepalive_timeout\s+= 60")
        self.assertIn('name  = "Managed-CachingDisabled"', self.cloudfront)
        self.assertIn('name  = "Managed-AllViewer"', self.cloudfront)
        self.assertNotIn("AllViewerExceptHostHeader", self.cloudfront)
        self.assertIn('allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]', self.cloudfront)
        self.assertRegex(self.cloudfront, r'viewer_protocol_policy\s+= "https-only"')
        self.assertRegex(self.cloudfront, r'http_version\s+= "http2"')
        self.assertRegex(self.cloudfront, r"compress\s+= false")
        self.assertNotIn("response_headers_policy_id", self.cloudfront)
        self.assertNotIn('aws_cloudfront_vpc_origin"', self.cloudfront)
        self.assertNotIn("ordered_cache_behavior", self.cloudfront)

    def test_cloudfront_mode_matches_frontend_price_class_and_waf(self):
        self.assertIn('price_class = var.environment == "prod" ? "PriceClass_All" : "PriceClass_100"', self.cloudfront)
        self.assertIn('web_acl_id = var.cloudfront_waf_web_acl_arn != "" ? var.cloudfront_waf_web_acl_arn : null', self.cloudfront)
        self.assertIn("cloudfront_default_certificate = true", self.cloudfront)
        self.assertNotIn("aliases", self.cloudfront)

    def test_cloudfront_mode_refuses_hostname_zone_and_missing_vpc_origin(self):
        self.assertIn('condition     = var.crew_dashboard_hostname == "" && var.crew_dashboard_zone_id == ""', self.cloudfront)
        self.assertIn(
            'condition     = var.enable_vpc_origin && var.internal_alb_arn != "" && var.internal_alb_dns != "" && var.internal_alb_dns != "localhost"',
            self.cloudfront,
        )

    def test_both_modes_publish_the_origin_parameter(self):
        ssm = next(body for kind, type_, name, body in _blocks(self.crew) if (type_, name) == ("aws_ssm_parameter", "crew_dashboard_origin"))
        self.assertRegex(ssm, ENABLED_GUARD)
        self.assertIn("value = local.crew_dashboard_origin", ssm)
        self.assertIn(
            "crew_dashboard_origin = local.crew_dashboard_cloudfront "
            '? "https://${one(aws_cloudfront_distribution.crew_dashboard[*].domain_name)}" '
            ': "https://${var.crew_dashboard_hostname}"',
            self.crew,
        )
        outputs = (INFRA / "outputs.tf").read_text()
        self.assertIn('value       = var.crew_dashboard_enabled ? local.crew_dashboard_origin : ""', outputs)

    def test_cloudfront_mode_documents_the_shared_alb_routing_assumption(self):
        self.assertIn("path-only", self.cloudfront)
        self.assertIn("tools-facade", self.cloudfront)
        ingress = (INFRA.parent / "k8s/ingress.yaml").read_text()
        rules = ingress.split("spec:", 1)[1]
        self.assertNotRegex(rules, r"^\s+host:", msg="gateway Ingress rules must stay path-only for Host-based Crew routing")
        self.assertIn("name: bedrockgateway", rules)


class CrewProxyParameterTests(unittest.TestCase):
    def test_keepalive_and_connection_cap_parameters_follow_the_flag_pattern(self):
        flags = (INFRA / "chief-of-staff-flags.tf").read_text()
        self.assertRegex(flags, r'variable "crew_proxy_keepalive_seconds" \{[^}]*default\s+= 25')
        self.assertRegex(flags, r'variable "crew_proxy_max_conn_per_user" \{[^}]*default\s+= 64')
        self.assertIn("var.crew_proxy_max_conn_per_user >= 1 && var.crew_proxy_max_conn_per_user <= 64", flags)
        self.assertIn('name  = "/adp/${var.environment}/gateway/crew-proxy-keepalive-seconds"', flags)
        self.assertIn("value = tostring(var.crew_proxy_keepalive_seconds)", flags)
        self.assertIn('name  = "/adp/${var.environment}/gateway/crew-proxy-max-conn-per-user"', flags)
        self.assertIn("value = tostring(var.crew_proxy_max_conn_per_user)", flags)


class CrewCheckpointBucketTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tf = (INFRA / "crew-checkpoints.tf").read_text()

    def test_disabled_by_default_and_everything_gated(self):
        self.assertRegex(self.tf, r'variable "crew_checkpoints_enabled" \{[^}]*default\s+= false')
        self.assertRegex(self.tf, r'variable "crew_checkpoint_retention_days" \{[^}]*default\s+= 30')
        always = {("aws_ssm_parameter", "crew_checkpoints_enabled"), ("aws_ssm_parameter", "crew_checkpoint_retention_seconds")}
        blocks = _blocks(self.tf)
        self.assertGreaterEqual(len(blocks), 9)
        for kind, type_, name, body in blocks:
            with self.subTest(block=f"{type_}.{name}"):
                if (type_, name) in always:
                    self.assertNotIn("count", body)
                else:
                    self.assertRegex(body, r"(?m)^\s+count\s+= var\.crew_checkpoints_enabled \? 1 : 0$")

    def test_bucket_is_private_versioned_encrypted_and_tls_only(self):
        self.assertIn(
            'crew_checkpoint_bucket_name = "adp-${var.environment}-crew-checkpoints-${data.aws_caller_identity.current.account_id}"', self.tf
        )
        self.assertIn('status = "Enabled"', self.tf)
        for setting in ("block_public_acls", "block_public_policy", "ignore_public_acls", "restrict_public_buckets"):
            self.assertRegex(self.tf, rf"{setting}\s+= true")
        self.assertIn('sse_algorithm     = "aws:kms"', self.tf)
        self.assertIn('kms_master_key_id = var.crew_checkpoint_kms_key_arn != "" ? var.crew_checkpoint_kms_key_arn : null', self.tf)
        self.assertIn('"aws:SecureTransport" = "false"', self.tf)
        self.assertNotIn("AES256", self.tf)

    def test_retention_expires_current_and_noncurrent_objects(self):
        self.assertRegex(self.tf, r"expiration \{\s+days = var\.crew_checkpoint_retention_days")
        self.assertRegex(self.tf, r"noncurrent_version_expiration \{\s+noncurrent_days = var\.crew_checkpoint_retention_days")
        self.assertIn("value = tostring(var.crew_checkpoint_retention_days * 86400)", self.tf)

    def test_gateway_role_gets_only_the_checkpoint_operations(self):
        policy = next(body for kind, type_, name, body in _blocks(self.tf) if (type_, name) == ("aws_iam_role_policy", "gateway_crew_checkpoints_s3"))
        self.assertIn("role  = local.gateway_service_irsa_role_name", policy)
        self.assertIn('Action   = ["s3:GetBucketVersioning", "s3:GetBucketLocation"]', policy)
        self.assertIn('Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion", "s3:DeleteObjectVersion"]', policy)
        self.assertIn('Resource = "${aws_s3_bucket.crew_checkpoints[0].arn}/${local.crew_checkpoint_prefix}*"', policy)
        self.assertIn('crew_checkpoint_prefix      = "crew-checkpoints/"', self.tf)
        self.assertNotIn("s3:*", policy)
        self.assertNotIn("s3:ListBucket", policy)

    def test_bucket_parameter_is_published_for_the_renderers(self):
        self.assertIn('name  = "/adp/${var.environment}/gateway/crew-checkpoint-bucket"', self.tf)
        self.assertIn("value = aws_s3_bucket.crew_checkpoints[0].bucket", self.tf)
        self.assertIn('name  = "/adp/${var.environment}/gateway/crew-checkpoints-enabled"', self.tf)
        self.assertIn('name  = "/adp/${var.environment}/gateway/crew-checkpoint-retention-seconds"', self.tf)


if __name__ == "__main__":
    unittest.main()
