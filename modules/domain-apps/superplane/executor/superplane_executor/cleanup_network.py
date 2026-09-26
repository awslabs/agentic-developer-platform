"""One approved network key per shared step; recovery never invokes deletion."""

from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease

from .network_inventory import observe_native
from .network_journal import NetworkJournal
from .network_plan import canonical
from .network_runtime import Network


async def execute(provider, operation, target, plan, recipe, authorize):
    session, _ = await provider.session_for(operation, plan)

    class OneRecipe(Network):
        async def resource(self, key, descriptor, adopted, observe, create, delete):
            if key != recipe["key"] or descriptor != recipe["descriptor"]:
                raise OperationRefused(
                    "staged network callback differs from approved recipe"
                )
            await self.journal.release(
                key, observe=observe, delete=delete, expected=recipe
            )
            # Parent helpers must not traverse their association subrecipes.
            return None

    runtime = OneRecipe(provider, operation, target, plan, session, authorize)
    runtime.cleaning = True
    value, ref = recipe["descriptor"], recipe["reference"]
    region, kind = ref["region"], ref["kind"]
    side = (
        plan.network["cluster"]["network"]
        if region == plan.cluster_region
        else plan.network["regions"][region]["network"]
    )
    if kind == "attachment":
        await runtime.attachment(region, side)
    elif kind == "association":
        await runtime.association(region, side, value["attachment"])
    elif kind == "peering":
        remote = value["accepter_region"]
        await runtime.peering(remote, plan.network["regions"][remote])
    elif kind == "route":
        await runtime.route(region, **value)
    elif kind == "security-rule":
        await runtime.security(region, **value)
    else:
        raise OperationRefused("unsupported original cleanup recipe")


async def observe(provider, operation, plan, recipe, authorize):
    """Confirm exact known absence without inventing or replaying a deletion."""
    from harness_jobs.inventory import ResourcePresence

    journal = NetworkJournal(provider.domain_pool, operation, plan, authorize)
    lease = operation.grant.lease
    async with journal.locked(recipe["key"]) as db:
        row = await db.fetchrow(
            "SELECT * FROM controller_network_resources WHERE resource_key=$1",
            recipe["key"],
        )
        member = await db.fetchrow(
            "SELECT * FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
            recipe["key"],
            journal.allocation,
        )
        if (
            row is None
            or member is None
            or any(
                member[k] != expected
                for k, expected in {
                    "org_id": lease.org_id,
                    "workspace_id": lease.workspace_id,
                    "cluster_id": plan.network["cluster"]["cluster_id"],
                    "membership_generation": recipe["membership_generation"],
                    "source_operation_id": operation.request.parameters[
                        "controller_source_operation_id"
                    ],
                }.items()
            )
            or row["org_id"] != lease.org_id
        ):
            raise OperationRefused("original network cleanup membership differs")
        if member["released_at"] is not None:
            # This dependency is released even if a new generation serves peers.
            return True
        if (
            row["generation"] != recipe["generation"]
            or row["provider_reference"] != canonical(recipe["reference"])
            or row["descriptor"] != canonical(recipe["descriptor"])
            or row["owned"] != recipe["owned"]
        ):
            raise OperationRefused("original network cleanup native identity differs")
        effect = await db.fetchrow(
            "SELECT * FROM controller_network_effects WHERE resource_key=$1 AND generation=$2 AND action='delete'",
            recipe["key"],
            recipe["generation"],
        )
        if row["state"] not in {"present", "delete_intended", "absent"}:
            return False
        if effect is not None and (
            effect["operation_id"] != lease.operation_id
            or effect["descriptor"] != canonical({"reference": recipe["reference"]})
        ):
            raise OperationRefused("original network deletion intent differs")
        session, _ = await provider.session_for(operation, plan)
        await authorize()
        presence = await observe_native(
            session,
            plan.data["provider_account_id"],
            {plan.cluster_region, *plan.network["regions"]},
            row,
        )
        await authorize()
        if presence is not ResourcePresence.ABSENT:
            return False
        async with provider.execution_pool.acquire() as c, c.transaction():
            if not await lock_lease(c, lease):
                raise OperationRefused("cleanup observation fence expired")
            async with db.transaction():
                if effect is not None:
                    await db.execute(
                        "UPDATE controller_network_effects SET result=$4,confirmed_at=clock_timestamp() WHERE resource_key=$1 AND generation=$2 AND action='delete' AND operation_id=$3",
                        recipe["key"],
                        recipe["generation"],
                        lease.operation_id,
                        canonical({"absent": True}),
                    )
                # A retained exact native identity can already be absent before
                # a delete was dispatched. Record that observation atomically;
                # never manufacture a delete intent or claim this worker deleted it.
                await db.execute(
                    "UPDATE controller_network_resources SET state='absent' WHERE resource_key=$1 AND generation=$2",
                    recipe["key"],
                    recipe["generation"],
                )
                await db.execute(
                    "UPDATE controller_network_members SET released_at=clock_timestamp() WHERE resource_key=$1 AND allocation_id=$2 AND released_at IS NULL",
                    recipe["key"],
                    journal.allocation,
                )
                if not await lock_lease(c, lease):
                    raise OperationRefused("cleanup observation fence changed")
        return True
