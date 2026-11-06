"""Schedule-only repair mediation; never broadens ordinary service/broker access."""

from src.l5_service_identity import worker_id
from src.shared.identity.product_scope import _service_product_scope


def schedule_worker_id(actor):
    if actor.account_type != "service" or actor.auth_source != "iam" or actor.scope not in {"internal", "platform"}:
        return None
    for service in ("well_architected", "transform", "security"):
        worker = worker_id(service)
        if actor.agent_registry_id == worker:
            return worker
        if actor.agent_registry_id == worker.removesuffix("-worker") + "-repair" and "l5:schedule-admission" in actor.credential_scopes:
            return worker
    return None


async def schedule_product_scope(db, actor, delegation_id, product_id, project_id, operation):
    identity = schedule_worker_id(actor)
    if identity is None or operation not in {"product:read", "source:pin", "credential:session"}:
        return None
    grant = await _service_product_scope(db, actor, delegation_id, product_id, project_id, operation, identity)
    if grant is None or not isinstance(grant.execution_scope, dict) or grant.execution_scope.get("purpose") is not None:
        return None
    return grant
