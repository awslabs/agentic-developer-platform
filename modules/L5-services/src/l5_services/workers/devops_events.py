"""EventBridge Lambda entry for native lifecycle hints."""

import asyncio

from l5_services.devops.native_events import ingest_native_event
from l5_services.workers.execution import load_service


def lambda_handler(event, context, *, service=None):
    from l5_services.workers.database_probe import REQUEST_KEY, run_probe

    if isinstance(event, dict) and REQUEST_KEY in event:
        return asyncio.run(run_probe(event))
    if event == {"l5_runtime_probe": 1}:
        return {"service_id": "devops"}
    service = service or load_service()
    run = asyncio.run(ingest_native_event(service.sessions, event, now=service.clock()))
    return {"run_id": run.run_id}
