"""
Static checks on the S3 Vectors IRSA IAM policy (issue #2486).

The IRSA role could not create personal-context indexes because the policy
scoped every action to the wrong resource ARN form (``vector-bucket/<name>``).
The real S3 Vectors resource ARNs (AWS Service Authorization Reference,
service prefix ``s3vectors``) are:

    VectorBucket : arn:aws:s3vectors:<region>:<account>:bucket/<bucket-name>
    Index        : arn:aws:s3vectors:<region>:<account>:bucket/<bucket-name>/index/<index-name>

These tests read the module source directly (no terraform binary / AWS needed),
so they run in plain unit mode.
"""

from __future__ import annotations

from ..config import TERRAFORM_DIR

S3_VECTORS_MAIN_TF = TERRAFORM_DIR / "modules" / "s3-vectors" / "main.tf"


def _policy_source() -> str:
    """Return the module source with comment lines stripped.

    Comments legitimately mention the old ``vector-bucket/`` form to explain
    the fix, so assertions run against executable HCL only.
    """
    assert S3_VECTORS_MAIN_TF.is_file(), f"missing {S3_VECTORS_MAIN_TF}"
    lines = [
        line
        for line in S3_VECTORS_MAIN_TF.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    return "\n".join(lines)


class TestS3VectorsPolicyArns:
    """The IRSA policy must use the correct S3 Vectors resource ARN forms."""

    def test_no_legacy_vector_bucket_arn(self):
        """The wrong ``vector-bucket/`` ARN form must be gone from the policy.

        Guard against regressing to the form that matched no request and got
        every CreateIndex/QueryVectors/PutVectors call denied.
        """
        assert "vector-bucket/" not in _policy_source(), (
            "policy still uses the invalid 'vector-bucket/<name>' ARN form; "
            "S3 Vectors ARNs use 'bucket/<name>' and 'bucket/<name>/index/<name>'"
        )

    def test_bucket_level_arn_present(self):
        """Bucket-level actions scoped to the VectorBucket resource ARN."""
        assert ":bucket/adp-*" in _policy_source()

    def test_index_level_arn_present(self):
        """Index-level actions scoped to the Index resource ARN."""
        assert ":bucket/adp-*/index/*" in _policy_source()

    def test_create_index_granted(self):
        """CreateIndex — the action the remember/experience flow was denied."""
        assert "s3vectors:CreateIndex" in _policy_source()

    def test_query_and_put_vectors_granted(self):
        """QueryVectors + PutVectors — the recall/save round-trip actions."""
        src = _policy_source()
        assert "s3vectors:QueryVectors" in src
        assert "s3vectors:PutVectors" in src

    def test_list_vector_buckets_on_star(self):
        """ListVectorBuckets is account-scoped and must be granted on '*'."""
        src = _policy_source()
        assert "s3vectors:ListVectorBuckets" in src
        assert 'resources = ["*"]' in src
