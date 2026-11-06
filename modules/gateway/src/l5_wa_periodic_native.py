"""Bounded independent native reads for an explicitly selected WA interval."""

import json
import os
from datetime import UTC, datetime
from hashlib import sha256

from starlette.concurrency import run_in_threadpool

from src.auth.l5_owner_delegation import _error
from src.l5_backend_well_architected import WellArchitectedService
from src.shared.models.vault import UserCredential


def native_snapshot(provider, identity, profile, generation_ids=()):
    client = provider.client("wellarchitected", region_name=identity["region"])
    observed = client.get_agent_profile(profileArn=profile)
    contexts = client.list_agent_contexts(profileArn=profile)
    if contexts.get("nextToken") or contexts.get("items") != []:
        raise ValueError("selected Profile is not source-only")
    if observed.get("profileArn", observed.get("arn")) != profile or observed.get("aggregationConfiguration") != []:
        raise ValueError("selected Profile identity or source-only scope changed")
    evidence = {
        "arn": profile,
        "aggregation": observed.get("aggregationConfiguration"),
        "role": observed.get("executionRoleArn"),
        "pillars": observed.get("pillars"),
        "contexts": [],
        "eligible": observed.get("eligibleForArchitectureGeneration"),
        "scheduled": observed.get("eligibleForScheduledGeneration"),
        "field_errors": observed.get("fieldErrors"),
    }
    profile_hash = sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
    generations, token, seen = [], None, set()
    for _ in range(10):
        response = client.list_agent_recommendation_generations(
            profileArn=profile, recommendationType="ARCHITECTURE", **({"nextToken": token} if token else {})
        )
        items = response.get("items")
        if not isinstance(items, list) or len(generations) + len(items) > 100:
            raise ValueError("selected Profile generation inventory exceeds its bound")
        for item in items:
            if item.get("profileArn") != profile or not isinstance(item.get("id"), str):
                raise ValueError("native inventory has an unattributable generation")
            generations.append({"id": item["id"], "name": item.get("name")})
        token = response.get("nextToken")
        if not token:
            break
        if token in seen:
            raise ValueError("native inventory pagination did not complete")
        seen.add(token)
    else:
        raise ValueError("native inventory pagination exceeds its bound")
    if len({item["id"] for item in generations}) != len(generations):
        raise ValueError("native generation inventory repeated identities")
    details = []
    for generation_id in generation_ids:
        detail = client.get_agent_recommendation_generation(profileArn=profile, generationId=generation_id)
        if detail.get("id") != generation_id or detail.get("profileArn") != profile or not detail.get("name"):
            raise ValueError("native generation Get identity changed")
        details.append({"id": generation_id, "profile_arn": profile, "name": detail["name"], "status": detail.get("status")})
    return {
        "source": "independent-native-read",
        "profile_arn": profile,
        "profile_hash": profile_hash,
        "architecture_eligible": observed.get("eligibleForArchitectureGeneration"),
        "native_scheduled_eligible": observed.get("eligibleForScheduledGeneration"),
        "aggregation_configuration": observed["aggregationConfiguration"],
        "contexts": [],
        "generations": sorted(generations, key=lambda item: item["id"]),
        "generation_details": details,
        "limitations": ["generation_list_absence_is_not_no_effect_proof"],
    }


def audit_snapshot(provider, identity, profile, start, end):
    """Filter before returning any data; never expose unrelated CloudTrail events."""
    client = provider.client("cloudtrail", region_name=identity["region"])
    events, token, seen = {}, None, set()
    for _ in range(20):
        response = client.lookup_events(
            LookupAttributes=[{"AttributeKey": "EventName", "AttributeValue": "StartAgentRecommendationGeneration"}],
            StartTime=start,
            EndTime=end,
            MaxResults=50,
            **({"NextToken": token} if token else {}),
        )
        items = response.get("Events")
        if not isinstance(items, list):
            raise ValueError("native audit page is invalid")
        for item in items:
            raw = item.get("CloudTrailEvent")
            if not isinstance(raw, str) or len(raw) > 65536:
                raise ValueError("native audit event exceeds its bound")
            event = json.loads(raw)
            actor = (event.get("userIdentity") or {}).get("sessionContext", {}).get("sessionIssuer", {})
            request = event.get("requestParameters") or {}
            selected_profile = request.get("profileArn", request.get("ProfileArn"))
            if (
                event.get("eventSource") != "wellarchitected.amazonaws.com"
                or event.get("eventName") != "StartAgentRecommendationGeneration"
                or event.get("recipientAccountId") != identity["account_id"]
                or event.get("awsRegion") != identity["region"]
                or actor.get("arn") != identity["role_arn"]
                or selected_profile != profile
            ):
                continue
            at = datetime.fromisoformat(event["eventTime"].replace("Z", "+00:00"))
            if at.tzinfo is None or not start <= at <= end:
                raise ValueError("selected native audit event is outside its interval")
            if not event.get("eventID") or not event.get("requestID"):
                raise ValueError("selected native audit event has no independent identity")
            selected = {
                "event_id": event["eventID"],
                "request_id": event["requestID"],
                "observed_at": at.astimezone(UTC).isoformat(),
                "error_code": event.get("errorCode"),
                "generation_id": (event.get("responseElements") or {}).get("id"),
                "name": request.get("name", request.get("Name")),
            }
            if selected["event_id"] in events and events[selected["event_id"]] != selected:
                raise ValueError("native audit identity changed across pages")
            events[selected["event_id"]] = selected
            if len(events) > 100:
                raise ValueError("selected native audit exceeds its bound")
        token = response.get("NextToken")
        if not token:
            break
        if token in seen:
            raise ValueError("native audit pagination did not complete")
        seen.add(token)
    else:
        raise ValueError("native audit pagination exceeds its bound")
    return {
        "source": "independent-cloudtrail-read",
        "observed_at": datetime.now(UTC).isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "events": sorted(events.values(), key=lambda item: (item["observed_at"], item["event_id"])),
        "pagination_complete": True,
        "delivery_complete": False,
        "limitations": ["cloudtrail_delivery_is_delayed", "absence_alone_is_not_no_effect_proof"],
    }


async def read_native(backend, scope, settings, *, interval=None, generation_ids=()):
    profile, identity = scope["native_resource_ref"]["record_id"], scope["provider_identity"]
    actions = [
        "wellarchitected:GetAgentProfile",
        "wellarchitected:ListAgentContexts",
        "wellarchitected:ListAgentRecommendationGenerations",
        "wellarchitected:GetAgentRecommendationGeneration",
    ]
    statement = {"Effect": "Allow", "Action": actions, "Resource": profile}
    policy = {"Version": "2012-10-17", "Statement": [statement]}
    if interval is not None:
        if os.environ.get("L5_WA_PERIODIC_AUDIT_READS_ENABLED") != "true":
            return {
                "source": "unavailable",
                "code": "periodic_native_audit_disabled",
                "next_action": "Enable the reviewed audit collector with existing selected-role CloudTrail access. This route grants no permissions.",
            }
        # LookupEvents does not support resource-level IAM. This temporary session
        # can only read this API in the selected Region; no policy is installed.
        policy["Statement"] = [
            {
                "Effect": "Allow",
                "Action": ["cloudtrail:LookupEvents"],
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": identity["region"]}},
            }
        ]
    credential = await backend.db.get(UserCredential, scope["credential_id"], populate_existing=True)
    try:
        provider, actual = await WellArchitectedService(backend).selected_provider(credential, settings, session_policy=policy)
        if actual != identity:
            raise ValueError("selected provider identity changed")
        if interval is not None:
            return await run_in_threadpool(audit_snapshot, provider, identity, profile, *interval)
        snapshot = await run_in_threadpool(native_snapshot, provider, identity, profile, generation_ids)
        if snapshot["profile_hash"] != scope["native_resource_ref"]["record_revision"]:
            raise ValueError("native Profile differs from its immutable binding snapshot")
        return snapshot
    except Exception as error:
        raise _error(
            409,
            "periodic_native_observation_unavailable",
            "Restore the selected connection's native/audit read access and retry observation. Preserve the original Start and access scope.",
        ) from error
