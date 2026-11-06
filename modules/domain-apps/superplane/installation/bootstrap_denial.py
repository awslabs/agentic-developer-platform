"""Owner observation of the exact AWS denial before any bootstrap grant existed.

Protected journal/expiry readers are installation service dependencies. Caller
JSON cannot supply an absence assertion or replace those live readers. Collection
is read-only; native cleanup and immutable publication are separate steps.
"""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path

from .bootstrap_recovery import collect, image_sources
from .provider_authority import require


def denial_contract():
    path = (
        Path(__file__).resolve().parents[4]
        / "modules/gateway/src/shared/domain_bootstrap_denial.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_denial_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_events(events, identity, authority, *, acquired_at, stopped_at):
    """CloudTrail's AccessDenied is positive evidence; missing events are not."""
    spec = identity["first_grant"]
    role = authority["role_arn"]
    session_name = "adp-superplane-operation-" + identity["operation_id"][:8]
    principal = (
        role.replace(":iam:", ":sts:").replace(":role/", ":assumed-role/")
        + "/"
        + session_name
    )
    expected = {
        "name": identity["cluster_arn"].split("/")[-1],
        "principalArn": spec["principal_arn"],
        "type": "STANDARD",
        "kubernetesGroups": spec["groups"],
        "username": spec["username"],
        "clientRequestToken": spec["client_token"],
        "tags": {
            "superplane-generation": identity["generation"],
            "OrgId": identity["org_id"],
            "WorkspaceId": identity["workspace_id"],
        },
    }
    denied = []
    for event in events:
        require(
            acquired_at
            <= datetime.fromisoformat(event["eventTime"].replace("Z", "+00:00"))
            <= stopped_at
        )
        # The session name is shared by successive attempts; the protected lease
        # acquisition window excludes previous attempts without discarding any
        # event in this attempt. Unknown writers are refused, not ignored.
        user = event.get("userIdentity", {})
        if event.get("readOnly") is True:
            continue
        require(
            event.get("eventSource") == "eks.amazonaws.com"
            and event.get("eventName") == "CreateAccessEntry"
        )
        require(
            user.get("arn") == principal
            and user.get("sessionContext", {}).get("sessionIssuer", {}).get("arn")
            == role
        )
        require(
            event.get("recipientAccountId") == authority["account_id"]
            and event.get("awsRegion") == authority["region"]
        )
        require(
            event.get("errorCode") == "AccessDenied"
            and event.get("requestParameters") == expected
        )
        require(set(event.get("responseElements") or {}) <= {"message"})
        require(event.get("eventID") and event.get("requestID"))
        denied.append(event)
    require(len(denied) == 1)
    return denied[0]


def verify_absence(eks, plan, *, cluster_name):
    """Read all access entries, plus explicit absence of every planned actor."""
    from superplane_bootstrap.eks_grants import _absent, _pages

    specs = [g for g in plan["grants"] if g["kind"] == "eks-entry"]
    require(
        len(specs) == 3
        and {g["actor"] for g in specs} == {"registrar", "installer", "supervisor"}
    )
    for spec in specs:
        try:
            eks.describe_access_entry(
                clusterName=cluster_name, principalArn=spec["principal_arn"]
            )
        except Exception as error:
            if not _absent(error):
                raise
        else:
            raise ValueError("a planned bootstrap actor still has access")
    groups = {group for spec in specs for group in spec["groups"]}
    entries = []
    for principal in _pages(
        eks, "list_access_entries", "accessEntries", clusterName=cluster_name
    ):
        entry = eks.describe_access_entry(
            clusterName=cluster_name, principalArn=principal
        ).get("accessEntry")
        require(isinstance(entry, dict) and entry.get("principalArn") == principal)
        require(
            isinstance(entry.get("kubernetesGroups", []), list)
            and not groups.intersection(entry.get("kubernetesGroups", []))
        )
        entries.append(entry)
    # Provider timestamps are normalized only for hashing; no credential material.
    return json.loads(
        json.dumps(sorted(entries, key=lambda x: x["principalArn"]), default=str)
    )


def collect_denied(
    enrollment,
    original,
    descriptor,
    session,
    *,
    read_journal,
    read_expiry,
    complete=True,
    **source_options,
):
    schema = denial_contract()
    before = read_journal()
    identity = schema.journal_identity(before, complete=complete)
    require(
        all(
            identity[k] == descriptor[k]
            for k in ("org_id", "workspace_id", "operation_id")
        )
    )
    expiry = read_expiry()
    now = datetime.now(timezone.utc)
    guard = datetime.fromisoformat(expiry["earliest_owner_evidence_at"])
    require(
        now >= guard
        and expiry.get("original_authority_expired") is True
        and expiry.get("issued_sessions_expired") is True
    )
    require(
        all(
            expiry[k] == descriptor[k]
            for k in ("operation_id", "source_principal", "source_key", "source_fence")
        )
    )
    evidence = collect(enrollment, original, descriptor, session, **source_options)
    require(evidence["source_image"].rsplit("@", 1)[-1] == schema.SOURCE_IMAGE)
    read_sources = source_options.get("read_sources", image_sources)
    require(
        read_sources(evidence["source_image"], list(schema.SOURCE_HASHES))
        == schema.SOURCE_HASHES
    )
    authority = enrollment["authority"]
    start = datetime.fromisoformat(descriptor["source_acquired_at"])
    end = guard - timedelta(seconds=900)
    events = {}
    for region in {
        authority["region"],
        enrollment.get("management_region", authority["region"]),
    }:
        client = session.client("cloudtrail", region_name=region)
        for page in client.get_paginator("lookup_events").paginate(
            LookupAttributes=[
                {
                    "AttributeKey": "Username",
                    "AttributeValue": "adp-superplane-operation-"
                    + identity["operation_id"][:8],
                }
            ],
            StartTime=start,
            EndTime=end,
        ):
            for item in page["Events"]:
                event = json.loads(item["CloudTrailEvent"])
                require(
                    event["eventID"] not in events or events[event["eventID"]] == event
                )
                events[event["eventID"]] = event
            require(len(events) <= 1000)
    ordered = sorted(events.values(), key=lambda x: x["eventID"])
    denied = verify_events(
        ordered, identity, authority, acquired_at=start, stopped_at=end
    )
    plan = json.loads(before["plan_json"])
    inventory = verify_absence(
        session.client("eks", region_name=authority["region"]),
        plan,
        cluster_name=identity["cluster_arn"].split("/")[-1],
    )
    require(read_journal() == before and read_expiry() == expiry)
    evidence.update(
        evidence_kind=schema.KIND,
        denial_source_hashes=schema.SOURCE_HASHES,
        denied_grant={
            **identity,
            "denied_request_verified": True,
            "absence_verified": True,
            "event_ids": [denied["eventID"]],
            "cloudtrail_sha256": schema.digest(ordered),
            "inventory_sha256": schema.digest(inventory),
        },
    )
    schema.validate_denial(evidence)
    return evidence


def cleanup_evidence(evidence):
    """Adapt a freshly collected denial to the native absence-only backend."""
    schema = denial_contract()
    denial = schema.validate_denial(evidence)
    now = datetime.now(timezone.utc)
    require(
        datetime.fromisoformat(evidence["checked_at"])
        <= now
        < datetime.fromisoformat(evidence["expires_at"])
    )
    return {
        **denial,
        "evidence_kind": schema.KIND,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
    }
