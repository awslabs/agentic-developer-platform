"""Owner-invoked native closed cleanup. No HTTP route or credential response.

Run the maintained module in the reviewed Gateway image with a protected permit
ID. The authority record selects every target. Paid dispatch remains closed.
"""

import argparse
import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

from src.internal import domain_bootstrap_recovery as bootstrap
from src.internal import domain_operation_store as stores
from src.internal.domain_closed_cleanup_authority import CONFIG, ServiceAuthority, native_paths, read_permit, source_digest
from src.internal.domain_closed_cleanup_store import NativeStore
from src.internal.domain_operation_dispatch import paid_operation
from src.internal.domain_operation_runtime import bootstrap_store
from src.internal.domain_provider_authority import require, tables
from src.internal.domain_provider_session import sealed_target
from src.shared.database import get_engine, get_session_factory
from src.shared.services.secrets_manager import SecretsManagerHelper


class SessionClient:
    """Refresh service-held AWS sessions without caching authority decisions."""

    def __init__(self, session):
        self.session = session

    def __getattr__(self, method):
        require(
            method in {"describe_cluster", "list_access_entries", "describe_access_entry", "list_associated_access_policies", "create_access_entry"}
        )

        def call(**kwargs):
            return getattr(self.session().client("eks", config=CONFIG), method)(**kwargs)

        return call


def wire(value):
    return json.loads(json.dumps(value, default=str))


async def execute(permit_id, *, teardown_only=False):
    permit = await asyncio.to_thread(read_permit, permit_id)
    native_paths()
    from superplane_bootstrap.closed_cleanup_boundary import cleanup_revalidator
    from superplane_bootstrap.closed_cleanup_permit import entry_spec

    op = permit["operation"]
    binding = stores.binding_for("superplane", op["org_id"])
    require(binding.adp_org_id == permit["authority"]["adp_org_id"])
    domain_context = nullcontext(None) if teardown_only else stores.domain_connect(binding)
    async with stores.operation_connect(binding) as operations, domain_context as domain, get_session_factory()() as db:
        async with operations.transaction():
            await operations.execute("SET LOCAL lock_timeout = '3000ms'")
            require(
                await operations.fetchval(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended($1,0))", "harness-provider-dispatch:" + op["operation_id"]
                )
            )
            require(
                await operations.fetchval(
                    "SELECT operation_id FROM harness_operations WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3 FOR UPDATE",
                    op["operation_id"],
                    op["org_id"],
                    op["workspace_id"],
                )
            )
            await operations.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1 FOR UPDATE", op["operation_id"])

            async def boundary():
                operation = await paid_operation(binding, op["operation_id"], connection=operations)
                lease = dict(await operations.fetchrow("SELECT * FROM harness_operation_leases WHERE operation_id=$1", op["operation_id"]))
                require(wire(operation) == op and wire(lease) == permit["lease"])
                a = permit["authority"]
                credential, account = sealed_target(operation, a["region"])
                require(credential == (a["credential_id"], a["service"], a["label"]) and account == a["account_id"])
                calls = await operations.fetch("SELECT * FROM harness_provider_call_intent WHERE operation_id=$1", op["operation_id"])
                calls = await stores.harness("provider_retry").active_call_rows(operations, op["operation_id"], calls)
                require(len(calls) == 1)
                expiry = await bootstrap.cleanup_authority(db, bootstrap_store(), binding, operation, lease, dict(calls[0]))
                descriptor = permit["descriptor"]
                require(
                    all(
                        wire(expiry[key]) == descriptor[key]
                        for key in ("operation_id", "source_key", "source_fence", "source_principal", "source_acquired_at", "settled_bootstrap")
                    )
                )
                return expiry

            async def snapshot():
                request = stores.harness("identity").decode_payload(op["request_payload"])
                artifact = await domain.fetchrow(
                    "SELECT * FROM workspace_lifecycle_artifacts WHERE artifact_id=$1 AND org_id=$2 AND workspace_id=$3",
                    request.parameters["lifecycle_artifact_id"],
                    op["org_id"],
                    op["workspace_id"],
                )
                require(artifact is not None and wire(dict(artifact)) == permit["artifact"])
                require(
                    await domain.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM workspaces WHERE id::text=$1 AND org_id::text=$2 "
                        "AND provisioning_operation_id=$3 AND status='Provisioning' AND cluster_id IS NULL)",
                        op["workspace_id"],
                        op["org_id"],
                        op["operation_id"],
                    )
                )
                for table in ("workspace_lifecycle_effects", "cluster_memberships"):
                    require(not await domain.fetchval("SELECT EXISTS(SELECT 1 FROM " + table + " WHERE workspace_id::text=$1)", op["workspace_id"]))
                require(
                    not await domain.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM workspace_lifecycle_artifacts WHERE source_operation_id=$1)", op["operation_id"]
                    )
                )
                rows = [
                    dict(row)
                    for row in await domain.fetch("SELECT * FROM workspace_bootstrap_authority WHERE workspace_id::text=$1", op["workspace_id"])
                ]
                closure = permit["qualification"]["settled_bootstrap"]
                current = stores.harness("settled_bootstrap_repeat").journals_bound(closure, rows)
                count = await domain.fetchval("SELECT count(*) FROM workspace_bootstrap_reservations WHERE workspace_id::text=$1", op["workspace_id"])
                require(count in ({0, 1} if current["revoked"] else {1}))
                return {"journals": rows}

            authority = ServiceAuthority(permit, db, SecretsManagerHelper(), boundary)
            await authority.revalidate()
            if not teardown_only:
                await snapshot()
            loop = asyncio.get_running_loop()
            # Do not cancel this thread while it holds native locks/credentials.
            # All external calls have transport timeouts and check permit expiry.
            task = asyncio.create_task(
                asyncio.to_thread(compose, permit, authority, domain, loop, snapshot, boundary, cleanup_revalidator, entry_spec, teardown_only)
            )
            try:
                result = await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
            await boundary()
            return result


def policy(actions, resources, condition=None):
    return {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Action": actions, "Resource": resources, **({"Condition": condition} if condition else {})}],
    }


def make_verifier(permit, authority, wait, reader):
    from superplane_bootstrap.closed_cleanup_access import VerifierAccess
    from superplane_bootstrap.closed_cleanup_journal import CleanupJournal
    from superplane_bootstrap.closed_cleanup_permit import VIEW_POLICY, entry_spec

    a, q = permit["authority"], permit["qualification"]
    cluster = q["cluster_arn"]
    verifier_prefix = cluster.replace(":cluster/", ":access-entry/") + "/role/" + a["account_id"] + "/" + a["role_arn"].rsplit("/", 1)[1] + "/*"

    def current():
        wait(authority.revalidate())

    spec = entry_spec(permit)
    create_policy = policy(
        ["eks:CreateAccessEntry"],
        cluster,
        {
            "StringEquals": {
                "eks:principalArn": a["role_arn"],
                "eks:accessEntryType": "STANDARD",
                "eks:username": spec["username"],
                **{"aws:RequestTag/" + k: v for k, v in spec["tags"].items()},
            },
            "ForAllValues:StringEquals": {"aws:TagKeys": list(spec["tags"])},
            "Null": {"eks:kubernetesGroups": "true"},
        },
    )
    create_policy["Statement"].append(
        {
            "Effect": "Allow",
            "Action": ["eks:TagResource"],
            "Resource": verifier_prefix,
            "Condition": {
                "StringEquals": {"aws:RequestTag/" + k: v for k, v in spec["tags"].items()},
                "ForAllValues:StringEquals": {"aws:TagKeys": list(spec["tags"])},
            },
        }
    )
    creator = SessionClient(lambda: wait(authority.registrar(create_policy)))
    policy_reader = SessionClient(lambda: wait(authority.registrar(policy(["eks:ListAssociatedAccessPolicies"], verifier_prefix))))
    observer = SimpleNamespace(
        describe_access_entry=reader.describe_access_entry, list_associated_access_policies=policy_reader.list_associated_access_policies
    )

    def verifier_client(arn):
        require(arn.startswith(verifier_prefix[:-1]) and arn != verifier_prefix[:-1])
        policy_doc = policy(
            ["eks:AssociateAccessPolicy"],
            arn,
            {"StringEquals": {"eks:policyArn": VIEW_POLICY, "eks:accessScope": "cluster"}},
        )
        # DisassociateAccessPolicy identifies the policy in its URI and has no
        # accessScope request field. Requiring that key would deny teardown.
        policy_doc["Statement"].extend(policy(["eks:DisassociateAccessPolicy"], arn, {"StringEquals": {"eks:policyArn": VIEW_POLICY}})["Statement"])
        policy_doc["Statement"].append({"Effect": "Allow", "Action": ["eks:DeleteAccessEntry"], "Resource": arn})
        return wait(authority.registrar(policy_doc)).client("eks", config=CONFIG)

    authority_table, evidence_table = tables()
    journal = CleanupJournal(stores.aws_client("dynamodb"), authority_table, evidence_table, permit, current)
    verifier = VerifierAccess(permit, journal, creator, observer, current, verifier_client)
    return verifier


def compose(permit, authority, domain, loop, snapshot, boundary, revalidator_factory, entry_factory, teardown_only):
    from superplane_bootstrap.authority_journal import AuthorityJournal
    from superplane_bootstrap.closed_cleanup_kube import ScopedKubernetes
    from superplane_bootstrap.closed_cleanup_run import run_closed_cleanup
    from superplane_bootstrap.eks_grants import EksGrants, _absent, _pages
    from superplane_bootstrap.registry import SqlRegistrationStore
    from superplane_bootstrap.target import VerifiedTarget
    from superplane_contracts.provisioning import OperationBinding, ResolvedPrincipal

    store = NativeStore(domain, loop)
    wait = store.wait
    q, a = permit["qualification"], permit["authority"]

    def current():
        wait(authority.revalidate())

    if teardown_only:
        cluster = q["cluster_arn"]
        read_policy = policy(["eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies"], cluster.replace(":cluster/", ":access-entry/") + "/*")
        reader = SessionClient(lambda: wait(authority.provider(read_policy)))
        verifier = make_verifier(permit, authority, wait, reader)
        return verifier.teardown(abandon="result" not in verifier.journal.read())
    revalidate_native = revalidator_factory(
        q, permit["descriptor"], read_snapshot=lambda: wait(snapshot()), read_expiry=lambda: wire(wait(boundary()))
    )

    def revalidate():
        current()
        result = revalidate_native()
        current()
        return result

    cluster = q["cluster_arn"]
    entry_prefix = cluster.replace(":cluster/", ":access-entry/") + "/"
    read_policy = policy(
        ["eks:DescribeCluster", "eks:ListAccessEntries", "eks:DescribeAccessEntry", "eks:ListAssociatedAccessPolicies"], [cluster, entry_prefix + "*"]
    )
    # Distinct STS sessions supply independent AWS observations and mutation.
    reader = SessionClient(lambda: wait(authority.provider(read_policy)))
    observed = reader.describe_cluster(name=cluster.rsplit("/", 1)[1])["cluster"]
    metadata = json.loads(permit["artifact"]["artifact_metadata_json"])
    outputs = {key: value["value"] for key, value in metadata["outputs"].items()}
    require(metadata["next_phase"] == "bootstrap-workspace")
    require(
        all(
            outputs[key] == expected
            for key, expected in (
                ("cluster_arn", cluster),
                ("account_id", a["account_id"]),
                ("aws_region", a["region"]),
                ("org_id", a["org_id"]),
                ("workspace_id", a["workspace_id"]),
            )
        )
    )
    require(observed["status"] == "ACTIVE" and observed["arn"] == cluster and observed["name"] == outputs["cluster_name"])
    require(
        observed["endpoint"] == outputs["cluster_endpoint"]
        and observed["certificateAuthority"]["data"] == outputs["cluster_certificate_authority_data"]
    )
    target = VerifiedTarget(
        org_id=a["org_id"],
        workspace_id=a["workspace_id"],
        account_id=a["account_id"],
        region=a["region"],
        cluster_name=outputs["cluster_name"],
        cluster_arn=cluster,
        endpoint=outputs["cluster_endpoint"],
        certificate_authority_data=outputs["cluster_certificate_authority_data"],
        principal_arn=permit["registrar"]["role_arn"],
        cluster_ownership="adp-created",
    )
    deadline = datetime.fromisoformat(permit["descriptor"]["source_acquired_at"]) + timedelta(seconds=permit["operation"]["max_runtime_seconds"])
    original = OperationBinding(
        operation_id=permit["operation"]["operation_id"],
        principal=ResolvedPrincipal(subject=permit["descriptor"]["source_principal"], org_id=a["org_id"], workspace_id=a["workspace_id"]),
        action="provision",
        permission="workspace:provision",
        expires_at=deadline,
    )
    require(original.is_expired(datetime.now(deadline.tzinfo)))
    native = AuthorityJournal(store, original, target, q["generation"], q["claim"])
    original_arns = {q["retained_progress"][g["key"]]["identity"]["arn"] for g in q["retained_plan"]["grants"] if g["kind"] == "eks-entry"}

    def original_client(arn):
        require(arn in original_arns)
        return wait(authority.provider(policy(["eks:DeleteAccessEntry", "eks:DisassociateAccessPolicy"], arn))).client("eks", config=CONFIG)

    eks = EksGrants(reader, target, entry_client=original_client)
    verifier = make_verifier(permit, authority, wait, reader)
    # Token material is generated only for this object-scoped transport. Neither
    # session factory returns through the command response or writes credentials.
    kube = ScopedKubernetes(
        permit,
        target,
        lambda: wait(authority.provider(policy(["sts:GetCallerIdentity"], "*"))),
        lambda: wait(authority.registrar(policy(["sts:GetCallerIdentity"], "*"))),
        revalidate,
    )

    def absent(plan):
        revalidate()
        require(plan == q["retained_plan"])
        entries = [g for g in plan["grants"] if g["kind"] == "eks-entry"]
        require(len(entries) == 3)
        for entry in entries:
            try:
                reader.describe_access_entry(clusterName=target.cluster_name, principalArn=entry["principal_arn"])
            except Exception as error:
                require(_absent(error))
            else:
                require(False)
        groups = {group for g in entries for group in g["groups"]}
        for principal in _pages(reader, "list_access_entries", "accessEntries", clusterName=target.cluster_name):
            observed_entry = reader.describe_access_entry(clusterName=target.cluster_name, principalArn=principal)["accessEntry"]
            require(observed_entry["principalArn"] == principal and not groups.intersection(observed_entry.get("kubernetesGroups", [])))
        revalidate()
        return True

    try:
        return run_closed_cleanup(
            native,
            SqlRegistrationStore(store),
            eks,
            kube,
            verifier,
            revalidate=revalidate,
            verify_absence=absent,
            read_component=kube.read_component,
            teardown_only=teardown_only,
        )
    except Exception:
        # Native SQL may have rolled back while provider deletions succeeded.
        # Remove only our reader; never restore revoked grants or erase history.
        try:
            verifier.teardown(abandon="result" not in verifier.journal.read())
        except Exception:
            pass  # Protected intent remains pending for fresh-owner teardown.
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("source-digest", "cleanup", "teardown"))
    parser.add_argument("--permit-id")
    args = parser.parse_args()
    if args.mode == "source-digest":
        require(args.permit_id is None)
        print(json.dumps({"service_source_sha256": source_digest()}))
        return
    require(args.permit_id is not None)

    async def run():
        try:
            return await execute(args.permit_id, teardown_only=args.mode == "teardown")
        finally:
            await get_engine().dispose()

    try:
        print(json.dumps(asyncio.run(run())))
    except Exception:
        # No tracebacks/provider bodies/credentials on the operator channel.
        print(
            json.dumps(
                {
                    "cleanup_complete": False,
                    "permit_id": args.permit_id,
                    "status": "refused-or-unavailable",
                    "next_step": "reconcile protected cleanup journal; use teardown with current owner authority",
                }
            )
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
