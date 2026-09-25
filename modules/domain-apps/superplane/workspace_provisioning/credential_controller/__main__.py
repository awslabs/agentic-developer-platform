"""Installed controller process; registration runs separately with installer DB rights."""

import asyncio
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import ssl
import sys
import tempfile
import time
from uuid import uuid4

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.membership import SharedMembership

from .registry import Authority, acquire, load_authority, renew, verify
from .renewal import Renewal
from .transports import compose_transports


def read_file(name):
    path = Path(os.environ[name])
    if not path.is_absolute():
        raise BootstrapRefused("credential controller requires explicit mounted files")
    value = path.read_text()
    if not value or len(value) > 262144:
        raise BootstrapRefused("credential controller mounted configuration is invalid")
    return value


def source_identity(authority):
    import boto3

    source = boto3.Session(
        region_name=authority.document["management_target"]["region"]
    )
    credentials = source.get_credentials()
    if (
        credentials is None
        or credentials.method != "assume-role-with-web-identity"
        or os.environ.get("AWS_ROLE_ARN") != authority.document["controller_role_arn"]
    ):
        raise BootstrapRefused(
            "credential controller requires its installed IRSA identity"
        )
    identity = source.client("sts").get_caller_identity()
    if (
        identity.get("UserId", "").split(":", 1)[0]
        != authority.document["controller_role_id"]
    ):
        raise BootstrapRefused("installed credential controller role was replaced")
    return source


class Bridge:
    def __init__(self, pool, loop, authority, holder, fence):
        self.pool, self.loop, self.authority = pool, loop, authority
        self.holder, self.fence = holder, fence
        self.binding, self.action = None, None
        self.member, self.namespace_uid, self.retiring = None, None, False

    def wait(self, coroutine):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=30)
        except BaseException:
            future.cancel()
            raise

    async def _verify(self, binding=None, action=None):
        async with self.pool.acquire() as connection:
            await verify(
                connection, self.authority, self.holder, self.fence, binding, action
            )
            if binding is None and self.member is not None:
                member = self.member
                valid = await connection.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM cluster_memberships m "
                    "JOIN workspaces w ON w.id=m.workspace_id AND w.org_id=m.org_id "
                    "JOIN clusters c ON c.id=m.cluster_id AND c.org_id=m.org_id "
                    "WHERE m.org_id::text=$1 AND m.workspace_id::text=$2 AND m.cluster_id::text=$3 "
                    "AND m.generation=$4 AND m.namespace_uid=$5 AND c.eks_cluster_arn=$6 AND c.endpoint=$7 "
                    "AND ($8 OR (m.state='active' AND w.status IN ('Ready','active') "
                    "AND w.cluster_id=m.cluster_id AND w.shared_cluster_id=m.cluster_id "
                    "AND w.namespace_name=m.namespace AND c.sharing_enabled AND c.status IN ('Ready','Active') "
                    "AND ($9=false OR c.platform_eligible))))",
                    member.org_id,
                    member.workspace_id,
                    member.cluster_id,
                    member.generation,
                    self.namespace_uid,
                    member.cluster_arn,
                    member.endpoint,
                    self.retiring,
                    self.authority.document["target"]["cluster_arn"]
                    == self.authority.document["management_target"]["cluster_arn"],
                )
                if not valid:
                    raise BootstrapRefused(
                        "member changed before credential provider access"
                    )
            await renew(connection, self.authority, self.holder, self.fence)

    def check(self):
        self.wait(self._verify(self.binding, self.action))

    def authorize(self, binding, action):
        self.binding, self.action = binding, action
        self.check()

    def store(self, function, *args, **kwargs):
        async def call():
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    await verify(connection, self.authority, self.holder, self.fence)
                    return await function(connection, *args, **kwargs)

        return self.wait(call())


async def candidates(connection, authority, after=None):
    return [
        dict(row)
        for row in await connection.fetch(
            "SELECT m.*,w.status AS workspace_status,c.eks_cluster_arn,c.endpoint "
            "FROM cluster_memberships m JOIN workspaces w ON w.id=m.workspace_id AND w.org_id=m.org_id "
            "JOIN clusters c ON c.id=m.cluster_id AND c.org_id=m.org_id "
            "WHERE m.org_id::text=$1 AND m.cluster_id::text=$2 AND m.namespace_uid IS NOT NULL "
            "AND m.state IN ('active','removed') "
            "AND (m.state='active' OR EXISTS(SELECT 1 FROM membership_credentials k WHERE k.membership_id=m.id AND k.state<>'revoked')) "
            "AND ($3::uuid IS NULL OR m.id>$3::uuid) "
            "AND (m.state<>'active' OR w.status NOT IN ('Ready','active') OR "
            "EXISTS(SELECT 1 FROM membership_credentials k WHERE k.membership_id=m.id "
            "AND k.state<>'revoked' AND (k.state<>'active' OR k.expires_at<=clock_timestamp()+interval '5 minutes')) "
            "OR NOT EXISTS(SELECT 1 FROM membership_credentials k WHERE k.membership_id=m.id AND k.scope='reader' AND k.state='active') "
            "OR NOT EXISTS(SELECT 1 FROM membership_credentials k WHERE k.membership_id=m.id AND k.scope='mutator' AND k.state='active')) "
            "ORDER BY m.id LIMIT 256",
            authority.document["org_id"],
            authority.document["cluster_id"],
            after,
        )
    ]


def reconcile_member(authority, row, bridge):
    member = SharedMembership.create(
        org_id=str(row["org_id"]),
        workspace_id=str(row["workspace_id"]),
        cluster_id=str(row["cluster_id"]),
        request_id=str(row["operation_id"]),
        cluster_arn=row["eks_cluster_arn"],
        endpoint=row["endpoint"],
    )
    if member.generation != row["generation"] or member.namespace != row["namespace"]:
        raise BootstrapRefused(
            "renewal membership generation differs from original reservation"
        )
    bridge.member, bridge.namespace_uid = member, row["namespace_uid"]
    bridge.retiring = row["state"] == "removed" or row["workspace_status"] in {
        "Teardown",
        "retired",
        "Deleted",
    }
    bridge.check()
    source = source_identity(authority)
    bridge.check()
    with tempfile.TemporaryDirectory(prefix="sp-credential-") as directory:
        issuer, projector = compose_transports(
            authority, source, bridge.check, directory, member.workspace_id
        )
        try:
            renewal = Renewal(
                authority=authority,
                store=bridge.store,
                authorize=bridge.authorize,
                issuer_grants=issuer,
                projector_grants=projector,
                directory=directory,
            )
            renewal.reconcile(member, row["namespace_uid"], retiring=bridge.retiring)
        finally:
            issuer.client.client.close()
            projector.client.client.close()


async def register(pool, authority):
    """Explicit installer mode; runtime DSN must lack these registry write rights."""
    source = await asyncio.to_thread(source_identity, authority)
    with tempfile.TemporaryDirectory(prefix="sp-credential-install-") as directory:
        issuer, projector = await asyncio.to_thread(
            compose_transports,
            authority,
            source,
            lambda: source_identity(authority),
            directory,
            str(uuid4()),
        )
        # Both projection namespaces/Secrets must already exist and match the
        # reviewed installed UIDs. Registration never invents cloud prerequisites.
        for scope in ("reader", "mutator"):
            projection = authority.projection(scope)
            namespace = projector.client.resources.get(
                api_version="v1", kind="Namespace"
            ).get(name=projection["namespace"])
            secret = projector.client.resources.get(
                api_version="v1", kind="Secret"
            ).get(name=projection["secret_name"], namespace=projection["namespace"])
            if (
                namespace.metadata.uid != projection["namespace_uid"]
                or secret.metadata.uid != projection["secret_uid"]
            ):
                raise BootstrapRefused("installed projection identity differs")
    async with pool.acquire() as connection:
        async with connection.transaction():
            match = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM clusters WHERE org_id::text=$1 AND id::text=$2 "
                "AND sharing_enabled AND eks_cluster_arn=$3 AND endpoint=$4)",
                authority.document["org_id"],
                authority.document["cluster_id"],
                authority.document["target"]["cluster_arn"],
                authority.document["target"]["endpoint"],
            )
            if not match:
                raise BootstrapRefused(
                    "installed authority requires an existing approved shared cluster"
                )
            await connection.execute(
                "INSERT INTO cluster_credential_authorities(authority_id,org_id,cluster_id,document_json,enabled) "
                "VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,$4,true) ON CONFLICT DO NOTHING",
                authority.authority_id,
                authority.document["org_id"],
                authority.document["cluster_id"],
                authority.document_json,
            )
            row = await load_authority(
                connection,
                authority.document["org_id"],
                authority.document["cluster_id"],
            )
            if row != authority:
                raise BootstrapRefused(
                    "credential authority registration cannot replace existing installation"
                )


async def main():
    import asyncpg

    install = sys.argv[1:] == ["--register"]
    if sys.argv[1:] not in ([], ["--register"]):
        raise BootstrapRefused("unknown credential controller command")
    config = json.loads(read_file("SUPERPLANE_CREDENTIAL_CONTROLLER_CONFIG"))
    if not isinstance(config, list) or not 1 <= len(config) <= 64:
        raise BootstrapRefused("installed credential authority list is invalid")
    authorities = [
        Authority.read(entry["authority_id"], json.dumps(entry["document"]))
        for entry in config
    ]
    schema = os.environ["SUPERPLANE_DOMAIN_SCHEMA"]
    if not schema.replace("_", "a").isalnum() or schema == "public":
        raise BootstrapRefused(
            "credential controller requires the isolated domain schema"
        )
    tls = ssl.create_default_context(cafile=os.environ["SUPERPLANE_DATABASE_CA_FILE"])
    pool = await asyncpg.create_pool(
        read_file("SUPERPLANE_DOMAIN_DSN_FILE").strip(),
        min_size=1,
        max_size=4,
        timeout=10,
        command_timeout=20,
        ssl=tls,
        server_settings={"search_path": schema + ",public"},
    )
    try:
        if install:
            for authority in authorities:
                await register(pool, authority)
            return
        async with pool.acquire() as connection:
            broad = await connection.fetchval(
                "SELECT has_table_privilege(current_user,'cluster_credential_authorities','INSERT,DELETE') "
                "OR EXISTS(SELECT 1 FROM unnest(ARRAY['authority_id','org_id','cluster_id','document_json','enabled']) AS col "
                "WHERE has_column_privilege(current_user,'cluster_credential_authorities',col,'UPDATE')) "
                "OR EXISTS(SELECT 1 FROM unnest(ARRAY['clusters','workspaces','cluster_memberships']) AS tab "
                "WHERE has_table_privilege(current_user,tab,'INSERT,UPDATE,DELETE'))"
            )
            if broad:
                raise BootstrapRefused(
                    "renewal database role may not install or enable its own authority"
                )
        holder, loop = str(uuid4()), asyncio.get_running_loop()
        cursors = {}
        while True:
            healthy = True
            for expected in authorities:
                try:
                    async with pool.acquire() as connection:
                        authority = await load_authority(
                            connection,
                            expected.document["org_id"],
                            expected.document["cluster_id"],
                        )
                        if authority != expected:
                            raise BootstrapRefused(
                                "installed controller configuration differs from server registry"
                            )
                        rows = await candidates(
                            connection, authority, cursors.get(authority.authority_id)
                        )
                        cursors[authority.authority_id] = (
                            rows[-1]["id"] if rows else None
                        )
                    for row in rows:
                        try:
                            async with pool.acquire() as connection:
                                fence = await acquire(connection, authority, holder)
                            await asyncio.to_thread(
                                reconcile_member,
                                authority,
                                row,
                                Bridge(pool, loop, authority, holder, fence),
                            )
                        except Exception:
                            healthy = False
                            print(
                                json.dumps(
                                    {
                                        "authority_id": authority.authority_id,
                                        "workspace_id": str(row["workspace_id"]),
                                        "state": "member_reconcile_refused",
                                    }
                                ),
                                flush=True,
                            )
                except Exception:
                    healthy = False
                    # Provider exceptions may carry tokens/DSNs. Report bounded
                    # failure only; durable journals preserve recovery evidence.
                    print(
                        json.dumps(
                            {
                                "authority_id": expected.authority_id,
                                "state": "reconcile_refused",
                                "at": datetime.now(UTC).isoformat(),
                            }
                        ),
                        flush=True,
                    )
            if healthy:
                Path("/tmp/credential-ready").write_text(str(time.time()))
            await asyncio.sleep(15)
    finally:
        await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print(
            "credential controller configuration or installed authority refused",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
