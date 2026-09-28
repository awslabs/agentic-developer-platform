"""AWS-authenticated, CA-pinned issuer/projector transport shared by both lifecycles."""

import base64
from pathlib import Path

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants
from superplane_bootstrap.namespace_admission import NamespaceAdmission

from ..credentials import assume_session


def _identity(source, session, actor, verify):
    verify()
    role = session.client("iam").get_role(RoleName=actor["role_arn"].rsplit("/", 1)[1])[
        "Role"
    ]
    verify()
    caller = session.client("sts").get_caller_identity()
    verify()
    if (
        role.get("Arn") != actor["role_arn"]
        or role.get("RoleId") != actor["role_id"]
        or caller.get("UserId", "").split(":", 1)[0] != actor["role_id"]
    ):
        raise BootstrapRefused("installed credential principal was replaced")


def _entry(session, target, actor, verify):
    verify()
    observed = session.client("eks", region_name=target.region).describe_access_entry(
        clusterName=target.cluster_name,
        principalArn=actor["role_arn"],
    )["accessEntry"]
    verify()
    if any(
        observed.get(key) != value
        for key, value in {
            "accessEntryArn": actor["access_entry_arn"],
            "principalArn": actor["role_arn"],
            "username": actor["username"],
            "kubernetesGroups": [actor["group"]],
            "type": "STANDARD",
        }.items()
    ):
        raise BootstrapRefused("installed credential EKS entry changed")


def _transport(authority, source, verify, directory, workspace_id, *, management):
    import boto3
    from botocore.signers import RequestSigner
    from kubernetes import client
    from kubernetes.dynamic import DynamicClient

    if not isinstance(source, boto3.Session):
        raise BootstrapRefused(
            "credential transport requires a privately delivered AWS session"
        )
    target = authority.target(workspace_id, management=management)
    actor = authority.document["projector" if management else "issuer"]
    session = assume_session(
        source, role_arn=actor["role_arn"], region=target.region, verify=verify
    )
    _identity(source, session, actor, verify)
    _entry(session, target, actor, verify)
    verify()
    observed = session.client("eks", region_name=target.region).describe_cluster(
        name=target.cluster_name
    )["cluster"]
    verify()
    if (
        observed.get("arn") != target.cluster_arn
        or observed.get("endpoint") != target.endpoint
        or observed.get("certificateAuthority", {}).get("data")
        != target.certificate_authority_data
        or observed.get("status") != "ACTIVE"
    ):
        raise BootstrapRefused("installed credential cluster TLS identity changed")
    path = Path(directory) / ("projector.ca.pem" if management else "issuer.ca.pem")
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(base64.b64decode(target.certificate_authority_data, validate=True))
    config = client.Configuration()
    config.host, config.ssl_ca_cert = target.endpoint, str(path)
    config.verify_ssl, config.proxy = True, None
    config.api_key_prefix["authorization"] = "Bearer"

    def refresh(selected):
        verify()
        _identity(source, session, actor, verify)
        _entry(session, target, actor, verify)
        sts = session.client("sts", region_name=target.region)
        signer = RequestSigner(
            sts.meta.service_model.service_id,
            target.region,
            "sts",
            "v4",
            session.get_credentials(),
            session._session.get_component("event_emitter"),
        )
        url = signer.generate_presigned_url(
            {
                "method": "GET",
                "url": sts.meta.endpoint_url
                + "/?Action=GetCallerIdentity&Version=2011-06-15",
                "body": {},
                "headers": {"x-k8s-aws-id": target.cluster_name},
                "context": {},
            },
            region_name=target.region,
            expires_in=60,
            operation_name="",
        )
        selected.api_key["authorization"] = "k8s-aws-v1." + base64.urlsafe_b64encode(
            url.encode()
        ).decode().rstrip("=")
        verify()

    config.refresh_api_key_hook = refresh
    api = client.ApiClient(config)

    class Deferred:
        def __init__(self):
            self.client, self._dynamic = api, None

        @property
        def resources(self):
            verify()
            if self._dynamic is None:
                self._dynamic = DynamicClient(api)
            return self._dynamic.resources

    return KubeGrants(Deferred(), target)


def compose_transports(
    authority,
    source_session,
    verify,
    directory,
    workspace_id,
    *,
    management_source_session=None,
):
    """No ambient kubeconfig, static API token or completed operation is consulted.

    Bootstrap supplies fresh operation verification; renewal supplies installed
    registry lease verification. Every Kubernetes request refreshes authenticated
    actor/entry checks as well as current service authority.
    """
    issuer = projector = None
    try:
        issuer = _transport(
            authority, source_session, verify, directory, workspace_id, management=False
        )
        projector = _transport(
            authority,
            management_source_session or source_session,
            verify,
            directory,
            workspace_id,
            management=True,
        )
        NamespaceAdmission(
            issuer, authority.cluster_reference(), "unused"
        ).verify_policy()
        return issuer, projector
    except BaseException:
        for grants in (issuer, projector):
            if grants is not None:
                grants.client.client.close()
        raise
