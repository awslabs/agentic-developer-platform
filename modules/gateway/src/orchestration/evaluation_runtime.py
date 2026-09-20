"""E2 reuses D3's authenticated release/runtime reader under evaluation authority."""

from dataclasses import replace
from datetime import UTC, datetime

from .deployment_controller import DeploymentServices
from .evaluation_plan import require
from .execution_policy import Action, ResourceRef, authorize_action


class EvaluationRuntime:
    def __init__(self, factory, *, deployments=None):
        self.factory = factory
        self.deployments = deployments or DeploymentServices(factory)

    async def authority(self, anchor, evaluation_address):
        services = self.deployments
        async with self.factory() as session:
            node, binding, merge, run_id, flow = await services.state(session, anchor)
            facts = await services.authority.authority_context(session, anchor, node, binding, run_id, Action.EVALUATE, delivery=True)
            auth = facts[-1]
            decision = authorize_action(
                auth,
                Action.EVALUATE,
                ResourceRef(repository_id=binding.repo, node_address=evaluation_address, org_id=node.org_id),
                anchor.identity.accepted_plan_version,
            )
            require(decision.permitted, "evaluation_authority_denied:" + (decision.reason.value if decision.reason else "unknown"))
            return node, binding, merge, flow, facts[2].policy, facts[3], auth

    async def verify(self, anchor, deployment, evaluation_address):
        services = self.deployments
        node, binding, merge, flow, policy, principal, auth = await self.authority(anchor, evaluation_address)
        require(not deployment.docs_only and deployment.delivery_complete, "evaluation_runtime_deployment_incomplete")
        async with self.factory() as session:
            rows = await services.actions(session, anchor)
        by_key = {row.operation_key: row for row in rows}
        require(set(deployment.workflow_operation_keys) <= set(by_key), "evaluation_runtime_workflow_missing")
        manifest = services.manifest_loader()
        observations = []
        for key in deployment.workflow_operation_keys:
            observation = await services.verify_workflow(
                anchor,
                by_key[key],
                node=node,
                binding=binding,
                merge=merge,
                flow=flow,
                policy=policy,
                principal=principal,
                auth=replace(auth, now=datetime.now(UTC)),
                components=tuple(c.component for c in deployment.components),
                manifest=manifest,
                action=Action.EVALUATE,
                resource_address=evaluation_address,
                revalidate=True,
            )
            require(observation.receipt.actual_revision == deployment.actual_revision, "evaluation_runtime_revision_changed")
            observations.append(observation.receipt)
        fresh = [c for receipt in observations for c in receipt.components]

        def signature(component):
            return (
                component.component,
                component.actual_revision,
                component.artifact_hash,
                component.image_digest or "",
                component.tick_digest or "",
                component.migration_head or "",
                component.asset_count,
            )

        require(sorted(map(signature, fresh)) == sorted(map(signature, deployment.components)), "evaluation_runtime_evidence_changed")
        return min(component.observed_at for component in fresh)
