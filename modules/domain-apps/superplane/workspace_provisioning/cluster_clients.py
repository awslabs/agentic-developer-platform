"""Explicit EKS-derived Kubernetes transport; no ambient kubeconfig or proxy state."""

import base64
import json

from .artifacts import canonical
from .credentials import assume_session
from .runtime_config import LifecycleRefused


def write_kubeconfig(process, outputs, *, name):
    process.verify()
    path = process.directory / (name + ".kubeconfig.json")
    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "current-context": "workspace",
        "clusters": [
            {
                "name": "workspace",
                "cluster": {
                    "server": outputs["cluster_endpoint"],
                    "certificate-authority-data": outputs[
                        "cluster_certificate_authority_data"
                    ],
                },
            }
        ],
        "contexts": [
            {
                "name": "workspace",
                "context": {"cluster": "workspace", "user": "operation-role"},
            }
        ],
        "users": [
            {
                "name": "operation-role",
                "user": {
                    "exec": {
                        "apiVersion": "client.authentication.k8s.io/v1beta1",
                        "command": process.binaries["aws"],
                        "args": [
                            "eks",
                            "get-token",
                            "--region",
                            outputs["aws_region"],
                            "--cluster-name",
                            outputs["cluster_name"],
                        ],
                        "interactiveMode": "Never",
                    }
                },
            }
        ],
    }
    # Exclusive creation and private directory prevent another attempt from
    # replacing the transport before KubectlClusterAccess takes its snapshot.
    with path.open("x") as stream:
        path.chmod(0o600)
        stream.write(canonical(document))
    return path


def kubernetes_client(process, outputs, *, name):
    from kubernetes import client
    from kubernetes.dynamic import DynamicClient

    process.verify()
    certificate = process.directory / (name + ".ca.pem")
    try:
        data = base64.b64decode(
            outputs["cluster_certificate_authority_data"], validate=True
        )
    except (ValueError, TypeError):
        raise LifecycleRefused("verified EKS CA data is invalid") from None
    with certificate.open("xb") as stream:
        certificate.chmod(0o600)
        stream.write(data)
    configuration = client.Configuration()
    configuration.host = outputs["cluster_endpoint"]
    configuration.ssl_ca_cert = str(certificate)
    configuration.verify_ssl = True
    configuration.proxy = None
    configuration.api_key_prefix["authorization"] = "Bearer"

    def refresh(selected):
        process.verify()
        result = json.loads(
            process.checked(
                [
                    "aws",
                    "eks",
                    "get-token",
                    "--region",
                    outputs["aws_region"],
                    "--cluster-name",
                    outputs["cluster_name"],
                ]
            )
        )
        token = result["status"]["token"]
        if not isinstance(token, str) or not token.startswith("k8s-aws-v1."):
            raise LifecycleRefused("EKS did not supply a scoped Kubernetes token")
        selected.api_key["authorization"] = token
        process.verify()

    configuration.refresh_api_key_hook = refresh

    class DeferredDiscovery:
        def __init__(self):
            self.client = client.ApiClient(configuration)
            self._dynamic = None

        @property
        def resources(self):
            process.verify()
            if self._dynamic is None:
                self._dynamic = DynamicClient(self.client)
            return self._dynamic.resources

    return DeferredDiscovery()


def scoped_entry_client(source, *, role_arn, region, verify, external_id=None):
    """Return only exact immutable-entry revocation sessions required by EksGrants."""

    def resolve(entry_arn):
        if not isinstance(entry_arn, str) or not entry_arn.startswith(
            f"arn:aws:eks:{region}:{role_arn.split(':')[4]}:access-entry/"
        ):
            raise LifecycleRefused(
                "EKS revocation identity belongs to another account or region"
            )
        policy = canonical(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "eks:DeleteAccessEntry",
                            "eks:DisassociateAccessPolicy",
                            "eks:DescribeAccessEntry",
                            "eks:ListAssociatedAccessPolicies",
                        ],
                        "Resource": entry_arn,
                    }
                ],
            }
        )
        return assume_session(
            source,
            role_arn=role_arn,
            region=region,
            verify=verify,
            external_id=external_id,
            policy=policy,
        ).client("eks", region_name=region)

    return resolve
