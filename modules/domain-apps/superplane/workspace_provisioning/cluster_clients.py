"""Explicit EKS-derived Kubernetes transport; no ambient kubeconfig or proxy state."""

import base64
from datetime import UTC, datetime, timedelta
from threading import Lock
from types import SimpleNamespace

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
    configuration.api_key_prefix["BearerToken"] = "Bearer"
    token_lock = Lock()
    cached_token = None
    refresh_at = datetime.min.replace(tzinfo=UTC)

    def refresh(selected):
        nonlocal cached_token, refresh_at
        # Every request still checks its current operation authority. Signing a
        # token uses the already scoped SDK session, without an AWS CLI process
        # and its separate credential bridge for every discovery request.
        process.verify()
        with token_lock:
            now = datetime.now(UTC)
            if cached_token is None or now >= refresh_at:
                from botocore.signers import RequestSigner

                credentials = process.session.get_credentials()
                if credentials is None:
                    raise LifecycleRefused(
                        "scoped Kubernetes credentials are unavailable"
                    )
                # Trigger the session's normal refresh before checking its expiry.
                credentials.get_frozen_credentials()
                expiry = getattr(credentials, "_expiry_time", None)
                if expiry is None or expiry.tzinfo is None or expiry <= now:
                    raise LifecycleRefused(
                        "expiring Kubernetes credentials are required"
                    )
                sts = process.session.client(
                    "sts",
                    region_name=outputs["aws_region"],
                    endpoint_url=f"https://sts.{outputs['aws_region']}.amazonaws.com",
                )
                signer = RequestSigner(
                    sts.meta.service_model.service_id,
                    outputs["aws_region"],
                    "sts",
                    "v4",
                    credentials,
                    sts.meta.events,
                )
                url = signer.generate_presigned_url(
                    {
                        "method": "GET",
                        "url": sts.meta.endpoint_url.rstrip("/")
                        + "/?Action=GetCallerIdentity&Version=2011-06-15",
                        "body": {},
                        "headers": {"x-k8s-aws-id": outputs["cluster_name"]},
                        "context": {},
                    },
                    region_name=outputs["aws_region"],
                    expires_in=60,
                    operation_name="",
                )
                process.verify()
                cached_token = "k8s-aws-v1." + base64.urlsafe_b64encode(
                    url.encode()
                ).decode().rstrip("=")
                refresh_at = min(now + timedelta(minutes=14), expiry) - timedelta(
                    seconds=60
                )
            selected.api_key["authorization"] = cached_token

    configuration.refresh_api_key_hook = refresh

    class BoundedApiClient(client.ApiClient):
        def call_api(self, *args, **kwargs):
            if kwargs.get("_request_timeout") is None:
                kwargs["_request_timeout"] = (5, 30)
            return super().call_api(*args, **kwargs)

    class DeferredDiscovery:
        def __init__(self):
            self.client = BoundedApiClient(configuration)
            self._dynamic = None
            self._discovery_lock = Lock()
            self._resources = SimpleNamespace(get=self._get_resource)

        def _get_resource(self, **kwargs):
            # Discovery mutates its resource cache; individual resource HTTP
            # reads may run concurrently once lookup has returned.
            with self._discovery_lock:
                return self._dynamic.resources.get(**kwargs)

        def self_subject_access_review(self, body, *, target):
            """Review this actor without CLI startup or Kubernetes discovery."""
            from superplane_bootstrap.kube_grants import KubeGrants

            transport = KubeGrants(self, target)
            # Seed authentication before auth_settings() is evaluated. The hook
            # still performs a fresh operation check for every HTTP request.
            refresh(self.client.configuration)
            result = self.client.call_api(
                "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                "POST",
                header_params={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
                body=body,
                response_type="object",
                auth_settings=["BearerToken"],
                _return_http_data_only=True,
                _request_timeout=(5, 30),
            )
            process.verify()
            transport._verify_transport()
            return result

        @property
        def resources(self):
            process.verify()
            with self._discovery_lock:
                if self._dynamic is None:
                    # Seed authentication before discovery's first request.
                    refresh(self.client.configuration)
                    self._dynamic = DynamicClient(
                        self.client,
                        cache_file=str(process.directory / (name + ".discovery.json")),
                    )
            return self._resources

    return DeferredDiscovery()


def scoped_entry_client(
    source,
    *,
    role_arn,
    region,
    verify,
    external_id=None,
    session_name="superplane-lifecycle",
):
    """Return only exact immutable-entry revocation sessions required by EksGrants."""

    def resolve(entry_arn):
        if not isinstance(entry_arn, str) or not entry_arn.startswith(
            f"arn:aws:eks:{region}:{role_arn.split(':')[4]}:access-entry/"
        ):
            raise LifecycleRefused(
                "EKS revocation identity belongs to another account or region"
            )
        if getattr(source, "_superplane_scoped_entry", None) is not None:
            verify()
            session = source._superplane_scoped_entry(entry_arn)
            verify()
            return session.client("eks", region_name=region)
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
            session_name=session_name,
        ).client("eks", region_name=region)

    return resolve
