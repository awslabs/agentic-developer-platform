"""Dedicated service IAM authenticates to EKS; no cluster token reaches workers."""

import base64
from contextlib import contextmanager
import os
from pathlib import Path
import re
import tempfile

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest

from lib.codex_kubernetes_validation import KubernetesValidationAPI, KubernetesValidationExecutor
from lib.codex_validation import ValidationUnavailable


def eks_token(*, cluster, region, credentials):
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region)):
        raise ValidationUnavailable("Validation cluster configuration invalid")
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    request = AWSRequest(method="GET", url=f"https://sts.{region}.{suffix}/?Action=GetCallerIdentity&Version=2011-06-15",
                         headers={"x-k8s-aws-id": cluster})
    SigV4QueryAuth(credentials.get_frozen_credentials(), "sts", region, expires=60).add_auth(request)
    return "k8s-aws-v1." + base64.urlsafe_b64encode(request.url.encode()).decode().rstrip("=")


@contextmanager
def service_executor(task_id):
    region = os.environ.get("AWS_REGION", "us-east-1")
    session = boto3.Session(region_name=region)
    credentials = session.get_credentials()
    if credentials is None:
        raise ValidationUnavailable("Validation service IAM identity unavailable")
    with tempfile.TemporaryDirectory(prefix="adp-validation-ca-") as directory:
        ca = Path(directory) / "ca.crt"
        ca.write_bytes(base64.b64decode(os.environ["ADP_VALIDATION_CLUSTER_CA"], validate=True))
        api = KubernetesValidationAPI(endpoint=os.environ["ADP_VALIDATION_CLUSTER_ENDPOINT"], ca_file=ca,
            token_provider=lambda: eks_token(cluster=os.environ["ADP_VALIDATION_CLUSTER_NAME"], region=region, credentials=credentials))
        try:
            yield KubernetesValidationExecutor(api=api, namespace=os.environ["ADP_VALIDATION_NAMESPACE"], task_id=task_id)
        finally:
            api.session.close()
