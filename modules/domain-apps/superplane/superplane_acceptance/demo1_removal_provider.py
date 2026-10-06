"""Independent bounded provider baseline and post-retirement comparison.

The private baseline is observational evidence, never deletion authority. Complete
means the named exact IDs and supported tagged/owned-VPC census were read; it
never means a global cloud inventory or a billing observation was established.
"""

import re
import subprocess
import time
from dataclasses import replace
from datetime import UTC, datetime

from workspace_provisioning.artifacts import digest as document_digest

from .demo1_aws import AWS_ARN, NOT_FOUND, RESOURCE_KINDS, AwsProviderReader, _resource
from .demo1_discovery import COLLECTIONS, MAX_RESOURCES, ProviderCensus
from .demo1_evidence import EvidenceError, digest, identifier, instant
from .demo1_provider import InventoryQuery
from .demo1_plan_provider import (
    capture_plan,
    exact_state as plan_state,
    parse_resource as plan_resource,
    validate_plan,
)
from .demo1_report import reference

SCOPE = "recorded identities plus maintained dedicated workspace plan, tagged EC2 and owned-VPC census"
VERSION = "demo1-provider-baseline-v1"
EXTRA = {
    "snapshot": (
        "snapshots",
        "--snapshot-ids",
        "InvalidSnapshot.NotFound",
        "DescribeSnapshots",
    ),
    "elastic-ip": (
        "addresses",
        "--allocation-ids",
        "InvalidAllocationID.NotFound",
        "DescribeAddresses",
    ),
    "natgateway": (
        "nat-gateways",
        "--nat-gateway-ids",
        "NatGatewayNotFound",
        "DescribeNatGateways",
    ),
    "route-table": (
        "route-tables",
        "--route-table-ids",
        "InvalidRouteTableID.NotFound",
        "DescribeRouteTables",
    ),
    "internet-gateway": (
        "internet-gateways",
        "--internet-gateway-ids",
        "InvalidInternetGatewayID.NotFound",
        "DescribeInternetGateways",
    ),
    "vpc-endpoint": (
        "vpc-endpoints",
        "--vpc-endpoint-ids",
        "InvalidVpcEndpointId.NotFound",
        "DescribeVpcEndpoints",
    ),
    "launch-template": (
        "launch-templates",
        "--launch-template-ids",
        "InvalidLaunchTemplateId.NotFound",
        "DescribeLaunchTemplates",
    ),
}


def require(condition):
    if not condition:
        raise EvidenceError(
            "provider removal: incomplete, changed or unavailable evidence"
        )


def binding(selected, envelope, checkpoint):
    require(checkpoint is not None and checkpoint.submitted is True)
    require(
        checkpoint.request_id == selected.request_id
        and checkpoint.plan_revision == selected.plan_revision
    )
    identifier(checkpoint.workspace_id, "provider workspace")
    identifier(checkpoint.retirement_request_id, "provider retirement request")
    return {
        **{
            key: getattr(selected, key)
            for key in (
                "release_source",
                "image_digest",
                "schema_revision",
                "connection_id",
                "role",
                "account",
                "region",
                "org_id",
                "request_id",
                "plan_revision",
            )
        },
        "workspace_id": checkpoint.workspace_id,
        "retirement_request_id": checkpoint.retirement_request_id,
        "broker_label": envelope.broker_label,
        "authority_ref": envelope.authority_ref,
        "authorized_at": selected.authorized_at.isoformat(),
        "deadline": selected.deadline.isoformat(),
    }


def query_for(selected, checkpoint, owned, survivors):
    require(
        isinstance(owned, list) and isinstance(survivors, list) and owned and survivors
    )
    resources = owned + survivors
    require(
        len(resources) <= MAX_RESOURCES and all(isinstance(v, str) for v in resources)
    )
    require(len(resources) == len(set(resources)))
    query = InventoryQuery(
        selected.connection_id,
        selected.role,
        selected.account,
        selected.region,
        checkpoint.workspace_id,
        tuple(owned),
        tuple(survivors),
    )
    for arn in owned:
        _resource(arn, query)
    for arn in survivors:
        try:
            _resource(arn, query)
        except EvidenceError:
            require(plan_resource(arn, query)[0] == "key")
    require(len({arn.split(":")[1] for arn in resources}) == 1)
    require(sum(":vpc/" in arn for arn in owned) == 1)
    require(sum(":eks:" in arn and ":cluster/" in arn for arn in owned) == 1)
    require(any(":cluster/" in arn or ":instance/" in arn for arn in survivors))
    return query


def bounded_reader(selected, envelope, max_runtime_seconds, runner, clock, monotonic):
    require(type(max_runtime_seconds) is int and max_runtime_seconds > 0)
    require(
        type(envelope.max_runtime_seconds) is int and envelope.max_runtime_seconds > 0
    )
    end = monotonic() + min(max_runtime_seconds, envelope.max_runtime_seconds)

    def remaining():
        now = clock()
        require(selected.authorized_at <= now < selected.deadline)
        seconds = min(end - monotonic(), (selected.deadline - now).total_seconds())
        require(seconds > 0)
        return seconds

    def bounded_run(command, **options):
        options["timeout"] = min(30, remaining())
        return runner(command, **options)

    remaining()
    return AwsProviderReader(
        connection_id=selected.connection_id,
        broker_label=envelope.broker_label,
        account=selected.account,
        role_name=selected.role,
        region=selected.region,
        runner=bounded_run,
        clock=clock,
    ), remaining


def exact_resource(arn, query):
    require(isinstance(arn, str) and len(arn) <= 1024)
    match = AWS_ARN.fullmatch(arn)
    if match is None:
        kind, name = plan_resource(arn, query)
        return kind, name, ("plan",)
    require(
        match is not None and arn.split(":")[1] == query.expected_owned[0].split(":")[1]
    )
    try:
        return (*_resource(arn, query), None)
    except EvidenceError:
        require(
            match["service"] == "ec2"
            and match["region"] == query.region
            and match["account"] == query.account
            and match["kind"] in EXTRA
        )
        kind, identity = match["kind"], match["name"]
        operation, option, error, api = EXTRA[kind]
        collection, field, _, prefix, _ = COLLECTIONS[operation]
        require(re.fullmatch(prefix + r"-[0-9a-f]{8}(?:[0-9a-f]{9})?", identity))
        return kind, identity, (operation, option, error, api, collection, field)


def exact_states(reader, query, resources):
    """Read every immutable ID, including tombstones, without trusting tags."""
    require(isinstance(resources, list) and 0 < len(resources) <= MAX_RESOURCES)
    require(
        all(isinstance(value, str) for value in resources)
        and len(resources) == len(set(resources))
    )
    states = {}
    for arn in resources:
        kind, identity, descriptor = exact_resource(arn, query)
        if descriptor == ("plan",):
            states[arn] = plan_state(reader, query, arn)
            continue
        require(reader._identity())
        if descriptor is not None:
            operation, option, error, api, collection, field = descriptor
            service, operation, nested = "ec2", "describe-" + operation, None
        elif kind == "cluster":
            service, operation, option = "eks", "describe-cluster", "--name"
            error, api = NOT_FOUND[kind]
        else:
            service, operation, option, field, collection, nested = RESOURCE_KINDS[kind]
            error, api = NOT_FOUND[kind]
        code, value, message = reader._execute(
            service,
            operation,
            option,
            identity,
            "--region",
            query.region,
            "--no-paginate",
        )
        if code:
            found = re.fullmatch(
                rf"\s*An error occurred \({re.escape(error)}\) when calling the {api} operation: ([^\r\n]+)\s*",
                message,
            )
            require(
                code in (254, 255)
                and found
                and re.search(
                    rf"(?<![A-Za-z0-9_-]){re.escape(identity)}(?![A-Za-z0-9_-])",
                    found[1],
                )
            )
            states[arn] = "absent"
            continue
        require(
            isinstance(value, dict)
            and not any(
                value.get(k) for k in ("NextToken", "nextToken", "PaginationToken")
            )
        )
        if kind == "cluster":
            row = value.get("cluster")
            require(isinstance(row, dict) and row.get("arn") == arn)
        else:
            rows = value.get(collection)
            if kind in {"natgateway", "vpc-endpoint", "launch-template"} and rows == []:
                states[arn] = "absent"
                continue
            if nested:
                require(
                    isinstance(rows, list)
                    and len(rows) == 1
                    and isinstance(rows[0], dict)
                    and rows[0].get("OwnerId") == query.account
                )
                rows = rows[0].get(nested)
            require(
                isinstance(rows, list)
                and len(rows) == 1
                and isinstance(rows[0], dict)
                and rows[0].get(field) == identity
            )
            row = rows[0]
            require("OwnerId" not in row or row["OwnerId"] == query.account)
        state = "present"
        if kind == "instance":
            require(
                isinstance(row.get("State"), dict)
                and row["State"].get("Name")
                in {
                    "pending",
                    "running",
                    "stopping",
                    "stopped",
                    "shutting-down",
                    "terminated",
                }
            )
            if row["State"]["Name"] == "terminated":
                state = "terminal"
        elif kind == "natgateway":
            require(
                row.get("State")
                in {"pending", "failed", "available", "deleting", "deleted"}
            )
            if row["State"] == "deleted":
                state = "terminal"
        states[arn] = state
    return states


def validate_census(value, query, started, observed, allowed_unresolved=()):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "status",
            "listing_complete",
            "inventory_complete",
            "observed_at",
            "resources",
            "unresolved_resources",
            "owned_vpc_scanned",
        }
    )
    require(
        value["status"] == "OBSERVED"
        and value["listing_complete"] is True
        and value["inventory_complete"] is False
        and value["owned_vpc_scanned"] is True
        and isinstance(value["unresolved_resources"], list)
        and len(value["unresolved_resources"]) <= MAX_RESOURCES
        and all(isinstance(v, str) for v in value["unresolved_resources"])
        and {
            arn.removesuffix(":*") if ":logs:" in arn and ":log-group:" in arn else arn
            for arn in value["unresolved_resources"]
        }
        <= set(allowed_unresolved)
    )
    require(
        started <= instant(value["observed_at"], "baseline census time") <= observed
    )
    require(
        isinstance(value["resources"], dict)
        and len(value["resources"]) <= MAX_RESOURCES
    )
    for arn, row in value["resources"].items():
        kind, _, _ = exact_resource(arn, query)
        require(
            kind != "cluster"
            and isinstance(row, dict)
            and set(row) == {"kind", "presence", "basis"}
            and row["kind"] == kind
            and isinstance(row["basis"], list)
            and 0 < len(row["basis"]) <= 3
            and all(isinstance(b, str) for b in row["basis"])
            and row["basis"] == sorted(set(row["basis"]))
            and set(row["basis"])
            <= {"owned-vpc", "workspace-tags", "provider-attachment"}
        )
        require(
            row["presence"]
            in ({"present", "terminated"} if kind == "instance" else {"present"})
        )
    require(not set(value["resources"]) & set(query.expected_survivors))


def census(reader, query, selected, started, now, allowed_unresolved=()):
    # The maintained EC2 census parses only EKS/EC2 peers. Retained KMS identities
    # are independently exact-read and may appear as tagged unresolved entries.
    census_query = replace(
        query,
        expected_survivors=tuple(
            arn for arn in query.expected_survivors if ":kms:" not in arn
        ),
    )
    value = ProviderCensus(reader, census_query, selected.org_id).collect()
    validate_census(value, query, started, now(), allowed_unresolved)
    return value


def capture_provider_baseline(
    selected,
    envelope,
    checkpoint,
    ownership,
    max_runtime_seconds,
    *,
    runner=subprocess.run,
    clock=lambda: datetime.now(UTC),
    monotonic=time.monotonic,
):
    """Capture before retirement approval/submission; caller persists it privately."""
    scope = binding(selected, envelope, checkpoint)
    started = clock()
    require(
        isinstance(ownership, dict)
        and type(ownership.get("version")) is int
        and ownership["version"] == 1
        and ownership.get("status") == "OBSERVED"
        and ownership.get("inventory_complete") is False
    )
    require(
        all(
            ownership.get(key) == scope[key]
            for key in (
                "org_id",
                "workspace_id",
                "request_id",
                "plan_revision",
                "region",
            )
        )
    )
    require(ownership.get("account_id") == selected.account)
    digest(ownership.get("artifact_id"), "provider ownership artifact")
    identifier(ownership.get("current_operation_id"), "provider bootstrap operation")
    require(
        selected.authorized_at
        <= instant(ownership["recorded_at"], "provider ownership time")
        <= started
    )
    owned = ownership.get("owned_resources")
    preserved = ownership.get("preserved_resources")
    require(
        isinstance(preserved, list)
        and len(preserved) <= MAX_RESOURCES
        and all(isinstance(value, str) for value in preserved)
    )
    survivors = list(dict.fromkeys((*selected.survivors, *preserved)))
    query = query_for(selected, checkpoint, owned, survivors)
    reader, remaining = bounded_reader(
        selected, envelope, max_runtime_seconds, runner, clock, monotonic
    )
    initial = exact_states(
        reader, query, list(query.expected_owned + query.expected_survivors)
    )
    require(all(state == "present" for state in initial.values()))
    plan = capture_plan(reader, query, selected)
    before = census(
        reader,
        query,
        selected,
        started,
        clock,
        set(plan["resources"]) | set(query.expected_survivors),
    )
    require(
        any(
            row["kind"] == "instance" and row["presence"] == "present"
            for row in before["resources"].values()
        )
    )
    require(any(row["kind"] == "volume" for row in before["resources"].values()))
    expected = sorted(
        set(query.expected_owned) | set(before["resources"]) | set(plan["resources"])
    )
    require(not set(expected) & set(query.expected_survivors))
    observed = exact_states(reader, query, expected + survivors)
    require(
        all(
            state == "present"
            for arn, state in observed.items()
            if arn in query.expected_owned or arn in survivors
        )
    )
    require(
        all(
            state in {"present", "terminal"}
            or (arn in plan["optional"] and state == "absent")
            for arn, state in observed.items()
        )
    )
    require(
        any(
            ":instance/" in arn and state == "present"
            for arn, state in observed.items()
            if arn in expected
        )
    )
    remaining()
    baseline = {
        "version": VERSION,
        "scope": scope,
        "ownership_artifact_id": ownership["artifact_id"],
        "source_operation_id": ownership["current_operation_id"],
        "recorded_owned": list(query.expected_owned),
        "survivors": survivors,
        "expected_removed": expected,
        "census": before,
        "maintained_plan": plan,
        "resource_states": observed,
        "started_at": started.isoformat(),
        "observed_at": clock().isoformat(),
        "inventory_scope": SCOPE,
        "full_inventory_complete": False,
    }
    result = {**baseline, "baseline_sha256": document_digest(baseline)}
    validate_provider_baseline(selected, envelope, checkpoint, result, clock=clock)
    return result


def validate_baseline(selected, envelope, checkpoint, baseline, now):
    require(
        isinstance(baseline, dict)
        and set(baseline)
        == {
            "version",
            "scope",
            "ownership_artifact_id",
            "source_operation_id",
            "recorded_owned",
            "survivors",
            "expected_removed",
            "census",
            "maintained_plan",
            "resource_states",
            "started_at",
            "observed_at",
            "inventory_scope",
            "full_inventory_complete",
            "baseline_sha256",
        }
    )
    require(
        baseline["version"] == VERSION
        and baseline["scope"] == binding(selected, envelope, checkpoint)
        and baseline["inventory_scope"] == SCOPE
        and baseline["full_inventory_complete"] is False
    )
    require(
        document_digest({k: v for k, v in baseline.items() if k != "baseline_sha256"})
        == digest(baseline["baseline_sha256"], "provider baseline digest")
    )
    digest(baseline["ownership_artifact_id"], "provider ownership artifact")
    identifier(baseline["source_operation_id"], "provider bootstrap operation")
    started, observed = (
        instant(baseline[key], "provider baseline time")
        for key in ("started_at", "observed_at")
    )
    require(selected.authorized_at <= started <= observed <= now < selected.deadline)
    query = query_for(
        selected, checkpoint, baseline["recorded_owned"], baseline["survivors"]
    )
    require(set(selected.survivors) <= set(query.expected_survivors))
    before = baseline["census"]
    plan = baseline["maintained_plan"]
    validate_plan(plan, query)
    validate_census(
        before,
        query,
        started,
        observed,
        set(plan["resources"]) | set(query.expected_survivors),
    )
    require(
        any(
            row["kind"] == "instance" and row["presence"] == "present"
            for row in before["resources"].values()
        )
    )
    require(any(row["kind"] == "volume" for row in before["resources"].values()))
    expected = baseline["expected_removed"]
    require(
        isinstance(expected, list)
        and len(expected) <= MAX_RESOURCES
        and all(isinstance(v, str) for v in expected)
        and expected
        == sorted(
            set(query.expected_owned)
            | set(before["resources"])
            | set(plan["resources"])
        )
        and len(expected) + len(query.expected_survivors) <= MAX_RESOURCES
        and not set(expected) & set(query.expected_survivors)
    )
    states = baseline["resource_states"]
    require(
        isinstance(states, dict)
        and set(states) == set(expected) | set(query.expected_survivors)
    )
    for arn, state in states.items():
        kind, _, _ = exact_resource(arn, query)
        allowed = (
            {"present", "terminal"}
            if kind in {"instance", "natgateway"} and arn in expected
            else {"present"}
        )
        if arn in plan["optional"]:
            allowed.add("absent")
        require(state in allowed)
        if arn in query.expected_owned or arn in query.expected_survivors:
            require(state == "present")
    require(any(":instance/" in arn and states[arn] == "present" for arn in expected))
    return query


def validate_provider_baseline(
    selected, envelope, checkpoint, baseline, *, clock=lambda: datetime.now(UTC)
):
    """Validate a saved private baseline without contacting a provider."""
    try:
        return validate_baseline(selected, envelope, checkpoint, baseline, clock())
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceError("provider removal: invalid saved baseline") from exc


def verify_provider_removal(
    selected,
    envelope,
    checkpoint,
    baseline,
    max_runtime_seconds,
    *,
    runner=subprocess.run,
    clock=lambda: datetime.now(UTC),
    monotonic=time.monotonic,
):
    """Report exact observed scope; caller also requires authenticated removal success."""
    report = {
        "status": "BLOCKED",
        "inventory_scope": SCOPE,
        "inventory_complete": False,
        "full_inventory_complete": False,
        "cost_usd": None,
        "checks": {
            "owned_absence": {"status": "BLOCKED"},
            "survivors": {"status": "BLOCKED"},
        },
    }
    try:
        started = clock()
        query = validate_provider_baseline(
            selected, envelope, checkpoint, baseline, clock=clock
        )
        reader, remaining = bounded_reader(
            selected, envelope, max_runtime_seconds, runner, clock, monotonic
        )
        after = census(
            reader,
            query,
            selected,
            started,
            clock,
            set(baseline["maintained_plan"]["resources"])
            | set(query.expected_survivors),
        )
        expected, survivors = baseline["expected_removed"], baseline["survivors"]
        # Changing tags or detaching resources cannot hide recorded identities.
        # New resources cannot be accepted by refreshing the saved baseline.
        require(set(after["resources"]) <= set(expected))
        states = exact_states(reader, query, expected + survivors)
        remaining()
        absent = all(states[arn] in {"absent", "terminal"} for arn in expected)
        preserved = all(states[arn] == "present" for arn in survivors)
        completed = clock()
        require(started <= completed < selected.deadline)
        report.update(
            status="OBSERVED",
            inventory_complete=True,
            observed_at=completed.isoformat(),
            baseline_ref=reference(baseline["baseline_sha256"]),
            workspace_ref=reference(checkpoint.workspace_id),
            absent_refs=[reference(arn) for arn in expected if states[arn] == "absent"],
            terminal_refs=[
                reference(arn) for arn in expected if states[arn] == "terminal"
            ],
            remaining_refs=[
                reference(arn) for arn in expected if states[arn] == "present"
            ],
            missing_survivor_refs=[
                reference(arn) for arn in survivors if states[arn] != "present"
            ],
        )
        report["checks"] = {
            "owned_absence": {"status": "PASS" if absent else "FAIL"},
            "survivors": {"status": "PASS" if preserved else "FAIL"},
        }
    except (EvidenceError, KeyError, TypeError, ValueError):
        report["reason"] = (
            "provider baseline or current reads incomplete, changed, denied or outside the authorized window"
        )
    return report
