"""Explicit pinned Kubernetes issuance, delegated RBAC and UID-fenced revocation.

The trusted composer must journal grant intents before applying delegation_specs.
Authorization callbacks must verify current membership AND a live operation or
installed cluster-owned renewal authority; this class never retains run authority.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import GENERATION_ANNOTATION, KubeGrants

from .binding import CredentialBinding, IssuedCredential, canonical, uid

BINDING_ANNOTATION = "superplane.aws-e/member-credential"


def delegation_specs(binding: CredentialBinding):
    member = binding.membership
    name = binding.service_account
    marker = canonical(
        {
            "membership": member.encode(),
            "namespace_uid": binding.namespace_uid,
            "revision": binding.revision,
            "scope": binding.scope,
        }
    )
    metadata = {
        "name": name,
        "namespace": member.namespace,
        "annotations": {
            GENERATION_ANNOTATION: member.generation,
            BINDING_ANNOTATION: marker,
        },
    }
    read = ["get", "list", "watch"]
    verbs = read if binding.scope == "reader" else [*read, "create", "patch", "delete"]
    bodies = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": metadata,
            "automountServiceAccountToken": False,
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": metadata,
            "rules": [
                {"apiGroups": [""], "resources": ["pods"], "verbs": verbs},
                {
                    "apiGroups": [""],
                    "resources": ["events", "resourcequotas"],
                    "verbs": read,
                },
                {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": verbs},
                {
                    "apiGroups": ["superplane.ai"],
                    "resources": ["superplanenodes"],
                    "verbs": verbs,
                },
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": metadata,
            "roleRef": {
                "apiGroup": "rbac.authorization.k8s.io",
                "kind": "Role",
                "name": name,
            },
            "subjects": [
                {"kind": "ServiceAccount", "name": name, "namespace": member.namespace}
            ],
        },
    ]
    return tuple(
        {
            "key": f"{name}-{body['kind']}",
            "kind": "kubernetes",
            "actor": "member-credential-issuer",
            "cluster_arn": member.cluster_arn,
            "generation": member.generation,
            "lifetime": "workspace",
            "body": body,
        }
        for body in bodies
    )


class MemberIssuer:
    def __init__(self, grants, authorize, *, audience, now=lambda: datetime.now(UTC)):
        if not isinstance(grants, KubeGrants) or not callable(authorize):
            raise BootstrapRefused(
                "issuer requires verified Kubernetes transport and authority"
            )
        if not isinstance(audience, str) or not audience or len(audience) > 256:
            raise BootstrapRefused(
                "issuer requires the installed Kubernetes API audience"
            )
        self.grants, self.authorize, self.audience, self.now = (
            grants,
            authorize,
            audience,
            now,
        )

    def _verify(self, binding, action):
        target, member = self.grants.target, binding.membership
        if (target.org_id, target.cluster_arn, target.endpoint) != (
            member.org_id,
            member.cluster_arn,
            member.endpoint,
        ):
            raise BootstrapRefused("credential membership targets another cluster")
        self.authorize(binding, action)
        self.grants._verify_transport()

    def _namespace(self, binding, action):
        self._verify(binding, action)
        resource = self.grants.client.resources.get(api_version="v1", kind="Namespace")
        body = resource.get(name=binding.membership.namespace)
        body = body.to_dict() if hasattr(body, "to_dict") else body
        if (
            body.get("metadata", {}).get("uid") != binding.namespace_uid
            or body.get("metadata", {}).get("deletionTimestamp")
            or body.get("status", {}).get("phase") != "Active"
        ):
            raise BootstrapRefused("credential namespace changed or is terminating")
        self._verify(binding, action)

    def _service_account(self, binding, expected_uid, action):
        self._namespace(binding, action)
        spec = delegation_specs(binding)[0]
        self._verify(binding, action)
        observed = self.grants._get(spec)
        if observed is None:
            raise BootstrapRefused("credential service account is absent")
        identity = self.grants._identity(spec, observed)
        self.grants.verify(spec, identity)
        if (
            identity["uid"] != uid(expected_uid)
            or observed["metadata"].get("deletionTimestamp")
            or observed["metadata"].get("annotations")
            != spec["body"]["metadata"]["annotations"]
        ):
            raise BootstrapRefused("credential service account binding changed")
        self._verify(binding, action)
        return spec, observed

    def issue(self, binding, *, service_account_uid, lifetime_seconds=900):
        if type(lifetime_seconds) is not int or not 600 <= lifetime_seconds <= 3600:
            raise BootstrapRefused("member token lifetime must be 600 to 3600 seconds")
        self._service_account(binding, service_account_uid, "issue")
        # Verify exact compiled permissions too. No reader/mutator may mint tokens
        # or access fleet-wide NodePools, RBAC, Secrets or other namespaces.
        for spec in delegation_specs(binding)[1:]:
            self._verify(binding, "issue")
            observed = self.grants.observe(spec)
            if observed is None:
                raise BootstrapRefused("member delegation is incomplete")
            self.grants.verify(spec, observed)
        self._verify(binding, "issue")
        path = "/api/v1/namespaces/{}/serviceaccounts/{}/token".format(
            quote(binding.membership.namespace, safe=""),
            quote(binding.service_account, safe=""),
        )
        started = self.now()
        try:
            result = self.grants.client.client.call_api(
                path,
                "POST",
                body={
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenRequest",
                    "spec": {
                        "audiences": [self.audience],
                        "expirationSeconds": lifetime_seconds,
                    },
                },
                response_type="object",
                auth_settings=["BearerToken"],
                header_params={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                _return_http_data_only=True,
            )
            token = result["status"]["token"]
            expiry = datetime.fromisoformat(
                result["status"]["expirationTimestamp"].replace("Z", "+00:00")
            )
            if (
                not isinstance(token, str)
                or not 1 <= len(token) <= 65536
                or any(c.isspace() for c in token)
                or expiry.tzinfo is None
                or expiry <= self.now() + timedelta(seconds=60)
                or expiry > started + timedelta(seconds=lifetime_seconds)
            ):
                raise ValueError()
        except Exception:
            # Provider exceptions may embed token-bearing response bodies.
            raise BootstrapRefused("Kubernetes member token issuance refused") from None
        self._service_account(binding, service_account_uid, "issue")
        return IssuedCredential(
            binding,
            service_account_uid,
            expiry,
            token,
            self.grants.target.certificate_authority_data,
        )

    def revoke(self, binding, *, service_account_uid):
        # An absent generation-specific SA is already revoked. Still require
        # current cleanup authority and the original namespace incarnation.
        uid(service_account_uid)
        self._namespace(binding, "revoke")
        self._verify(binding, "revoke")
        if self.grants._get(delegation_specs(binding)[0]) is None:
            self._verify(binding, "revoke")
            return
        spec, observed = self._service_account(binding, service_account_uid, "revoke")
        version = observed["metadata"].get("resourceVersion")
        if not version:
            raise BootstrapRefused("credential service account version is absent")
        self._verify(binding, "revoke")
        self.grants._resource(spec).delete(
            **self.grants._args(spec),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {
                    "uid": service_account_uid,
                    "resourceVersion": version,
                },
            },
        )
        self._verify(binding, "revoke")
