"""Immutable public credential identity and deliberately redacted token material."""

import base64
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import re

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.membership import SharedMembership

EXTENSION = "superplane.aws-e/membership"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def uid(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value):
        raise BootstrapRefused("credential immutable UID is invalid")
    return value


@dataclass(frozen=True)
class CredentialBinding:
    membership: SharedMembership
    namespace_uid: str
    revision: int
    scope: str

    def __post_init__(self):
        if not isinstance(self.membership, SharedMembership):
            raise BootstrapRefused("credential requires a shared membership")
        SharedMembership.read(self.membership.encode())
        uid(self.namespace_uid)
        if (
            type(self.revision) is not int
            or not 1 <= self.revision <= 2**31 - 1
            or self.scope not in {"reader", "mutator"}
        ):
            raise BootstrapRefused("credential revision or scope is invalid")

    @property
    def service_account(self):
        return f"sp-{self.scope}-{self.membership.generation[:24]}-{self.revision}"

    def metadata(self, service_account_uid, expires_at):
        uid(service_account_uid)
        if expires_at.tzinfo is None:
            raise BootstrapRefused("credential expiry must include a timezone")
        member = self.membership
        return {
            "org_id": member.org_id,
            "workspace_id": member.workspace_id,
            "cluster_id": member.cluster_id,
            "cluster_arn": member.cluster_arn,
            "generation": member.generation,
            "namespace": member.namespace,
            "namespace_uid": self.namespace_uid,
            "service_account_uid": service_account_uid,
            "revision": self.revision,
            "expires_at": expires_at.astimezone(UTC).isoformat(),
            "scope": self.scope,
        }


@dataclass(frozen=True, repr=False)
class IssuedCredential:
    binding: CredentialBinding
    service_account_uid: str
    expires_at: datetime
    _token: str = field(repr=False)
    certificate_authority_data: str

    def __repr__(self):
        return "IssuedCredential(<redacted>)"

    @property
    def metadata(self):
        return self.binding.metadata(self.service_account_uid, self.expires_at)

    def kubeconfig(self, certificate_authority_data):
        """Secret egress only: the caller must not log or persist this in journals."""
        member = self.binding.membership
        if (
            certificate_authority_data != self.certificate_authority_data
            or not base64.b64decode(certificate_authority_data, validate=True)
        ):
            raise BootstrapRefused("credential requires pinned cluster CA")
        return canonical(
            {
                "apiVersion": "v1",
                "kind": "Config",
                "current-context": member.cluster_arn,
                "clusters": [
                    {
                        "name": "target",
                        "cluster": {
                            "server": member.endpoint,
                            "certificate-authority-data": certificate_authority_data,
                        },
                    }
                ],
                "contexts": [
                    {
                        "name": member.cluster_arn,
                        "context": {
                            "cluster": "target",
                            "user": "member",
                            "namespace": member.namespace,
                        },
                    }
                ],
                "users": [{"name": "member", "user": {"token": self._token}}],
                "extensions": [{"name": EXTENSION, "extension": self.metadata}],
            }
        )
