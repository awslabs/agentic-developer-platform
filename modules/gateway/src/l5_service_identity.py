"""Map L5 service ownership to the installed worker registry identity."""

WORKER_IDS = {
    "security": "l5-worker",
    "devops": "l5-devops-worker",
    "finops": "l5-finops-worker",
    "transform": "l5-transform-worker",
    "well_architected": "l5-well-architected-worker",
}


def worker_id(service_id: str) -> str:
    return WORKER_IDS[service_id]


def service_for_worker(identity: str) -> str | None:
    return next((service_id for service_id, worker in WORKER_IDS.items() if worker == identity), None)


def contribution_worker_id(actor) -> str | None:
    """Repair may only reuse a producer grant inside contribution mediation.

    Keep service_for_worker and general credential/Product authorization unchanged:
    a repair registry identity is never a provider or native execution identity.
    """
    if actor.account_type != "service" or actor.auth_source != "iam" or actor.scope not in {"internal", "platform"}:
        return None
    if actor.agent_registry_id in WORKER_IDS.values():
        return actor.agent_registry_id
    if "l5:contribution-repair" in actor.credential_scopes:
        return next((worker for worker in WORKER_IDS.values() if actor.agent_registry_id == worker.removesuffix("-worker") + "-repair"), None)
    return None
