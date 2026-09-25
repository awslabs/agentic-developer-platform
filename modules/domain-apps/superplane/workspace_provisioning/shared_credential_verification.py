"""Verify actual projected member credentials against their pinned API server."""

import base64
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile

from superplane_bootstrap.errors import BootstrapRefused

from .member_credentials.binding import IssuedCredential
from .member_credentials.issuer import delegation_specs


def verify_projected_credential(
    projector, binding, receipt, *, target, directory, verify, expect_closed=False
):
    """Reusable by bootstrap and renewal; never infer identity from JWT claims.

    The caller fences the whole invocation against its operation/controller lease.
    Returned metadata contains no token. The exact delivered bytes are verified
    through SelfSubjectReview, a namespaced read and denial of namespace mutation.
    Mutators must also hold every compiled namespace write permission. This
    positive authorization proof runs with both closed bootstrap and open renewal
    gates; only bootstrap additionally requires admission to reject its dry run.
    """
    from kubernetes import client
    from kubernetes.client.exceptions import ApiException

    verify()
    projector._validate_receipt(binding, receipt)
    _, secret = projector._read(binding, "verify")
    key, annotation = projector._names(binding)
    if not projector._owned(secret, key, annotation, receipt):
        raise BootstrapRefused("projected credential differs from its durable receipt")
    metadata = json.loads(receipt.metadata_json)
    try:
        raw = base64.b64decode(secret["data"][key], validate=True)
        document = json.loads(raw)
        token = document["users"][0]["user"]["token"]
        credential = IssuedCredential(
            binding,
            metadata["service_account_uid"],
            datetime.fromisoformat(metadata["expires_at"]),
            token,
            target.certificate_authority_data,
        )
        if (
            credential.expires_at <= datetime.now(UTC)
            or credential.metadata != metadata
            or document
            != json.loads(credential.kubeconfig(target.certificate_authority_data))
            or (target.org_id, target.cluster_arn, target.endpoint)
            != (
                binding.membership.org_id,
                binding.membership.cluster_arn,
                binding.membership.endpoint,
            )
            or not isinstance(token, str)
            or not token
        ):
            raise ValueError()
        certificate = base64.b64decode(target.certificate_authority_data, validate=True)
    except Exception:
        raise BootstrapRefused("projected credential document is invalid") from None

    # No ambient kubeconfig, exec authentication, proxy or unverified CA fallback.
    with tempfile.NamedTemporaryFile(
        dir=Path(directory), suffix=".member-ca.pem"
    ) as ca:
        ca.write(certificate)
        ca.flush()
        configuration = client.Configuration()
        configuration.host = target.endpoint
        configuration.ssl_ca_cert = ca.name
        configuration.verify_ssl = True
        configuration.proxy = None
        configuration.api_key_prefix["authorization"] = "Bearer"
        configuration.api_key["authorization"] = token
        api = client.ApiClient(configuration)

        def call(path, method, *, body=None, query=None):
            verify()
            try:
                result = api.call_api(
                    path,
                    method,
                    body=body,
                    query_params=query or [],
                    response_type="object",
                    auth_settings=["BearerToken"],
                    header_params={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                    _request_timeout=(5, 15),
                    _return_http_data_only=True,
                )
            except ApiException as exc:
                if (
                    expect_closed
                    and method == "POST"
                    and path.endswith("/pods")
                    and exc.status == 403
                ):
                    try:
                        message = json.loads(exc.body)["message"]
                    except (ValueError, TypeError, KeyError):
                        message = ""
                    if "workspace admission is closed" in message:
                        verify()
                        return {"closed_gate_denied": True}
                raise BootstrapRefused(
                    "projected credential provider verification refused"
                ) from None
            except Exception:
                raise BootstrapRefused(
                    "projected credential provider verification unavailable"
                ) from None
            verify()
            return result

        try:
            identity = (
                call(
                    "/apis/authentication.k8s.io/v1/selfsubjectreviews",
                    "POST",
                    body={
                        "apiVersion": "authentication.k8s.io/v1",
                        "kind": "SelfSubjectReview",
                    },
                )
                .get("status", {})
                .get("userInfo", {})
            )
            username = f"system:serviceaccount:{binding.membership.namespace}:{binding.service_account}"
            if (identity.get("uid"), identity.get("username")) != (
                metadata["service_account_uid"],
                username,
            ):
                raise BootstrapRefused(
                    "projected credential ServiceAccount identity changed"
                )
            namespace = binding.membership.namespace
            pods = call(
                f"/api/v1/namespaces/{namespace}/pods", "GET", query=[("limit", "1")]
            )
            if not isinstance(pods, dict) or pods.get("kind") != "PodList":
                raise BootstrapRefused(
                    "projected credential namespace read was not observed"
                )
            for verb in ("patch", "delete", "update"):
                review = call(
                    "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                    "POST",
                    body={
                        "apiVersion": "authorization.k8s.io/v1",
                        "kind": "SelfSubjectAccessReview",
                        "spec": {
                            "resourceAttributes": {
                                "group": "",
                                "resource": "namespaces",
                                "name": namespace,
                                "verb": verb,
                            }
                        },
                    },
                ).get("status", {})
                if review.get("allowed") is not False or review.get("evaluationError"):
                    raise BootstrapRefused(
                        "member credential can alter namespace admission labels"
                    )
            if binding.scope == "mutator":
                # A Pod list proves reader access only. Verify the actual write
                # permissions before acknowledging a replacement mutator token.
                # SSAR tests authorization even while bootstrap admission is
                # closed, without persisting a workload or changing any peer.
                role = delegation_specs(binding)[1]["body"]
                actions = {
                    (group, resource, verb)
                    for rule in role["rules"]
                    for group in rule["apiGroups"]
                    for resource in rule["resources"]
                    for verb in rule["verbs"]
                    if verb not in {"get", "list", "watch"}
                }
                if not actions:
                    raise BootstrapRefused(
                        "member mutator has no compiled write permissions"
                    )
                for group, resource, verb in sorted(actions):
                    review = call(
                        "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                        "POST",
                        body={
                            "apiVersion": "authorization.k8s.io/v1",
                            "kind": "SelfSubjectAccessReview",
                            "spec": {
                                "resourceAttributes": {
                                    "group": group,
                                    "resource": resource,
                                    "namespace": namespace,
                                    "verb": verb,
                                }
                            },
                        },
                    ).get("status", {})
                    if (
                        not isinstance(review, dict)
                        or review.get("allowed") is not True
                        or review.get("evaluationError")
                    ):
                        raise BootstrapRefused(
                            "member mutator namespace write permission was not observed"
                        )
            if expect_closed and binding.scope == "mutator":
                result = call(
                    f"/api/v1/namespaces/{namespace}/pods",
                    "POST",
                    query=[("dryRun", "All")],
                    body={
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "metadata": {
                            "generateName": "member-gate-proof-",
                            "namespace": namespace,
                        },
                        "spec": {
                            "restartPolicy": "Never",
                            "automountServiceAccountToken": False,
                            "securityContext": {
                                "runAsNonRoot": True,
                                "runAsUser": 65532,
                                "seccompProfile": {"type": "RuntimeDefault"},
                            },
                            "containers": [
                                {
                                    "name": "probe",
                                    "image": "registry.k8s.io/pause:3.10",
                                    "securityContext": {
                                        "allowPrivilegeEscalation": False,
                                        "capabilities": {"drop": ["ALL"]},
                                    },
                                }
                            ],
                        },
                    },
                )
                if result != {"closed_gate_denied": True}:
                    raise BootstrapRefused(
                        "member credential bypassed the closed namespace gate"
                    )
        finally:
            api.close()
    verify()
    return metadata
