"""Transactional non-secret identity for member credential rotation.

This is a domain journal, not issuance authority. The caller must obtain fresh
service/operation authority before provider effects and consumer acknowledgement.
"""

from .runtime_config import LifecycleRefused
from .shared_membership import verify


async def _membership(connection, binding):
    from .member_credentials.binding import CredentialBinding

    if not isinstance(binding, CredentialBinding):
        raise LifecycleRefused("credential journal requires immutable binding")
    if not connection.is_in_transaction():
        raise LifecycleRefused("credential journal requires the caller transaction")
    member = binding.membership
    row = await connection.fetchrow(
        "SELECT id::text,namespace_uid FROM cluster_memberships "
        "WHERE org_id::text=$1 AND workspace_id::text=$2 AND cluster_id::text=$3 "
        "AND generation=$4 FOR UPDATE",
        member.org_id,
        member.workspace_id,
        member.cluster_id,
        member.generation,
    )
    await verify(connection, member, states={"reserved", "active"})
    if row is None or row["namespace_uid"] not in {None, binding.namespace_uid}:
        raise LifecycleRefused("credential namespace identity changed")
    if await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM membership_credentials WHERE membership_id=$1::text::uuid AND namespace_uid<>$2)",
        row["id"],
        binding.namespace_uid,
    ):
        raise LifecycleRefused("credential namespace identity changed")
    return row["id"]


async def reserve(connection, binding):
    """Reserve an exact revision before creating its ServiceAccount or token."""
    member_id = await _membership(connection, binding)
    existing = await connection.fetchrow(
        "SELECT * FROM membership_credentials WHERE membership_id=$1::text::uuid "
        "AND revision=$2 AND scope=$3",
        member_id,
        binding.revision,
        binding.scope,
    )
    if existing is not None:
        if existing["namespace_uid"] != binding.namespace_uid or existing["state"] in {
            "revoking",
            "revoked",
        }:
            raise LifecycleRefused("credential revision cannot be rebound or revived")
        return dict(existing)
    previous = await connection.fetchval(
        "SELECT max(revision) FROM membership_credentials WHERE membership_id=$1::text::uuid AND scope=$2",
        member_id,
        binding.scope,
    )
    pending = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM membership_credentials WHERE membership_id=$1::text::uuid "
        "AND scope=$2 AND state IN ('reserved','issued','projected'))",
        member_id,
        binding.scope,
    )
    if pending or binding.revision != (previous or 0) + 1:
        raise LifecycleRefused(
            "credential revision is stale or another issuance is pending"
        )
    return dict(
        await connection.fetchrow(
            "INSERT INTO membership_credentials(membership_id,revision,scope,namespace_uid) "
            "VALUES($1::text::uuid,$2,$3,$4) RETURNING *",
            member_id,
            binding.revision,
            binding.scope,
            binding.namespace_uid,
        )
    )


async def delegated(connection, binding, *, service_account_uid):
    """Retain the revocation object before any token is requested from Kubernetes."""
    member_id = await _membership(connection, binding)
    from .member_credentials.binding import uid

    uid(service_account_uid)
    row = await connection.fetchrow(
        "UPDATE membership_credentials SET service_account_uid=$4 "
        "WHERE membership_id=$1::text::uuid AND revision=$2 AND scope=$3 AND namespace_uid=$5 "
        "AND state='reserved' AND (service_account_uid IS NULL OR service_account_uid=$4) RETURNING *",
        member_id,
        binding.revision,
        binding.scope,
        service_account_uid,
        binding.namespace_uid,
    )
    if row is None:
        raise LifecycleRefused("credential delegation identity changed")
    return dict(row)


async def issued(connection, binding, *, service_account_uid, expires_at):
    member_id = await _membership(connection, binding)
    if (
        not isinstance(service_account_uid, str)
        or not service_account_uid
        or len(service_account_uid) > 255
    ):
        raise LifecycleRefused("credential ServiceAccount identity is invalid")
    row = await connection.fetchrow(
        "UPDATE membership_credentials SET state='issued',service_account_uid=$4,expires_at=$5 "
        "WHERE membership_id=$1::text::uuid AND revision=$2 AND scope=$3 AND namespace_uid=$6 "
        "AND state IN ('reserved','issued') AND service_account_uid=$4 "
        "AND (expires_at IS NULL OR expires_at=$5) AND $5>clock_timestamp() "
        "AND $5<=clock_timestamp()+interval '1 hour' RETURNING *",
        member_id,
        binding.revision,
        binding.scope,
        service_account_uid,
        expires_at,
        binding.namespace_uid,
    )
    if row is None:
        raise LifecycleRefused("credential issuance identity or expiry changed")
    return dict(row)


async def projected(connection, binding, *, secret_uid, resource_version):
    member_id = await _membership(connection, binding)
    if any(
        not isinstance(value, str) or not value or len(value) > 255
        for value in (secret_uid, resource_version)
    ):
        raise LifecycleRefused("credential projection identity is invalid")
    row = await connection.fetchrow(
        "UPDATE membership_credentials SET state='projected',projection_uid=$4,projection_version=$5 "
        "WHERE membership_id=$1::text::uuid AND revision=$2 AND scope=$3 AND namespace_uid=$6 "
        "AND state IN ('issued','projected') AND (projection_uid IS NULL OR projection_uid=$4) "
        "AND expires_at>clock_timestamp() RETURNING *",
        member_id,
        binding.revision,
        binding.scope,
        secret_uid,
        resource_version,
        binding.namespace_uid,
    )
    if row is None:
        raise LifecycleRefused("credential projection is expired or changed")
    return dict(row)


async def activate(
    connection, binding, *, service_account_uid, secret_uid, resource_version
):
    """Called only after a trusted consumer proves this projected revision works."""
    member_id = await _membership(connection, binding)
    row = await connection.fetchrow(
        "SELECT * FROM membership_credentials WHERE membership_id=$1::text::uuid AND revision=$2 AND scope=$3 "
        "AND namespace_uid=$4 AND service_account_uid=$5 AND projection_uid=$6 AND projection_version=$7 "
        "AND state IN ('projected','active') AND expires_at>clock_timestamp()",
        member_id,
        binding.revision,
        binding.scope,
        binding.namespace_uid,
        service_account_uid,
        secret_uid,
        resource_version,
    )
    if row is None:
        raise LifecycleRefused(
            "credential acknowledgement differs from projected identity"
        )
    if row["state"] == "active":
        return dict(row)
    await connection.execute(
        "UPDATE membership_credentials SET state='revoking' WHERE membership_id=$1::text::uuid AND scope=$2 AND state='active'",
        member_id,
        binding.scope,
    )
    return dict(
        await connection.fetchrow(
            "UPDATE membership_credentials SET state='active',observed_at=clock_timestamp() "
            "WHERE membership_id=$1::text::uuid AND revision=$2 AND scope=$3 RETURNING *",
            member_id,
            binding.revision,
            binding.scope,
        )
    )


async def fence_revocation(connection, binding):
    """Withdraw one exact revision before provider revocation, including retired members."""
    from .member_credentials.binding import CredentialBinding

    if not isinstance(binding, CredentialBinding) or not connection.is_in_transaction():
        raise LifecycleRefused(
            "credential revocation requires its binding and transaction"
        )
    member = binding.membership
    row = await connection.fetchrow(
        "SELECT id FROM cluster_memberships WHERE org_id::text=$1 AND workspace_id::text=$2 "
        "AND cluster_id::text=$3 AND generation=$4 FOR UPDATE",
        member.org_id,
        member.workspace_id,
        member.cluster_id,
        member.generation,
    )
    if row is None:
        raise LifecycleRefused("credential revocation membership is absent")
    result = await connection.fetchrow(
        "UPDATE membership_credentials SET state=CASE WHEN state='revoked' THEN state ELSE 'revoking' END "
        "WHERE membership_id=$1 AND revision=$2 AND scope=$3 AND namespace_uid=$4 RETURNING *",
        row["id"],
        binding.revision,
        binding.scope,
        binding.namespace_uid,
    )
    if result is None:
        raise LifecycleRefused("credential revocation identity is absent or changed")
    return dict(result)


async def revoked(connection, binding, *, service_account_uid):
    """Record confirmed SA absence, never infer revocation from projection removal."""
    row = await fence_revocation(connection, binding)
    if row["service_account_uid"] != service_account_uid or not service_account_uid:
        raise LifecycleRefused(
            "credential revocation acknowledgement changed ServiceAccount"
        )
    return dict(
        await connection.fetchrow(
            "UPDATE membership_credentials SET state='revoked' WHERE membership_id=$1 "
            "AND revision=$2 AND scope=$3 RETURNING *",
            row["membership_id"],
            binding.revision,
            binding.scope,
        )
    )
