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
        identity = [
            str(UUID(str(value)))
            for value in (org_id, workspace_id, cluster_id, request_id)
        ]
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
