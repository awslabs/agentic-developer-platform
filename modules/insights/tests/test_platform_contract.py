"""Guard deployment defaults before a live plan can be run by the owner."""

import pathlib
import unittest


PLATFORM = pathlib.Path(__file__).resolve().parents[1] / "platform"


class PlatformContractTest(unittest.TestCase):
    def test_evidence_bucket_is_versioned_private_and_encrypted(self):
        source = (PLATFORM / "evidence.tf").read_text()
        for marker in ('"${local.prefix}-evidence"', 'force_destroy = false', 'aws_s3_bucket_versioning',
                       'aws_s3_bucket_public_access_block', 'aws_s3_bucket_server_side_encryption_configuration',
                       'aws_kms_key.evidence.arn', '"assurance/v1/"'):
            self.assertIn(marker, source)

    def test_services_default_off_and_network_egress_has_no_open_cidr(self):
        source = (PLATFORM / "workloads.tf").read_text()
        self.assertIn('replicas = 0', source)
        self.assertIn('policy_types = ["Ingress", "Egress"]', source)
        self.assertIn('data.aws_prefix_list.s3.cidr_blocks', source)
        self.assertIn('var.github_api_cidrs', source)
        self.assertNotIn('cidr = "0.0.0.0/0"', source)

    def test_roles_bound_to_distinct_service_accounts(self):
        source = (PLATFORM / "identity.tf").read_text()
        self.assertIn('system:serviceaccount:adp-insights:insights-${each.key}', source)
        self.assertIn('"${local.oidc_issuer}:aud" = "sts.amazonaws.com"', source)
        self.assertIn('aws_kms_key.evidence.arn', source)


if __name__ == "__main__":
    unittest.main()
