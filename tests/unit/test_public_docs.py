"""Public examples must not silently acquire live deployment identities."""

from pathlib import Path
import runpy
import unittest


CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/check-public-docs.py")
)


class PublicDocsTests(unittest.TestCase):
    def test_detects_nonexample_identities_without_echoing_values(self):
        values = (
            "arn:aws:iam::555544443333:role/example",
            "https://d123456789abcd.cloudfront.net/api",
            "https://internal-demo.us-east-1.elb.amazonaws.com",
            "vpc-abcdef01234567890",
            "us-east-1_RealPool123",
        )
        for value in values:
            result = list(CHECK["findings"](value))
            self.assertTrue(result, value)
            self.assertNotIn(value, repr(result))

    def test_preserves_examples_public_links_and_vendor_endpoints(self):
        text = "\n".join(
            (
                "arn:aws:iam::000000000101:role/example",
                "arn:aws:iam::123456789012:role/example",
                "https://gateway.example.com/api",
                "https://docs.aws.amazon.com/",
                "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem",
                "https://github.com/example/project/actions/runs/123456789/job/555544443333",
                "vpc-00000000000000001 us-east-1_Example001",
            )
        )
        self.assertEqual(list(CHECK["findings"](text)), [])


if __name__ == "__main__":
    unittest.main()
