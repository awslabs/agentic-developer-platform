"""Kubernetes transport signed by the registered user's exact AWS session."""

import base64
from datetime import UTC, datetime
import json
import re
import ssl
import urllib.error
import urllib.request

from .http import NoRedirect, Unsupported


class KubernetesUnavailable(Unsupported):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"scoped Kubernetes request returned HTTP {status_code}")


class ScopedKubernetes:
    def __init__(self, client, cluster_name):
        import boto3
        from botocore.config import Config
        from botocore.signers import RequestSigner

        bounded = Config(
            connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}
        )
        self.session = boto3.Session()
        sts = self.session.client("sts", config=bounded)
        identity = sts.get_caller_identity()
        role = registered_role(client, identity)
        self.cluster = self.session.client("eks", config=bounded).describe_cluster(
            name=cluster_name
        )["cluster"]
        if (
            self.cluster["arn"].split(":")[4] != identity["Account"]
            or self.cluster["name"] != cluster_name
        ):
            raise Unsupported("cluster account/name mismatch")
        from urllib.parse import urlsplit

        endpoint = urlsplit(self.cluster["endpoint"])
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise Unsupported("invalid EKS endpoint")
        region = self.cluster["arn"].split(":")[3]
        signer = RequestSigner(
            sts.meta.service_model.service_id,
            region,
            "sts",
            "v4",
            self.session.get_credentials(),
            self.session.events,
        )
        url = signer.generate_presigned_url(
            {
                "method": "GET",
                "url": f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
                "body": {},
                "headers": {"x-k8s-aws-id": cluster_name},
                "context": {},
            },
            region_name=region,
            expires_in=60,
            operation_name="",
        )
        self._bearer = "k8s-aws-v1." + base64.urlsafe_b64encode(
            url.encode()
        ).decode().rstrip("=")
        ca = base64.b64decode(self.cluster["certificateAuthority"]["data"]).decode()
        context = ssl.create_default_context(cadata=ca)
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=context)
        )
        self.identity, self.role, self.bounded = identity, role, bounded

    def request(self, method, path, body=None):
        if not path.startswith("/") or path.startswith("//") or ".." in path:
            raise ValueError("invalid Kubernetes path")
        request = urllib.request.Request(
            self.cluster["endpoint"] + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": "Bearer " + self._bearer,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as exc:
            raise KubernetesUnavailable(exc.code) from None
        with response:
            payload = response.read(8 * 1024 * 1024 + 1)
            if len(payload) > 8 * 1024 * 1024:
                raise Unsupported("Kubernetes observation too large")
            return json.loads(payload)

    def get(self, path):
        return self.request("GET", path)

    def kubeconfig(self):
        # Fed on stdin to kubectl. Never use ambient kubeconfig exec plugins,
        # which could authenticate as a different, broader AWS profile.
        return json.dumps(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": "qualification",
                "clusters": [
                    {
                        "name": "qualification",
                        "cluster": {
                            "server": self.cluster["endpoint"],
                            "certificate-authority-data": self.cluster[
                                "certificateAuthority"
                            ]["data"],
                        },
                    }
                ],
                "users": [{"name": "qualification", "user": {"token": self._bearer}}],
                "contexts": [
                    {
                        "name": "qualification",
                        "context": {
                            "cluster": "qualification",
                            "user": "qualification",
                        },
                    }
                ],
            }
        )


def registered_role(client, identity):
    rows = client.get("/auth/credentials")
    matches = [
        r
        for r in rows
        if r["id"] == client.config.connection_ref
        and r.get("service") == "aws"
        and r.get("credential_type") == "aws_role"
    ]
    row = matches[0] if len(matches) == 1 else None
    scopes = (row or {}).get("scopes") or {}
    role = scopes.get("role_arn", "")
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/(.+)", role)
    expires = (row or {}).get("expires_at")
    if (
        not match
        or scopes.get("status") != "verified"
        or identity["Account"] != client.config.expected_account_id
        or match[1] != identity["Account"]
        or not identity["Arn"].startswith(
            f"arn:aws:sts::{match[1]}:assumed-role/{match[2].split('/')[-1]}/"
        )
        or (
            expires
            and datetime.fromisoformat(expires.replace("Z", "+00:00"))
            <= datetime.now(UTC)
        )
    ):
        raise Unsupported(
            "Kubernetes requires the registered role; platform credentials are not a fallback"
        )
    return role
