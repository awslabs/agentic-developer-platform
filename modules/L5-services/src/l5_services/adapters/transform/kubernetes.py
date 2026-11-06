"""Private EKS transport owned by the installed L5 control workload."""

import base64
import json
import re
import ssl
from urllib.parse import urlsplit

import boto3
import httpx
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest


class TransformKubernetesAPI:
    def __init__(self, *, endpoint, cluster, ca, region):
        parsed = urlsplit(endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".eks.amazonaws.com")
                or parsed.netloc != parsed.hostname or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", cluster)
                or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]", region)):
            raise ValueError("Transform requires protected private EKS configuration")
        self.endpoint, self.cluster, self.region = endpoint.rstrip("/"), cluster, region
        self.session = boto3.Session(region_name=region)
        self.client = httpx.Client(verify=ssl.create_default_context(
            cadata=base64.b64decode(ca, validate=True).decode()), trust_env=False, follow_redirects=False)

    def request(self, method, path, *, body=None, params=None, raw=False, maximum=1024 * 1024, timeout_seconds=2, metadata_only=False):
        if not path.startswith(("/api/v1/", "/apis/networking.k8s.io/v1/")):
            raise ValueError("Transform cluster request path rejected")
        if metadata_only and (method != "GET" or raw or body is not None):
            raise ValueError("Transform metadata observation must be read-only")
        credentials = self.session.get_credentials()
        if credentials is None:
            raise RuntimeError("Transform control workload identity unavailable")
        signed = AWSRequest(method="GET", url=f"https://sts.{self.region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
                            headers={"x-k8s-aws-id": self.cluster})
        SigV4QueryAuth(credentials.get_frozen_credentials(), "sts", self.region, expires=60).add_auth(signed)
        token = "k8s-aws-v1." + base64.urlsafe_b64encode(signed.url.encode()).decode().rstrip("=")
        headers = {"Authorization": "Bearer " + token}
        if metadata_only:
            # No fallback to full Secret/ConfigMap representations.
            headers["Accept"] = "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1"
        if method == "PATCH":
            headers["Content-Type"] = "application/json-patch+json"
        with self.client.stream(method, self.endpoint + path, json=body, params=params,
                                headers=headers, timeout=timeout_seconds) as response:
            if response.status_code == 404 and method in {"GET", "DELETE"}:
                return None
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes(65536):
                content.extend(chunk)
                if len(content) > maximum:
                    raise ValueError("Transform cluster response exceeds its bound")
            value = bytes(content) if raw else json.loads(content)
            if metadata_only:
                if (not isinstance(value, dict) or value.get("kind") != "PartialObjectMetadata"
                        or value.get("apiVersion") != "meta.k8s.io/v1"
                        or set(value) - {"kind", "apiVersion", "metadata"}):
                    raise ValueError("Transform metadata response included unsupported resource contents")
                return value
            return value
