"""Kubeconfig generation — the exceptional, export-a-credential-holder path.

Brokered API operations (``app.services.proxy``) are the default: the control plane
performs the work for an authorized caller and no credential leaves the server. Exporting
a kubeconfig is deliberately the exception, because it hands a user something they run
themselves, outside the control plane's authorization and audit path.

Two properties keep that export safe:

**The file contains no credential.** Earlier revisions wrote the STS ``SessionToken``
into ``users[0].user.token``. That is wrong twice over: the Kubernetes API server cannot
verify an STS SessionToken (so the file did not work), and it copied a live credential
into a file users download and keep (so it leaked one). Instead the file carries an
``exec`` block — the standard client-go credential plugin — telling ``kubectl`` to run
``aws eks get-token`` itself at the moment of use. The exported file therefore grants
nothing on its own: whoever holds it must still be able to assume the workspace role.

**TLS verification is mandatory.** A kubeconfig without ``certificate-authority-data``
makes clients fall back to the system trust store or to ``insecure-skip-tls-verify``, so
generation is refused when the cluster CA is unavailable rather than emitting a config
that silently connects unverified.

One consequence of exporting rather than brokering: when the tenant's role requires an
``sts:ExternalId``, the exec plugin cannot supply it. ``aws eks get-token`` has no flag
for it and the CLI has no environment variable for it, so the config delegates role
assumption to a locally configured AWS profile. Brokered calls have no such limitation —
they pass ``ExternalId`` directly — which is another reason brokering is the default.
"""

import logging
from datetime import datetime, timedelta, timezone

import yaml

from app.config import settings
from app.services.eks_auth import EksAuthError, validate_ca_data

logger = logging.getLogger(__name__)

# Lifetime advertised to the caller for the exported config's own validity window.
# The exec plugin fetches a fresh token on each invocation, so this bounds how long the
# export is advertised as usable, not the lifetime of any embedded secret — there is none.
TOKEN_DURATION_SECONDS = 900

# API version of the client-go credential plugin contract that `aws eks get-token`
# speaks. v1beta1 is what current kubectl and the AWS CLI agree on.
EXEC_API_VERSION = "client.authentication.k8s.io/v1beta1"


class KubeconfigError(Exception):
    """Raised when a kubeconfig cannot be generated safely.

    Raised rather than emitting a config with verification disabled or a credential
    inlined, so a misconfiguration is a failed export instead of an unsafe one.
    """


def generate_kubeconfig(
    cluster_endpoint: str,
    cluster_ca_cert: str,
    cluster_name: str,
    workspace_aws_account_id: str,
    workspace_name: str,
    region: str | None = None,
    external_id: str | None = None,
) -> tuple[str, datetime]:
    """Generate a scoped kubeconfig that fetches its own token via ``aws eks get-token``.

    Args:
        cluster_endpoint: EKS cluster API server endpoint.
        cluster_ca_cert: Base64-encoded CA certificate. Required — a config without it
            would verify against the system trust store rather than the cluster.
        cluster_name: EKS cluster name, passed to the exec plugin so the token it
            fetches is bound to this cluster.
        workspace_aws_account_id: Account the workspace cluster lives in.
        workspace_name: Workspace name, used for the role name and context naming.
        region: Region of the cluster. Defaults to the API's configured region.
        external_id: The tenant's stored ExternalId, if their role's trust policy
            requires one. Read from the stored cloud-account record by the caller, never
            derived in code (U16a, #5051). Not a secret: it is an anti-confused-deputy
            condition value the tenant themselves placed in their trust policy, and the
            holder of this file must already be able to assume the role for it to matter.
            When present, the exported config delegates role assumption to a local AWS
            profile, because that is the only way the CLI will send an ExternalId.

    Returns:
        Tuple of (kubeconfig_yaml_string, expiry_datetime).

    Raises:
        KubeconfigError: If the cluster CA is missing or malformed, or required
            identifiers are absent.
    """
    if not cluster_endpoint:
        raise KubeconfigError("Cluster endpoint is required to generate a kubeconfig")
    if not cluster_name:
        raise KubeconfigError("Cluster name is required to generate a kubeconfig")

    # Mandatory verification: validate the CA before emitting anything, reusing the
    # proxy path's definition of a usable CA so the two halves cannot diverge.
    try:
        validate_ca_data(cluster_ca_cert)
    except EksAuthError as exc:
        raise KubeconfigError(str(exc)) from exc

    cluster_region = region or settings.aws_region
    role_arn = f"arn:aws:iam::{workspace_aws_account_id}:role/superplane-workspace-{workspace_name}"
    user_name = f"superplane-{workspace_name}"

    # `aws eks get-token` performs the assume itself and returns a signed, cluster-bound
    # token, so the file describes how to obtain access rather than containing access.
    exec_args = [
        "--region",
        cluster_region,
        "eks",
        "get-token",
        "--cluster-name",
        cluster_name,
    ]

    if external_id:
        # An ExternalId can only reach STS through a config-file profile. `aws eks
        # get-token` has no `--external-id` flag and the CLI has no environment variable
        # for it — verified against aws-cli 2.36.48, where the AssumeRole body sent with
        # `AWS_EXTERNAL_ID` set carries only RoleArn and RoleSessionName.
        #
        # `--role-arn` must be omitted here, not combined with the profile. With both, the
        # CLI assumes the role directly for token minting and that call carries no
        # ExternalId — so the tenant's condition is never satisfied. Delegating entirely to
        # the profile is what makes the assumption carry it.
        #
        # The holder configures the profile locally with `role_arn` and `external_id`. The
        # exported file still grants nothing by itself: the profile only names the role,
        # and the holder must be authorized to assume it.
        exec_args += ["--profile", user_name]
        logger.info(
            "Kubeconfig for workspace '%s' delegates role assumption to local AWS profile "
            "'%s', which must set role_arn=%s and the tenant's external_id. Brokered API "
            "access needs no such setup.",
            workspace_name,
            user_name,
            role_arn,
        )
    else:
        # No ExternalId condition to satisfy, so the plugin can assume the role directly
        # and the export needs no local profile setup.
        exec_args += ["--role-arn", role_arn]

    exec_config: dict[str, object] = {
        "apiVersion": EXEC_API_VERSION,
        "command": "aws",
        "args": exec_args,
        # The plugin returns the token in its stdout JSON; it needs no interactive input.
        "interactiveMode": "Never",
        "provideClusterInfo": False,
    }

    kubeconfig = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "cluster": {
                    "server": cluster_endpoint,
                    "certificate-authority-data": cluster_ca_cert,
                },
                "name": cluster_name,
            }
        ],
        "contexts": [
            {
                "context": {
                    "cluster": cluster_name,
                    "user": user_name,
                },
                "name": user_name,
            }
        ],
        "current-context": user_name,
        "users": [
            {
                "name": user_name,
                "user": {
                    # No `token` key: nothing here is a credential.
                    "exec": exec_config,
                },
            }
        ],
    }

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=TOKEN_DURATION_SECONDS)
    return yaml.dump(kubeconfig, default_flow_style=False), expires_at
