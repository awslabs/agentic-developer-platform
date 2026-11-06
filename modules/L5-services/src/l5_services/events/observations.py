"""Retain event arrivals and decisions without replacing original receipt facts."""

from uuid import uuid4

from l5_services.models.schedule import EventObservation


def observe(session, receipt, kind, now, *, reason=None):
    if receipt.service_id != "well_architected" or receipt.event_kind != "schedule":
        return
    session.add(EventObservation(
        observation_id=str(uuid4()), tenant_id=receipt.tenant_id,
        idempotency_key=receipt.idempotency_key, observed_at=now, kind=kind,
        state=receipt.state, reason=reason or receipt.reason, run_id=receipt.run_id,
    ))
