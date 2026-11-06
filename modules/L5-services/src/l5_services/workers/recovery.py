"""Scheduled Lambda entry point for owed SQL work."""

import asyncio
import os

from l5_services.workers.execution import load_service
from l5_services.workers.relay import repair_once


def lambda_handler(event, context, *, service=None):
    from l5_services.workers.database_probe import REQUEST_KEY, run_probe

    if isinstance(event, dict) and REQUEST_KEY in event:
        return asyncio.run(run_probe(event))
    service = service or load_service()
    if event == {"l5_runtime_probe": 1}:
        result = {"service_id": service.service_id}
        if os.environ.get("L5_CONSUMER_QUALIFICATION_CASES"):
            from l5_services.workers.consumer_fixture import PROTOCOL

            result["consumer_fixture_protocol"] = PROTOCOL
        return result
    if isinstance(event, dict) and "l5_consumer_fixture" in event:
        from l5_services.workers.consumer_fixture import fixture_once

        return asyncio.run(fixture_once(service, event))
    return {"enqueued": asyncio.run(repair_once(service))}
