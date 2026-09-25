"""Immutable installed authority and fenced reconciliation, never bootstrap leases."""

from dataclasses import dataclass
import json
import re
from uuid import UUID

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.namespace_admission import ClusterAuthorityReference
from superplane_bootstrap.target import VerifiedTarget


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise BootstrapRefused("installed credential authority fields differ")


@dataclass(frozen=True)
class Authority:
    authority_id: str
    document_json: str

    @property
    def document(self):
        return json.loads(self.document_json)

    @classmethod
    def read(cls, authority_id, raw):
        from base64 import b64decode
        from urllib.parse import urlsplit

        try:
            str(UUID(authority_id))
            doc = json.loads(raw)
            fields(
                doc,
                "version org_id cluster_id controller_role_arn controller_role_id target issuer management_target projector projection audience"
                + (" shared_dependencies" if doc.get("version") == 2 else ""),
            )
            if doc["version"] not in {1, 2} or type(doc["version"]) is not int:
                raise ValueError()
            for key in ("org_id", "cluster_id"):
                if str(UUID(doc[key])) != doc[key]:
                    raise ValueError()
            fields(
                doc["issuer"],
                "role_arn role_id access_entry_arn username group policy_uid binding_uid",
            )
            fields(doc["projector"], "role_arn role_id access_entry_arn username group")
            for target_key, actor_key in (
                ("target", "issuer"),
                ("management_target", "projector"),
            ):
                target, actor = doc[target_key], doc[actor_key]
                fields(
                    target,
                    "account_id region cluster_name cluster_arn endpoint certificate_authority_data",
                )
                if not re.fullmatch(
                    r"[0-9]{12}", target["account_id"]
                ) or not re.fullmatch(r"[a-z0-9-]+", target["region"]):
                    raise ValueError()
                expected = f"arn:aws:eks:{target['region']}:{target['account_id']}:cluster/{target['cluster_name']}"
                endpoint = urlsplit(target["endpoint"])
                if (
                    target["cluster_arn"] != expected
                    or endpoint.scheme != "https"
                    or not endpoint.hostname
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                    or endpoint.path not in {"", "/"}
                    or not b64decode(
                        target["certificate_authority_data"], validate=True
                    )
                ):
                    raise ValueError()
                if (
                    not actor["role_arn"].startswith(
                        f"arn:aws:iam::{target['account_id']}:role/"
                    )
                    or not actor["access_entry_arn"].startswith(
                        f"arn:aws:eks:{target['region']}:{target['account_id']}:access-entry/{target['cluster_name']}/"
                    )
                    or not actor["group"].startswith("superplane:")
                    or not actor["role_id"]
                    or not actor["username"]
                ):
                    raise ValueError()
            if (
                not doc["controller_role_arn"].startswith(
                    f"arn:aws:iam::{doc['management_target']['account_id']}:role/"
                )
                or not doc["controller_role_id"]
                or not doc["audience"]
            ):
                raise ValueError()
            fields(
                doc["projection"],
                "namespace namespace_uid reader_secret reader_secret_uid mutator_secret mutator_secret_uid",
            )
            projection = doc["projection"]
            if projection["reader_secret"] == projection["mutator_secret"]:
                raise ValueError()
            for key, value in projection.items():
                if not isinstance(value, str) or not value or len(value) > 253:
                    raise ValueError()
                if not key.endswith("uid") and not re.fullmatch(
                    r"[a-z0-9][a-z0-9.-]*", value
                ):
                    raise ValueError()
            if not doc["issuer"]["policy_uid"] or not doc["issuer"]["binding_uid"]:
                raise ValueError()
            if doc["version"] == 2:
                from ..shared_dependencies import validate_descriptor

                validate_descriptor(doc["shared_dependencies"], doc)
            return cls(authority_id, canonical(doc))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BootstrapRefused(
                "installed credential authority is invalid"
            ) from None

    def target(self, workspace_id, *, management=False):
        doc = self.document
        target = doc["management_target" if management else "target"]
        actor = doc["projector" if management else "issuer"]
        return VerifiedTarget(
            org_id=doc["org_id"],
            workspace_id=workspace_id,
            principal_arn=actor["role_arn"],
            cluster_ownership="adopted",
            **target,
        )

    def cluster_reference(self):
        doc, actor = self.document, self.document["issuer"]
        return ClusterAuthorityReference(
            doc["org_id"],
            doc["target"]["cluster_arn"],
            actor["role_arn"],
            actor["access_entry_arn"],
            actor["username"],
            actor["group"],
            actor["policy_uid"],
            actor["binding_uid"],
        )

    def projection(self, scope):
        if scope not in {"reader", "mutator"}:
            raise BootstrapRefused("unknown member credential scope")
        value = self.document["projection"]
        return {
            "namespace": value["namespace"],
            "namespace_uid": value["namespace_uid"],
            "secret_name": value[scope + "_secret"],
            "secret_uid": value[scope + "_secret_uid"],
            "scope": scope,
        }


async def load_authority(connection, org_id, cluster_id):
    row = await connection.fetchrow(
        "SELECT authority_id::text,document_json FROM cluster_credential_authorities "
        "WHERE org_id::text=$1 AND cluster_id::text=$2 AND enabled",
        str(org_id),
        str(cluster_id),
    )
    if row is None:
        raise BootstrapRefused(
            "cluster credential authority is not installed and enabled"
        )
    authority = Authority.read(row["authority_id"], row["document_json"])
    if (authority.document["org_id"], authority.document["cluster_id"]) != (
        str(org_id),
        str(cluster_id),
    ):
        raise BootstrapRefused("installed authority registry identity differs")
    return authority


async def require_runtime_database_role(connection):
    """Column grants confer write authority even without a table-level grant."""
    broad = await connection.fetchval(
        "SELECT has_table_privilege(current_user,'cluster_credential_authorities','INSERT,DELETE,TRUNCATE,TRIGGER') "
        "OR has_any_column_privilege(current_user,'cluster_credential_authorities','INSERT') "
        "OR EXISTS(SELECT 1 FROM unnest(ARRAY['authority_id','org_id','cluster_id','document_json','enabled']) AS col "
        "WHERE has_column_privilege(current_user,'cluster_credential_authorities',col,'UPDATE')) "
        "OR EXISTS(SELECT 1 FROM unnest(ARRAY['clusters','workspaces','cluster_memberships']) AS tab "
        "WHERE has_table_privilege(current_user,tab,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER') "
        "OR has_any_column_privilege(current_user,tab,'INSERT') "
        "OR EXISTS(SELECT 1 FROM pg_attribute a WHERE a.attrelid=tab::regclass "
        "AND a.attnum>0 AND NOT a.attisdropped "
        "AND NOT (tab='cluster_memberships' AND a.attname='updated_at') "
        "AND has_column_privilege(current_user,tab,a.attname,'UPDATE')))"
    )
    if broad:
        raise BootstrapRefused(
            "renewal database role may not install or enable its own authority"
        )


async def acquire(connection, authority, holder):
    fence = await connection.fetchval(
        "UPDATE cluster_credential_authorities SET holder=$2,fence_token=fence_token+1,"
        "lease_expires_at=clock_timestamp()+interval '60 seconds' "
        "WHERE authority_id::text=$1 AND enabled AND document_json=$3 "
        "AND (holder=$2 OR holder IS NULL OR lease_expires_at<=clock_timestamp()) RETURNING fence_token",
        authority.authority_id,
        holder,
        authority.document_json,
    )
    if fence is None:
        raise BootstrapRefused("cluster credential reconciliation lease is held")
    return fence


async def renew(connection, authority, holder, fence):
    """Refresh only the current live installed lease; never revive an old fence."""
    extended = await connection.fetchval(
        "UPDATE cluster_credential_authorities SET lease_expires_at=clock_timestamp()+interval '60 seconds' "
        "WHERE authority_id::text=$1 AND enabled AND document_json=$2 AND holder=$3 "
        "AND fence_token=$4 AND lease_expires_at>clock_timestamp() RETURNING fence_token",
        authority.authority_id,
        authority.document_json,
        holder,
        fence,
    )
    if extended != fence:
        raise BootstrapRefused(
            "credential reconciliation lease expired or was replaced"
        )


async def verify(connection, authority, holder, fence, binding=None, action=None):
    if not await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM cluster_credential_authorities WHERE authority_id::text=$1 "
        "AND enabled AND document_json=$2 AND holder=$3 AND fence_token=$4 AND lease_expires_at>clock_timestamp())",
        authority.authority_id,
        authority.document_json,
        holder,
        fence,
    ):
        raise BootstrapRefused(
            "installed cluster credential authority is revoked or fenced"
        )
    if binding is None:
        return
    member = binding.membership
    if (member.org_id, member.cluster_id, member.cluster_arn, member.endpoint) != (
        authority.document["org_id"],
        authority.document["cluster_id"],
        authority.document["target"]["cluster_arn"],
        authority.document["target"]["endpoint"],
    ):
        raise BootstrapRefused("renewal membership differs from installed cluster")
    cleanup = action in {"revoke", "unproject", "cleanup"}
    row = await connection.fetchrow(
        "SELECT m.state,m.namespace_uid,w.status,k.state AS credential_state FROM cluster_memberships m "
        "JOIN workspaces w ON w.id=m.workspace_id AND w.org_id=m.org_id "
        "JOIN clusters c ON c.id=m.cluster_id AND c.org_id=m.org_id "
        "JOIN membership_credentials k ON k.membership_id=m.id AND k.revision=$5 AND k.scope=$6 "
        "WHERE m.org_id::text=$1 AND m.workspace_id::text=$2 AND m.cluster_id::text=$3 AND m.generation=$4 "
        "AND m.namespace=$7 AND k.namespace_uid=$8 "
        "AND c.eks_cluster_arn=$9 AND c.endpoint=$10 "
        "AND ($11 OR (w.cluster_id=m.cluster_id AND w.shared_cluster_id=m.cluster_id "
        "AND w.namespace_name=m.namespace AND c.sharing_enabled AND c.status IN ('Ready','Active') "
        "AND ($12=false OR c.platform_eligible)))",
        member.org_id,
        member.workspace_id,
        member.cluster_id,
        member.generation,
        binding.revision,
        binding.scope,
        member.namespace,
        binding.namespace_uid,
        member.cluster_arn,
        member.endpoint,
        cleanup,
        authority.document["target"]["cluster_arn"]
        == authority.document["management_target"]["cluster_arn"],
    )
    if row is None or row["namespace_uid"] != binding.namespace_uid:
        raise BootstrapRefused("credential membership incarnation changed")
    if cleanup:
        if row["credential_state"] not in {"revoking", "revoked"}:
            raise BootstrapRefused("credential cleanup was not durably fenced")
    elif (
        row["state"] != "active"
        or row["status"] not in {"Ready", "active"}
        or row["credential_state"] not in {"reserved", "issued", "projected", "active"}
    ):
        raise BootstrapRefused("membership no longer authorizes credential renewal")
