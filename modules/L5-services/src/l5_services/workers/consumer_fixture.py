"""Finite consumer qualification through the installed worker's real IAM identity.

This handler never queues work, invokes a provider, invents an outbox row or
changes an admission limit. Configuration pins every existing contribution.
"""

import hashlib
import json
import os

import httpx

from l5_services.models.publication import Publication
from l5_services.models.run import Run
from l5_services.publication.installed import (
    InstalledPublicationClient,
    qualification_cases,
)
from l5_services.publication.recovery import (
    activate_wa_publications,
    repair_invalidation,
    repair_publication,
)

PROTOCOL = "l5-consumer-fixture-v1"
PHASES = {"admit", "replay", "invalidate", "late-replay"}


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def clients_for(service):
    prefix = {
        "security": "",
        "devops": "devops_",
        "finops": "finops_",
        "transform": "transform_",
        "well_architected": "well_architected_",
    }[service.service_id]
    result = []
    for name in (prefix + "context_client", prefix + "insights_client"):
        client = getattr(service, name, None)
        if (
            isinstance(client, InstalledPublicationClient)
            and client.qualification_cases
        ):
            if client.service_kind != service.service_id:
                raise ValueError("fixture consumer belongs to another installed worker")
            result.append(client)
    if not result:
        raise ValueError("no finite consumer qualification configured")
    return result


async def fixture_once(service, event):
    if (
        set(event) != {"l5_consumer_fixture", "phase", "cases_digest"}
        or type(event["l5_consumer_fixture"]) is not int
        or event["l5_consumer_fixture"] != 1
        or event["phase"] not in PHASES
    ):
        raise ValueError("unsupported finite consumer qualification request")
    cases = json.loads(os.environ.get("L5_CONSUMER_QUALIFICATION_CASES", "null"))
    qualification_cases(cases, service.service_id, "product-context")
    if (
        digest(cases) != event["cases_digest"]
        or os.environ.get("L5_SERVICE_ID") != service.service_id
    ):
        raise ValueError(
            "qualification selection differs from installed worker configuration"
        )
    clients = clients_for(service)
    installed = [case for client in clients for case in client.qualification_cases]
    if sorted(
        installed, key=lambda item: (item["destination"], item["contribution_id"])
    ) != sorted(cases, key=lambda item: (item["destination"], item["contribution_id"])):
        raise ValueError(
            "every configured case must belong to a finite installed client"
        )
    observations = []
    for client in clients:
        for case in client.qualification_cases:
            key = (case["scope"]["tenant_id"], case["contribution_id"])
            async with service.sessions() as db:
                publication = await db.get(Publication, key)
                if (
                    publication is None
                    or not client.can_invalidate(publication)
                    or digest(publication.payload) != case["contribution_hash"]
                ):
                    raise ValueError(
                        "qualification contribution is not the selected durable outbox row"
                    )
                run = await db.get(Run, (key[0], case["run_id"]))
                if run is None or run.service_id != service.service_id:
                    raise ValueError("qualification does not own the durable run")
                original_native = digest(
                    {
                        "run_id": run.run_id,
                        "native_job_ref": run.native_job_ref,
                        "provider_readback_ref": run.provider_readback_ref,
                    }
                )
                before_receipt = publication.receipt
                prior_watermark = publication.applied_watermark
            phase, denied_status = event["phase"], None
            if phase == "admit":
                if service.service_id == "well_architected":
                    await activate_wa_publications(service.sessions, client)
                await repair_publication(service.sessions, client, *key)
            elif phase == "replay":
                if publication.state != "published" or before_receipt is None:
                    raise ValueError("original durable admission must precede replay")
                if await client.replay_fixture(publication) != before_receipt:
                    raise ValueError("replay changed the original consumer receipt")
                if await client.read_receipt(publication) != before_receipt:
                    raise ValueError("readback changed the original consumer receipt")
            elif phase == "invalidate":
                if (
                    publication.state == "invalidation_pending"
                    and publication.invalidation_watermark > 0
                ):
                    # This negative request cannot mutate SQL or apply a newer
                    # watermark: Gateway compares it to the owed durable fence.
                    try:
                        await client.replay_fixture(
                            publication,
                            invalidation_body={
                                **publication.invalidation,
                                "invalidation_watermark": publication.invalidation_watermark
                                - 1,
                            },
                        )
                    except httpx.HTTPStatusError as error:
                        denied_status = error.response.status_code
                    if denied_status not in {403, 404, 409, 422}:
                        raise ValueError(
                            "consumer accepted a stale invalidation watermark"
                        )
                    await repair_invalidation(service.sessions, client, *key)
                else:
                    raise ValueError(
                        "source/grant revocation must first create the actual owed tombstone"
                    )
            else:
                if (
                    publication.state != "invalidated"
                    or publication.applied_watermark <= 0
                ):
                    raise ValueError(
                        "actual durable invalidation must precede late replay"
                    )
                try:
                    await client.replay_fixture(publication)
                except httpx.HTTPStatusError as error:
                    denied_status = error.response.status_code
                if denied_status not in {403, 404, 409, 410}:
                    raise ValueError("invalidated contribution accepted a late replay")
            async with service.sessions() as db:
                observed = await db.get(Publication, key)
                run = await db.get(Run, (key[0], case["run_id"]))
                after_native = digest(
                    {
                        "run_id": run.run_id,
                        "native_job_ref": run.native_job_ref,
                        "provider_readback_ref": run.provider_readback_ref,
                    }
                )
                if (
                    after_native != original_native
                    or observed.applied_watermark < prior_watermark
                ):
                    raise ValueError(
                        "qualification changed native work or regressed the invalidation fence"
                    )
                if phase in {"admit", "replay"} and (
                    observed.state != "published" or observed.receipt is None
                ):
                    raise ValueError("consumer has not completed durable admission")
                if phase in {"invalidate", "late-replay"} and (
                    observed.state != "invalidated"
                    or observed.invalidation_receipt is None
                ):
                    raise ValueError("consumer has not applied the durable tombstone")
                observations.append(
                    {
                        "contribution_id": observed.publication_id,
                        "contribution_hash": observed.contribution_hash,
                        "destination": observed.destination,
                        "state": observed.state,
                        "receipt": observed.receipt,
                        "invalidation_receipt": observed.invalidation_receipt,
                        "applied_watermark": observed.applied_watermark,
                        "native_fingerprint": after_native,
                        "denied_status": denied_status,
                    }
                )
    return {
        "protocol": PROTOCOL,
        "service_id": service.service_id,
        "phase": event["phase"],
        "cases_digest": event["cases_digest"],
        "observations": observations,
    }
