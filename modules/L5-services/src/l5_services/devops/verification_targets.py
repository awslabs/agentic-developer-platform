"""Independently qualify a configured deployment and isolated test fixture."""

import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException


def verification_description(scope, verification, scenarios, *, run_id, attempt_id, token,
                             test_profile_ref=None):
    payload = {"run_id": run_id, "scope": scope,
               "deployment": verification["deployment_ref"], "source": verification["source_ref"],
               "target": verification["target_ref"], "credential_ref": verification["credential_ref"],
               "scenarios": scenarios, "scenario_specs": verification["scenario_specs"],
               "attempt_id": attempt_id, "idempotency_token": token}
    description = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if test_profile_ref is not None:
        # The documented ID-only envelope cannot enforce version consumption.
        # The selected revision remains in the nonempty, bounded requirement.
        payload["test_profile_ref"] = test_profile_ref
        payload["requirement"] = "Execute only the admitted scenarios and their bounded scenario_specs."
        description = json.dumps({
            "testProfileId": test_profile_ref["record_id"],
            "webhookEventMessage": json.dumps(payload, sort_keys=True, separators=(",", ":")),
        }, sort_keys=True, separators=(",", ":"))
    if len(description) > 10000:
        raise ValueError("verification description exceeds provider limit")
    return description


def _instant(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is not None:
            return instant.astimezone(UTC)
    except (AttributeError, TypeError, ValueError):
        pass
    return None


def verify_observation(options, scope, observation, *, now, deadline):
    """Use evidence from a server-configured reader, never a request-supplied claim."""
    references = ("source_ref", "deployment_ref", "target_ref", "credential_ref")
    if (not isinstance(observation, dict)
            or set(observation) != {*references, "deployed_sha", "isolated", "admin",
                                    "network_scope", "deployment_adapter", "checked_at", "expires_at",
                                    "scenario_specs"}
            or any(options[key] != observation.get(key) for key in references)
            or any((options[key]["tenant_id"], options[key]["project_id"]) !=
                   (scope["tenant_id"], scope["project_id"]) for key in references)
            or [options[key]["record_type"] for key in references] != [
                "repository_commit", "deployment", "verification_target", "secret"]
            or observation.get("deployed_sha") != options["source_ref"]["record_revision"]
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64}|sha256:[0-9a-f]{64})",
                            str(observation.get("deployed_sha"))) is None
            or observation.get("isolated") is not True
            or observation.get("admin") is not False
            or observation.get("network_scope") != options["target_ref"]["record_id"]
            or not observation.get("deployment_adapter")
            or not isinstance(observation.get("deployment_adapter"), str)
            or _instant(observation.get("checked_at")) is None
            or not now - timedelta(minutes=5) <= _instant(observation["checked_at"]) <= now
            or _instant(observation.get("expires_at")) is None
            or _instant(observation["expires_at"]) < deadline
            or not isinstance(observation.get("scenario_specs"), list)
            or len(observation["scenario_specs"]) != len(options["scenarios"])
            or any(not isinstance(spec, dict) or set(spec) != {
                "artifact", "max_requests", "timeout_seconds", "cleanup_ref", "network_scope",
                "positive_expected", "negative_expected"}
                or spec["artifact"] != artifact
                or type(spec["max_requests"]) is not int or not 1 <= spec["max_requests"] <= 20
                or type(spec["timeout_seconds"]) is not int or not 1 <= spec["timeout_seconds"] <= 300
                or spec["network_scope"] != options["target_ref"]["record_id"]
                or any(not isinstance(spec[key], str) or not 1 <= len(spec[key]) <= 128
                       for key in ("positive_expected", "negative_expected"))
                or spec["positive_expected"] == spec["negative_expected"]
                or not isinstance(spec["cleanup_ref"], dict)
                or not isinstance(spec["cleanup_ref"].get("source_connection_id"), str)
                or not isinstance(spec["cleanup_ref"].get("record_id"), str)
                or spec["cleanup_ref"].get("record_type") != "fixture_data"
                or (spec["cleanup_ref"].get("tenant_id"), spec["cleanup_ref"].get("project_id")) != (
                    scope["tenant_id"], scope["project_id"])
                for artifact, spec in zip(options["scenarios"], observation["scenario_specs"]))
            or len({(spec["cleanup_ref"]["source_connection_id"], spec["cleanup_ref"]["record_id"])
                    for spec in observation["scenario_specs"]}) != len(observation["scenario_specs"])
            or sum(spec["max_requests"] for spec in observation["scenario_specs"]) > 100
            or sum(spec["timeout_seconds"] for spec in observation["scenario_specs"]) > (
                deadline - now).total_seconds()
            or len(options["scenarios"]) > 20
            or any(scenario["media_type"] != "application/vnd.adp.release-scenario+json"
                   or scenario["bytes"] > 32768 for scenario in options["scenarios"])):
        code = "verification_target_unverified"
        if isinstance(observation, dict):
            if (observation.get("deployed_sha") != options["source_ref"]["record_revision"]
                    or _instant(observation.get("checked_at")) is None
                    or _instant(observation.get("checked_at")) < now - timedelta(minutes=5)
                    or _instant(observation.get("expires_at")) is None
                    or _instant(observation.get("expires_at")) < deadline):
                code = "verification_deployment_stale"
            elif observation.get("admin") is not False:
                code = "verification_fixture_privileged"
            elif (observation.get("target_ref") != options["target_ref"]
                  or observation.get("network_scope") != options["target_ref"]["record_id"]):
                code = "verification_target_out_of_scope"
        raise HTTPException(409, detail={"code": code})
    return observation


async def verify_target(options, scope, reader, *, now, deadline, clock=None):
    if (reader is None or not callable(getattr(reader, "read_deployment", None))
            or not callable(getattr(reader, "read_scenarios", None))):
        raise HTTPException(503, detail={"code": "deployment_evidence_adapter_unavailable"})
    observation = await reader.read_deployment(options["deployment_ref"], options["target_ref"])
    if not isinstance(observation, dict):
        raise HTTPException(409, detail={"code": "verification_target_unverified"})
    specs = await reader.read_scenarios(options["scenarios"])
    # A request-local producer observes after admission starts. Check freshness
    # at read completion while retaining the original bounded run deadline.
    return verify_observation(options, scope, {**observation, "scenario_specs": specs},
                              now=clock() if clock is not None else now, deadline=deadline)
