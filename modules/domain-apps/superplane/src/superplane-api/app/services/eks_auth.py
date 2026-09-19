"""EKS authentication primitives — signed cluster tokens and mandatory CA verification.

Two things in this module exist because getting either wrong is a security defect,
not a bug:

**A Kubernetes bearer token is not an STS SessionToken.** ``sts:AssumeRole`` returns
an AccessKeyId/SecretAccessKey/SessionToken triple. The SessionToken is an opaque
AWS-internal value; the Kubernetes API server cannot verify it and EKS does not accept
it. What EKS accepts is a *presigned* ``GetCallerIdentity`` request encoded as
``k8s-aws-v1.<base64url>`` — the same shape ``aws eks get-token`` and
aws-iam-authenticator produce. :func:`build_cluster_token` builds that, and it is the
only supported way to authenticate to a cluster from this codebase.

**TLS verification is not optional.** The cluster's CA certificate is what distinguishes
the real API server from anything else answering at that address. A missing CA is a
configuration failure to be reported, never a reason to continue unverified: an
unverified connection still succeeds, so the downgrade is silent while the control plane
hands brokered credentials to an unauthenticated endpoint. :func:`write_ca_bundle`
therefore raises on absent or malformed CA data and there is deliberately no parameter,
flag or fallback in this module that disables verification.
"""

import base64
import binascii
import hashlib
import logging
import os
import tempfile
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

# Token prefix defined by the EKS/aws-iam-authenticator token format.
CLUSTER_TOKEN_PREFIX = "k8s-aws-v1."

# Header that binds a presigned token to one named cluster. It is part of the signature's
# SignedHeaders, so a token minted for cluster A cannot be replayed against cluster B —
# the signature covers the cluster name.
CLUSTER_NAME_HEADER = "x-k8s-aws-id"

# Presign validity in seconds. 60 matches `aws eks get-token` and aws-iam-authenticator:
# the URL must be signed recently, while the resulting token remains usable by the API
# server for its own (~15 minute) window.
PRESIGN_EXPIRY_SECONDS = 60


class EksAuthError(Exception):
    """Raised when cluster authentication or CA verification cannot be set up safely.

    Deliberately raised — rather than degrading to an unverified or unauthenticated
    connection — so that a misconfiguration surfaces as a failed operation instead of
    an insecure successful one.
    """


def build_cluster_token(
    cluster_name: str,
    credentials: Mapping[str, str],
    region: str,
) -> str:
    """Build a signed EKS bearer token for ``cluster_name``.

    Presigns an STS ``GetCallerIdentity`` request with ``credentials`` and the
    ``x-k8s-aws-id`` header set to the cluster name, then encodes the URL as
    ``k8s-aws-v1.<base64url>``. The API server resolves the token by executing the
    presigned request, which is why the caller's identity — not a copied secret — is
    what authenticates.

    Args:
        cluster_name: EKS cluster name. Signed into the token, binding it to this cluster.
        credentials: STS credentials with AccessKeyId, SecretAccessKey and SessionToken,
            as returned by a brokered ``AssumeRole``.
        region: Region of the STS endpoint to sign against.

    Returns:
        The ``k8s-aws-v1.``-prefixed token, safe to send as an ``Authorization: Bearer``
        value.

    Raises:
        EksAuthError: If the cluster name is empty or the credential triple is
            incomplete. An incomplete triple cannot produce a valid signature, and
            signing with a partial credential would yield a token the cluster silently
            rejects.
    """
    if not cluster_name:
        raise EksAuthError("Cluster name is required to build an EKS token")

    missing = [
        key
        for key in ("AccessKeyId", "SecretAccessKey", "SessionToken")
        if not credentials.get(key)
    ]
    if missing:
        raise EksAuthError(
            f"Incomplete STS credentials for EKS token: missing {', '.join(missing)}"
        )

    session = boto3.Session(
        aws_access_key_id=credentials["AccessKeyId"],
        aws_secret_access_key=credentials["SecretAccessKey"],
        aws_session_token=credentials["SessionToken"],
        region_name=region,
    )
    # ``region_name`` alone does not guarantee a regional STS endpoint: botocore
    # can still select ``sts.amazonaws.com`` when the ambient
    # ``sts_regional_endpoints`` setting is legacy.  EKS tokens must be signed
    # for the requested cluster region independently of host configuration.
    sts_client = session.client(
        "sts",
        region_name=region,
        endpoint_url=f"https://sts.{region}.amazonaws.com",
    )

    # Inject the cluster-binding header before signing so it is covered by the
    # signature. Registered per-client, so it cannot leak into unrelated STS calls.
    def _add_cluster_header(request: Any, **_kwargs: Any) -> None:
        request.headers[CLUSTER_NAME_HEADER] = cluster_name

    sts_client.meta.events.register(
        "before-sign.sts.GetCallerIdentity", _add_cluster_header
    )

    try:
        presigned_url = sts_client.generate_presigned_url(
            "get_caller_identity",
            Params={},
            ExpiresIn=PRESIGN_EXPIRY_SECONDS,
            HttpMethod="GET",
        )
    except (ClientError, BotoCoreError) as exc:
        # Do not log the URL: it is a bearer credential until it expires.
        logger.error("Failed to presign EKS token request: %s", type(exc).__name__)
        raise EksAuthError("Failed to build EKS authentication token") from exc

    # base64url without padding, per the token format.
    encoded = base64.urlsafe_b64encode(presigned_url.encode("utf-8")).decode("utf-8")
    return CLUSTER_TOKEN_PREFIX + encoded.rstrip("=")


def decode_cluster_token(token: str) -> str:
    """Recover the presigned URL from a token built by :func:`build_cluster_token`.

    Exists so tests can assert what was actually signed — the cluster binding and the
    STS action — rather than only that a string was produced.

    Raises:
        EksAuthError: If the token is not in ``k8s-aws-v1.<base64url>`` form.
    """
    if not token.startswith(CLUSTER_TOKEN_PREFIX):
        raise EksAuthError(f"Token is not in {CLUSTER_TOKEN_PREFIX} form")

    payload = token[len(CLUSTER_TOKEN_PREFIX) :]
    # Restore the padding stripped when the token was built.
    padded = payload + "=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise EksAuthError("Token payload is not valid base64url") from exc


def validate_ca_data(cluster_ca_data: str) -> bytes:
    """Validate base64 cluster CA data and return the decoded PEM bytes.

    Single definition of "usable CA", shared by the brokered proxy path and the
    kubeconfig export so the two halves cannot diverge on what they accept.

    Raises:
        EksAuthError: If the CA data is absent or not decodable PEM. This is the check
            that makes verification mandatory — callers have no unverified path to fall
            back to, so a cluster whose CA cannot be resolved is unreachable rather than
            reachable insecurely.
    """
    if not cluster_ca_data or not cluster_ca_data.strip():
        raise EksAuthError(
            "Cluster CA certificate is unavailable; refusing to connect without "
            "TLS verification"
        )

    try:
        ca_bytes = base64.b64decode(cluster_ca_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EksAuthError("Cluster CA certificate is not valid base64") from exc

    if b"-----BEGIN CERTIFICATE-----" not in ca_bytes:
        # Catches truncated or double-encoded values, which would otherwise reach the
        # TLS stack as an empty trust store and fail with an unrelated-looking error.
        raise EksAuthError("Cluster CA certificate does not contain a PEM certificate")

    return ca_bytes


def write_ca_bundle(cluster_ca_data: str) -> str:
    """Decode base64 cluster CA data to a PEM file for TLS verification.

    The file cannot be deleted when this returns: the Kubernetes client reads
    ``ssl_ca_cert`` from disk on every request, so the path must stay valid for as long
    as any client built from it lives. Because a bundle is written per brokered request,
    creating a fresh temp file each time would grow ``/tmp`` without bound.

    So the path is derived from a hash of the CA content and reused. Identical CA data
    maps to one file, which makes the number of files a function of how many distinct
    cluster CAs this process has talked to — not of how much traffic it has served. The
    content is a public certificate, not a secret, and the hash means a rotated CA gets
    its own path instead of silently reusing a stale one.

    Args:
        cluster_ca_data: Base64-encoded PEM CA bundle, as EKS ``DescribeCluster``
            returns in ``certificateAuthority.data``.

    Returns:
        Path to a readable PEM file to pass as the client's trust anchor.

    Raises:
        EksAuthError: If the CA data is absent or not decodable PEM.
    """
    ca_bytes = validate_ca_data(cluster_ca_data)

    digest = hashlib.sha256(ca_bytes).hexdigest()[:32]
    path = os.path.join(tempfile.gettempdir(), f"eks-ca-{digest}.crt")

    # Reuse an existing bundle with identical content. Verifying the bytes rather than
    # trusting the filename means a truncated or tampered leftover is rewritten instead
    # of being used as a trust anchor.
    try:
        with open(path, "rb") as handle:
            if handle.read() == ca_bytes:
                return path
    except OSError:
        pass

    # Write to a unique temp file, then atomically move it into place, so a concurrent
    # request never observes a partially written trust anchor.
    fd, staging = tempfile.mkstemp(suffix=".crt", prefix=f"eks-ca-{digest}-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(ca_bytes)
        os.replace(staging, path)
    except OSError as exc:
        if os.path.exists(staging):
            os.unlink(staging)
        raise EksAuthError("Failed to write cluster CA bundle") from exc

    return path


def describe_cluster_ca(
    cluster_name: str,
    credentials: Mapping[str, str],
    region: str,
) -> str:
    """Fetch a cluster's CA data from EKS ``DescribeCluster``.

    EKS is the authoritative source for a cluster's CA, so it is read through the same
    brokered credentials used for the workload call rather than cached in the control
    plane's database, where it could go stale against a rotated cluster.

    Returns:
        Base64-encoded CA data suitable for :func:`write_ca_bundle`.

    Raises:
        EksAuthError: If the cluster cannot be described or reports no CA.
    """
    session = boto3.Session(
        aws_access_key_id=credentials.get("AccessKeyId"),
        aws_secret_access_key=credentials.get("SecretAccessKey"),
        aws_session_token=credentials.get("SessionToken"),
        region_name=region,
    )
    eks_client = session.client("eks", region_name=region)

    try:
        response = eks_client.describe_cluster(name=cluster_name)
    except (ClientError, BotoCoreError) as exc:
        logger.error(
            "DescribeCluster failed for %s: %s", cluster_name, type(exc).__name__
        )
        raise EksAuthError(
            f"Failed to resolve CA certificate for cluster '{cluster_name}'"
        ) from exc

    ca_data = (
        response.get("cluster", {}).get("certificateAuthority", {}).get("data") or ""
    )
    if not ca_data:
        raise EksAuthError(
            f"Cluster '{cluster_name}' reported no CA certificate; refusing to "
            "connect without TLS verification"
        )
    return ca_data
