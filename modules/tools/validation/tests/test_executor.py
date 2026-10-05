"""EKS presigning binds cluster, partition, expiry and session identity."""

import base64
from urllib.parse import parse_qs, urlsplit

from botocore.credentials import Credentials
import pytest

from validation_tools.executor import eks_token
from lib.codex_validation import ValidationUnavailable


@pytest.mark.parametrize("region,suffix", [("us-east-1", "amazonaws.com"), ("cn-north-1", "amazonaws.com.cn")])
def test_eks_token_binds_cluster_and_sts(region, suffix):
    token = eks_token(cluster="validation", region=region, credentials=Credentials("test", "secret", "session"))
    encoded = token.removeprefix("k8s-aws-v1.")
    parsed = urlsplit(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
    assert parsed.hostname == f"sts.{region}.{suffix}"
    query = parse_qs(parsed.query)
    assert query["Action"] == ["GetCallerIdentity"]
    assert query["X-Amz-Expires"] == ["60"]
    assert query["X-Amz-SignedHeaders"] == ["host;x-k8s-aws-id"]
    assert query["X-Amz-Security-Token"] == ["session"]
    assert token != eks_token(cluster="other", region=region, credentials=Credentials("test", "secret", "session"))


def test_invalid_cluster_cannot_change_signed_request():
    with pytest.raises(ValidationUnavailable):
        eks_token(cluster="bad\nheader", region="us-east-1", credentials=Credentials("test", "secret"))
