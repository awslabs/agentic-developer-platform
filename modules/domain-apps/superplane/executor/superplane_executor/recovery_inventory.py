"""Run the existing finalizer using only protected, claim-bound observations."""

from harness_jobs.identity import OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    InventoryAuthority,
    ResourceObservation,
    ResourcePresence,
)
from harness_jobs.leases import lock_lease

from .inventory import Finalizer
from .plan import Plan
from .recovery_authority import claim_identity


class RecoveryFinalizer:
    def __init__(self, provider, authority):
        self.provider, self.authority = provider, authority

    async def __call__(self, lease):
        finalizer = _ClaimFinalizer(self.provider, self.authority, lease)
        operation, _, _ = await finalizer.context(lease.operation_id)
        assessment = await Finalizer.__call__(finalizer, operation.grant, None)
        if assessment is None or finalizer.payload is None:
            raise OperationRefused("recovery inventory finalization incomplete")
        return finalizer.payload


class _ClaimFinalizer(Finalizer):
    def __init__(self, provider, transport, lease):
        self.provider, self.transport, self.lease = provider, transport, lease
        self.payload = None
        self.observations = {}
        self.authority = InventoryAuthority(
            connect=provider.execution_pool.acquire,
            authenticate=self.authenticate,
            query_provider=self.query,
        )

    async def authenticate(self, token):
        if token != self.lease.holder:
            raise OperationRefused("recovery claim selector mismatch")
        return (await self.transport.resolve_recovery(self.lease)).grant

    async def context(self, operation_id):
        if operation_id != self.lease.operation_id:
            raise OperationRefused("foreign recovery operation")
        operation = await self.transport.resolve_recovery(self.lease)
        if claim_identity(operation.grant.lease) != claim_identity(self.lease):
            raise OperationRefused("recovery claim changed")
        async with self.provider.domain_pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT w.id::text AS workspace_id,w.org_id::text AS domain_org_id,"
                "w.namespace_name AS namespace,c.id::text AS cluster_id,"
                "c.eks_cluster_arn AS cluster_arn,c.endpoint "
                "FROM workspaces w JOIN organizations o ON o.id=w.org_id "
                "JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id "
                "WHERE w.id::text=$1 AND o.id::text=$2",
                self.lease.workspace_id,
                self.lease.org_id,
            )
        if row is None:
            raise OperationRefused("original recovery target unavailable")
        target = dict(row)
        return operation, target, Plan.read(operation, target)

    async def discover(self, operation, target, plan, calls):
        allocation = operation.request.parameters["allocation_id"]
        snapshot = await self.transport.inventory(self.lease, allocation)
        resources, observations = {}, {}
        for item in snapshot:
            resource = AllocationResource(
                item["resource_id"],
                item["provider"],
                item["provider_reference"],
                item["kind"],
                frozenset(item["operation_keys"]),
            )
            if (
                resource.resource_id in observations
                or resource.provider_reference in resources
            ):
                raise OperationRefused("duplicate recovery inventory identity")
            resources[resource.provider_reference] = resource
            observations[resource.resource_id] = ResourceObservation(
                presence=ResourcePresence(item["presence"]),
                queried_by=item["provider_reference"],
                provider_state=item.get("provider_state"),
                detail=item.get("detail") or "",
            )
        known = await self.known(self.lease, allocation)
        if "controller_deployment_id" in operation.request.parameters and any(
            resource.kind == "workspace_object" and reference not in known
            for reference, resource in resources.items()
        ):
            raise OperationRefused("original workload UID evidence unavailable")
        # The provider must explicitly query retained handles, even after absence.
        # Missing a known member never turns an incomplete listing into release.
        for reference, resource in known.items():
            if reference in resources and resources[reference] != resource:
                raise OperationRefused("recovery inventory identity changed")
            resources.setdefault(reference, resource)
        await self.require_workspace_provenance(operation, resources)
        self.observations = observations
        return resources

    async def observe(self, operation, target, plan, resource):
        return self.observations.get(resource.resource_id) or ResourceObservation(
            presence=ResourcePresence.UNKNOWN,
            queried_by=resource.provider_reference,
            detail="retained resource was not observed",
        )

    async def query(self, lease, resources, query_id):
        if claim_identity(lease) != claim_identity(self.lease):
            raise OperationRefused("inventory claim mismatch")
        operation, target, plan = await self.context(lease.operation_id)
        observed = await self.discover(operation, target, plan, [])
        if set(observed.values()) != set(resources):
            raise OperationRefused("allocation membership changed after inventory seal")
        return {
            resource.resource_id: await self.observe(operation, target, plan, resource)
            for resource in resources
        }

    def token_for(self, operation):
        # Only a local selector: authenticate always verifies the actual claim/run.
        return self.lease.holder

    async def persist(self, operation, target, calls, assessment=None):
        if assessment is None:
            return
        if not assessment.inventory or not assessment.inventory.complete:
            raise OperationRefused("complete recovery inventory unavailable")
        self.payload = self.accounting(operation, calls, assessment)
        # The same finalizer retires capacity only after fresh authoritative absence.
        # The immutable receipt is subsequently committed with shared settlement;
        # the domain projection itself can never authorize ledger delivery.
        async with self.provider.execution_pool.acquire() as connection:
            async with connection.transaction():
                if not await lock_lease(connection, self.lease):
                    raise OperationRefused("recovery claim expired before retirement")
                await self._persist_observation(
                    operation, target, self.payload, assessment
                )
