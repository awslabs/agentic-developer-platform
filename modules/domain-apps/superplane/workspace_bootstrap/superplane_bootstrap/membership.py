"""Immutable shared placement bytes used by preview, reservation and bootstrap."""

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import UUID

from .errors import BootstrapRefused


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class SharedMembership:
    version: int
    org_id: str
    workspace_id: str
    cluster_id: str
    cluster_arn: str
    endpoint: str
    namespace: str
    generation: str
    request_id: str

    @classmethod
    def create(
        cls, *, org_id, workspace_id, cluster_id, cluster_arn, endpoint, request_id
    ):
        try:
            identity = [
                str(UUID(str(value)))
                for value in (org_id, workspace_id, cluster_id, request_id)
            ]
        except (TypeError, ValueError, AttributeError):
            raise BootstrapRefused(
                "shared membership identifiers must be UUIDs"
            ) from None
        org_id, workspace_id, cluster_id, request_id = identity
        generation = hashlib.sha256(
            ("superplane-membership:v1:" + canonical(identity)).encode()
        ).hexdigest()
        return cls.read(
            canonical(
                {
                    "version": 1,
                    "org_id": org_id,
                    "workspace_id": workspace_id,
                    "cluster_id": cluster_id,
                    "cluster_arn": cluster_arn,
                    "endpoint": endpoint,
                    "namespace": "sp-ws-" + UUID(workspace_id).hex,
                    "generation": generation,
                    "request_id": request_id,
                }
            )
        )

    @classmethod
    def read(cls, raw):
        try:
            if not isinstance(raw, str) or len(raw) > 2000:
                raise ValueError()
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != set(
                cls.__dataclass_fields__
            ):
                raise ValueError()
            if type(value["version"]) is not int or value["version"] != 1:
                raise ValueError()
            for name in ("org_id", "workspace_id", "cluster_id", "request_id"):
                if str(UUID(value[name])) != value[name]:
                    raise ValueError()
            identity = [
                value[name]
                for name in ("org_id", "workspace_id", "cluster_id", "request_id")
            ]
            expected = hashlib.sha256(
                ("superplane-membership:v1:" + canonical(identity)).encode()
            ).hexdigest()
            if (
                value["generation"] != expected
                or value["namespace"] != "sp-ws-" + UUID(value["workspace_id"]).hex
            ):
                raise ValueError()
            if not re.fullmatch(
                r"arn:aws:eks:[a-z0-9-]+:[0-9]{12}:cluster/[A-Za-z0-9][A-Za-z0-9_-]{0,99}",
                value["cluster_arn"],
            ):
                raise ValueError()
            endpoint = urlsplit(value["endpoint"])
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
                or endpoint.path not in {"", "/"}
            ):
                raise ValueError()
            return cls(**value)
        except (AttributeError, TypeError, ValueError):
            raise BootstrapRefused("shared membership document is invalid") from None

    def encode(self):
        return canonical(asdict(self))


REGISTRATION_FIELDS = (
    "membership_cluster_id",
    "membership_request_id",
    "membership_generation",
)


def registration_membership(identity):
    """Recover the approved reservation identity, preserving legacy records."""
    values = [identity.get(name) or "" for name in REGISTRATION_FIELDS]
    if not any(values):
        return None
    if not all(values) or identity.get("cluster_placement") != "shared":
        raise BootstrapRefused("registration has incomplete shared membership identity")
    binding = SharedMembership.create(
        org_id=identity["org_id"],
        workspace_id=identity["workspace_id"],
        cluster_id=values[0],
        request_id=values[1],
        cluster_arn=identity["cluster_arn"],
        endpoint=identity["endpoint"],
    )
    if (binding.cluster_id, binding.request_id, binding.generation) != tuple(
        values
    ) or binding.namespace != identity["namespace"]:
        raise BootstrapRefused("registration differs from approved shared membership")
    return binding


def registration_fields(binding):
    if binding is None:
        return {}
    return {
        "cluster_placement": "shared",
        "membership_cluster_id": binding.cluster_id,
        "membership_request_id": binding.request_id,
        "membership_generation": binding.generation,
    }
