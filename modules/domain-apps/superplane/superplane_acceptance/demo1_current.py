"""Bound current provider reads to immutable applied ownership, never cleanup authority."""

import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime

from .demo1_aws import AwsProviderReader, _resource
from .demo1_discovery import ProviderCensus, discovery_report
from .demo1_evidence import EvidenceError, fields, instant
from .demo1_ownership import observe_ownership, ownership_report
from .demo1_provider import InventoryQuery
from .demo1_plan_provider import (
    exact_state as plan_state,
    parse_resource as plan_resource,
)
from .demo1_report import reference
from .demo1_runtime import RuntimeReader


def observe_current_provider(
    selected,
    envelope,
    checkpoint,
    max_runtime_seconds,
    *,
    transport,
    runner=subprocess.run,
    clock=lambda: datetime.now(UTC),
    monotonic=time.monotonic,
):
    if (
        envelope.runtime_target is None
        or checkpoint is None
        or not checkpoint.submitted
    ):
        raise EvidenceError(
            "provider: selected runtime and original submitted checkpoint required"
        )
    started = clock()
    expires = monotonic() + min(max_runtime_seconds, envelope.max_runtime_seconds)

    def remaining():
        available = min(
            expires - monotonic(), (selected.deadline - clock()).total_seconds()
        )
        if clock() < selected.authorized_at or available <= 0:
            raise EvidenceError("provider: authorized read window exhausted")
        return available

    def bounded_run(command, **options):
        options["timeout"] = min(30, remaining())
        return runner(command, **options)

    runtime, ownership = observe_ownership(
        RuntimeReader(
            selected,
            envelope.runtime_target,
            runner=bounded_run,
            clock=clock,
            monotonic=monotonic,
        ),
        checkpoint,
        remaining(),
        now=started,
        transport=transport,
    )
    query = InventoryQuery(
        selected.connection_id,
        selected.role,
        selected.account,
        selected.region,
        checkpoint.workspace_id,
        tuple(ownership["owned_resources"]),
        tuple(dict.fromkeys((*selected.survivors, *ownership["preserved_resources"]))),
    )
    resources = query.expected_owned + query.expected_survivors
    if (
        not query.expected_survivors
        or len(resources) > 200
        or len(set(resources)) != len(resources)
    ):
        raise EvidenceError(
            "provider: bounded disjoint ownership and survivor identities required"
        )
    keys = []
    for resource in resources:
        try:
            _resource(resource, query)
        except EvidenceError:
            if (
                resource not in query.expected_survivors
                or plan_resource(resource, query)[0] != "key"
            ):
                raise
            keys.append(resource)
    census_query = replace(
        query,
        expected_survivors=tuple(
            arn for arn in query.expected_survivors if arn not in keys
        ),
    )
    report = {
        "runtime": runtime,
        "ownership": ownership_report(ownership),
        "provider": {
            "status": "BLOCKED",
            "inventory_complete": False,
            "cost_usd": None,
        },
        "checks": {
            "current_inventory": {
                "status": "BLOCKED",
                "detail": "provider reads not verified",
            },
            "survivors": {"status": "BLOCKED", "detail": "survivor reads not verified"},
            "cost": {
                "status": "BLOCKED",
                "detail": "cost unknown; no billing observation available",
            },
            "cleanup": {
                "status": "BLOCKED",
                "detail": "partial ownership inventory and unverified retirement cannot establish cleanup",
            },
        },
        "reason": "selected provider reads attempted; full inventory, cost and retirement acceptance remain unverified",
    }
    try:
        remaining()
        reader = AwsProviderReader(
            connection_id=selected.connection_id,
            broker_label=envelope.broker_label,
            account=selected.account,
            role_name=selected.role,
            region=selected.region,
            runner=bounded_run,
            clock=clock,
        )
        snapshot = reader.read_current_inventory(census_query)
        if len(snapshot["resource_states"]) == len(
            census_query.expected_owned + census_query.expected_survivors
        ) and all(
            state in {"present", "absent"}
            for state in snapshot["resource_states"].values()
        ):
            for key in keys:
                state = plan_state(reader, query, key)
                snapshot["resource_states"][key] = (
                    "incomplete" if state == "unavailable" else state
                )
                if state == "present":
                    snapshot["survivors_present"].append(key)
            snapshot["observed_at"] = clock().isoformat()
        remaining()
        _record_snapshot(report, snapshot, query, started, clock())
        if report["provider"].get("lookup_status") == "OBSERVED":
            census = ProviderCensus(
                reader, census_query, selected.org_id, snapshot["resource_states"]
            ).collect()
            remaining()
            report["provider"]["discovery"] = discovery_report(census)
            report["provider"]["reason"] = (
                "recorded lookups and scoped provider census read; full ownership coverage remains unverified"
            )
            report["checks"]["current_inventory"]["detail"] = (
                "recorded subset and current provider census read; durable full ownership and creation fence remain unverified"
            )
    except EvidenceError:
        report["provider"].pop("owned_absent_refs", None)
        report["provider"]["discovery"] = {
            "status": "BLOCKED",
            "listing_complete": False,
            "inventory_complete": False,
        }
        report["provider"]["reason"] = (
            "provider reads unavailable, invalid or outside the authorized window"
        )
    return report


def _record_snapshot(report, value, query, started, now):
    snapshot = fields(
        value,
        {
            "connection_id",
            "role",
            "account",
            "region",
            "workspace_id",
            "status",
            "owned_present",
            "survivors_present",
            "cost_usd",
            "observed_at",
            "resource_states",
        },
        "provider observation",
    )
    if (
        any(
            snapshot[key] != getattr(query, key)
            for key in ("connection_id", "role", "account", "region", "workspace_id")
        )
        or not started <= instant(snapshot["observed_at"], "provider time") <= now
        or snapshot["cost_usd"] is not None
        or snapshot["status"] not in ("complete", "incomplete", "denied")
    ):
        raise EvidenceError("provider: unverified observation binding")
    states = snapshot["resource_states"]
    expected = set(query.expected_owned + query.expected_survivors)
    if (
        not isinstance(states, dict)
        or not set(states) <= expected
        or any(
            state not in ("present", "absent", "denied", "incomplete")
            for state in states.values()
        )
    ):
        raise EvidenceError("provider: invalid resource observations")
    provider = report["provider"]
    provider["observed_at"] = snapshot["observed_at"]
    provider["artifact_ref"] = report["ownership"]["artifact_ref"]
    provider["workspace_ref"] = reference(query.workspace_id)
    if (
        snapshot["status"] == "denied"
        or set(states) != expected
        or any(state not in ("present", "absent") for state in states.values())
    ):
        provider["reason"] = (
            "provider inventory denied or incomplete; absence is not established"
        )
        return
    provider.update(
        lookup_status="OBSERVED",
        owned_present_refs=[
            reference(resource)
            for resource in query.expected_owned
            if states[resource] == "present"
        ],
        owned_absent_refs=[
            reference(resource)
            for resource in query.expected_owned
            if states[resource] == "absent"
        ],
        survivor_present_refs=[
            reference(resource)
            for resource in query.expected_survivors
            if states[resource] == "present"
        ],
        survivor_missing_refs=[
            reference(resource)
            for resource in query.expected_survivors
            if states[resource] == "absent"
        ],
        reason="only recorded resource identities were read; ownership coverage remains partial",
    )
    report["checks"]["current_inventory"]["detail"] = (
        "recorded subset read; remaining compute, storage and network ownership is unverified"
    )
    report["checks"]["survivors"] = {
        "status": "FAIL" if provider["survivor_missing_refs"] else "OBSERVED",
        "detail": "selected peer or preserved resource is absent"
        if provider["survivor_missing_refs"]
        else "selected peers and preserved resources are present; not full survivor acceptance",
    }
