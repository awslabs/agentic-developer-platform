"""Bounded one-minute repair sweep; provider generation stays with run workers."""

from fastapi import HTTPException

from l5_services.adapters.well_architected.results import repair_wa_comparisons
from l5_services.dispatcher import repair_owed_work
from l5_services.events.recovery import repair_committed_events, repair_pending_events
from l5_services.events.schedules import receive_schedule_tick
from l5_services.events.sources import admit_source_receipt
from l5_services.events.periodic import produce_due_schedules
from l5_services.models.setup import SetupWork
from l5_services.publication.recovery import (
    activate_wa_publications,
    repair_due_invalidations,
    repair_due_publications,
)


async def receive_schedule_event(service, *, tenant_id, product_id, project_id, service_id,
                                 trigger_id, scheduled_at, now=None):
    now = now or service.clock()
    async with service.sessions() as session:
        receipt = await receive_schedule_tick(
            session, tenant_id=tenant_id, product_id=product_id, project_id=project_id,
            service_id=service_id, trigger_id=trigger_id, scheduled_at=scheduled_at, now=now,
        )
        if receipt.state == "rejected":
            return receipt
        if callable(getattr(service, "admit_event", None)):
            await service.admit_event(receipt)
            await session.refresh(receipt)
            if receipt.state != "admitted" or not receipt.run_id:
                raise HTTPException(503, detail={"code": "schedule_admission_unverified"})
            return receipt
        if not callable(getattr(service, "verify_event_target", None)):
            raise HTTPException(503, detail={"code": "work_authority_unavailable"})
        await admit_source_receipt(
            session, service.sessions, receipt, service.verify_event_target, service.publish,
            now=now, budget_guard=getattr(service, "budget_guard", None),
        )
        return receipt


async def repair_once(service, *, now=None, limit=50):
    now = now or service.clock()
    service_id = getattr(service, "service_id", None)
    await repair_committed_events(service.sessions, limit=limit, service_id=service_id)
    if callable(getattr(service, "admit_event", None)):
        await produce_due_schedules(service, now=now, limit=limit)
    await repair_pending_events(service, now=now, limit=limit)
    comparisons = (await repair_wa_comparisons(service, now=now, limit=limit)
                   if service_id in {None, "well_architected"} else 0)
    runs = comparisons + await repair_owed_work(service.sessions, service.publish, now=now, limit=limit, service_id=service_id)
    setups = await repair_owed_work(service.sessions, service.publish, now=now, limit=limit,
                                   work_model=SetupWork, service_id=service_id)
    context_client = getattr(service, "context_client", None)
    publications = (await repair_due_publications(service.sessions, context_client, now=now, limit=limit)
                    if context_client is not None else 0)
    invalidations = (await repair_due_invalidations(service.sessions, context_client, now=now, limit=limit)
                     if context_client is not None else 0)
    for destination, name in (("product-context", "devops_context_client"),
                              ("engineering-insights", "devops_insights_client")):
        client = getattr(service, name, None)
        if client is not None:
            if service_id != "devops" or not getattr(client, "devops", False) or client.destination != destination:
                raise ValueError("DevOps repair requires its service-owned qualified consumer client")
            publications += await repair_due_publications(service.sessions, client, now=now, limit=limit)
            invalidations += await repair_due_invalidations(service.sessions, client, now=now, limit=limit)
    for destination, name in (("product-context", "finops_context_client"),
                              ("engineering-insights", "finops_insights_client")):
        client = getattr(service, name, None)
        if client is not None:
            if not getattr(client, "finops", False) or client.destination != destination:
                raise ValueError("FinOps repair requires a matching qualified consumer client")
            publications += await repair_due_publications(service.sessions, client, now=now, limit=limit)
            invalidations += await repair_due_invalidations(service.sessions, client, now=now, limit=limit)
    for destination, name in (("product-context", "transform_context_client"),
                              ("engineering-insights", "transform_insights_client")):
        client = getattr(service, name, None)
        if client is not None:
            if not getattr(client, "transform", False) or client.destination != destination:
                raise ValueError("Transform repair requires a matching qualified consumer client")
            publications += await repair_due_publications(service.sessions, client, now=now, limit=limit)
            invalidations += await repair_due_invalidations(service.sessions, client, now=now, limit=limit)
    for destination, name in (("product-context", "well_architected_context_client"),
                              ("engineering-insights", "well_architected_insights_client")):
        client = getattr(service, name, None)
        if client is not None:
            if not getattr(client, "well_architected", False) or client.destination != destination:
                raise ValueError("WA repair requires a matching qualified consumer client")
            await activate_wa_publications(service.sessions, client, now=now, limit=limit)
            publications += await repair_due_publications(service.sessions, client, now=now, limit=limit)
            invalidations += await repair_due_invalidations(service.sessions, client, now=now, limit=limit)
    return runs + setups + publications + invalidations
