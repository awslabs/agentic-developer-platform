"""Typed SA/RBAC effect journal for installed credential renewal only."""

import json

from superplane_bootstrap.errors import BootstrapRefused

from .registry import canonical


async def intent(connection, binding, spec):
    member = binding.membership
    member_id = await connection.fetchval(
        "SELECT id FROM cluster_memberships WHERE workspace_id::text=$1 AND org_id::text=$2 "
        "AND cluster_id::text=$3 AND generation=$4",
        member.workspace_id,
        member.org_id,
        member.cluster_id,
        member.generation,
    )
    body = spec["body"]
    if (
        body["kind"] not in {"ServiceAccount", "Role", "RoleBinding"}
        or body["metadata"]["namespace"] != member.namespace
    ):
        raise BootstrapRefused("credential delegation must be namespace-owned SA/RBAC")
    raw = canonical(spec)
    await connection.execute(
        "INSERT INTO membership_credential_components(membership_id,revision,scope,kind,name,desired_json,state) "
        "VALUES($1,$2,$3,$4,$5,$6,'planned') ON CONFLICT DO NOTHING",
        member_id,
        binding.revision,
        binding.scope,
        body["kind"],
        body["metadata"]["name"],
        raw,
    )
    row = await connection.fetchrow(
        "SELECT * FROM membership_credential_components WHERE membership_id=$1 AND revision=$2 "
        "AND scope=$3 AND kind=$4 AND name=$5 FOR UPDATE",
        member_id,
        binding.revision,
        binding.scope,
        body["kind"],
        body["metadata"]["name"],
    )
    if row is None or row["desired_json"] != raw or row["state"] == "revoked":
        raise BootstrapRefused("credential delegation intent changed or was revoked")
    return dict(row)


async def observed(connection, row, identity):
    raw = canonical(identity)
    changed = await connection.fetchval(
        "UPDATE membership_credential_components SET identity_json=$6,state='created' "
        "WHERE membership_id=$1 AND revision=$2 AND scope=$3 AND kind=$4 AND name=$5 "
        "AND state IN ('planned','created') AND (identity_json IS NULL OR identity_json=$6) RETURNING name",
        row["membership_id"],
        row["revision"],
        row["scope"],
        row["kind"],
        row["name"],
        raw,
    )
    if changed is None:
        raise BootstrapRefused("credential delegation provider identity changed")


async def rows(connection, binding):
    return [
        dict(row)
        for row in await connection.fetch(
            "SELECT d.* FROM membership_credential_components d JOIN cluster_memberships m ON m.id=d.membership_id "
            "WHERE m.workspace_id::text=$1 AND m.org_id::text=$2 AND m.generation=$3 AND d.revision=$4 AND d.scope=$5",
            binding.membership.workspace_id,
            binding.membership.org_id,
            binding.membership.generation,
            binding.revision,
            binding.scope,
        )
    ]


async def removed(connection, row):
    await connection.execute(
        "UPDATE membership_credential_components SET state='revoked' "
        "WHERE membership_id=$1 AND revision=$2 AND scope=$3 AND kind=$4 AND name=$5",
        row["membership_id"],
        row["revision"],
        row["scope"],
        row["kind"],
        row["name"],
    )


def establish(store, grants, authorize, binding, specs):
    identities = {}
    for spec in specs:
        authorize(binding, "issue")
        row = store(intent, binding, spec)
        authorize(binding, "issue")
        identity = grants.observe(spec)
        if identity is None:
            authorize(binding, "issue")
            identity = grants.create(spec)
        grants.verify(spec, identity)
        if (
            row["identity_json"] is not None
            and json.loads(row["identity_json"]) != identity
        ):
            raise BootstrapRefused("credential delegation object was replaced")
        authorize(binding, "issue")
        store(observed, row, identity)
        identities[spec["body"]["kind"]] = identity
    return identities


def cleanup(store, grants, authorize, binding):
    for row in store(rows, binding):
        if row["state"] == "revoked":
            continue
        spec = json.loads(row["desired_json"])
        authorize(binding, "cleanup")
        identity = grants.observe(spec)
        if identity is not None:
            grants.verify(spec, identity)
            if (
                row["identity_json"] is not None
                and json.loads(row["identity_json"]) != identity
            ):
                raise BootstrapRefused(
                    "refusing to delete replacement credential delegation"
                )
            authorize(binding, "cleanup")
            grants.delete(spec, identity)
            authorize(binding, "cleanup")
            if grants.observe(spec) is not None:
                raise BootstrapRefused("credential delegation deletion is not observed")
        store(removed, row)
