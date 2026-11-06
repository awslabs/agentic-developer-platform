"""Owner-read log proof of the qualified pre-component permission refusal.

The retained prerequisite marker is not a no-effect proof. Only the original
source-qualified failure chain establishes which permission gate failed. The
caller separately authenticates the source pod, its termination and image bytes.
"""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta

from .bootstrap_recovery import source_contract
from .provider_authority import require

KIND = "bootstrap-precomponent-permission-refusal-v1"


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def instant(value):
    result = datetime.fromisoformat(value)
    require(result.tzinfo is not None)
    return result


def frames(value):
    require(isinstance(value, list) and len(value) <= 12)
    result = []
    for frame in value:
        require(
            isinstance(frame, dict)
            and set(frame) == {"file", "line", "function"}
            and isinstance(frame["file"], str)
            and type(frame["line"]) is int
            and frame["line"] > 0
            and isinstance(frame["function"], str)
        )
        result.append((frame["file"], frame["line"], frame["function"]))
    return result


def verify_failure(value):
    """This five-cause chain belongs to the one qualified historical image.

    The marker excludes acquisition permission checks. Absent retention and the
    original cleanup prefix exclude post-installation supervisor checks. The
    remaining SSAR origin is install_components.verify_install_permissions,
    before namespace adoption, CRD apply or component creation.
    """
    require(
        set(value) == {"event", "exceptions"}
        and value["event"] == "lifecycle_provider_failed"
    )
    chain = value["exceptions"]
    require(isinstance(chain, list) and len(chain) == 5)
    require(
        all(set(item) == {"exception_type", "frames"} for item in chain)
        and [item["exception_type"] for item in chain]
        == [
            "LifecycleRefused",
            "BootstrapRefused",
            "BootstrapRefused",
            "LifecycleRefused",
            "LifecycleRefused",
        ]
    )
    locations = [frames(item["frames"]) for item in chain]
    require(
        locations[0] == [("diagnostics.py", 38, "call"), ("runtime.py", 555, "hook")]
        and locations[1] == []
        and locations[2]
        == [
            ("workspace.py", 851, "bootstrap_workspace"),
            ("temporary_authority.py", 143, "revoke"),
        ]
        and locations[3]
        == [
            ("endpoint.py", 198, "_send_request"),
            ("endpoint.py", 132, "create_request"),
            ("hooks.py", 412, "emit"),
            ("hooks.py", 256, "emit"),
            ("hooks.py", 239, "_emit"),
            ("signers.py", 108, "handler"),
            ("signers.py", 191, "sign"),
            ("signers.py", 312, "get_auth_instance"),
            ("credentials.py", 697, "get_frozen_credentials"),
            ("credentials.py", 584, "_refresh"),
            ("credentials.py", 600, "_protected_refresh"),
            ("credentials.py", 32, "refresh"),
        ]
        and locations[4]
        == [
            ("cluster_clients.py", 179, "self_subject_access_review"),
            ("cluster_clients.py", 156, "call_api"),
            ("api_client.py", 348, "call_api"),
            ("api_client.py", 166, "__call_api"),
            ("api_client.py", 524, "update_params_for_auth"),
            ("configuration.py", 344, "auth_settings"),
            ("configuration.py", 309, "get_api_key_with_prefix"),
            ("cluster_clients.py", 109, "refresh"),
            ("credentials.py", 697, "get_frozen_credentials"),
            ("credentials.py", 584, "_refresh"),
            ("credentials.py", 600, "_protected_refresh"),
            ("credentials.py", 32, "refresh"),
        ]
    )


def collect(
    session, *, evidence, identity, source, management_cluster, management_region
):
    """Read authentic original logs; accept no caller-supplied event or absence."""
    require(
        source == source_contract().qualified_source(evidence["source_image"])
        and source.get("precomponent_permission_refusal") is True
        and evidence["source_revision"] == source["revision"]
        and evidence["source_hashes"] == source["hashes"]
        and evidence["source_pod_uid"] == evidence["workload_binding"]
        and evidence["source_audit"]["log_group"]
        == f"/aws/eks/{management_cluster}/cluster"
        and evidence["source_audit"]["creation_event_id"]
        and evidence["source_audit"]["terminal_event_ids"]
        and all(
            evidence[key] == identity[key]
            for key in ("operation_id", "org_id", "workspace_id")
        )
    )
    acquired, checked = (
        instant(evidence["source_acquired_at"]),
        instant(evidence["checked_at"]),
    )
    require(
        acquired < checked <= datetime.now(UTC)
        and checked - acquired <= timedelta(days=30)
    )
    group = f"/aws/containerinsights/{management_cluster}/application"
    client = session.client("logs", region_name=management_region)
    selected, count = {}, 0
    for page_number, page in enumerate(
        client.get_paginator("filter_log_events").paginate(
            logGroupName=group,
            startTime=int((acquired - timedelta(minutes=5)).timestamp() * 1000),
            endTime=int(checked.timestamp() * 1000),
            filterPattern=json.dumps(evidence["source_pod_name"]),
        )
    ):
        require(page_number < 100)
        for event in page.get("events", []):
            count += 1
            require(count <= 10000)
            outer = json.loads(event["message"])
            metadata = outer.get("kubernetes", {})
            if metadata.get("pod_name") != evidence["source_pod_name"]:
                continue
            require(
                metadata.get("pod_id") == evidence["source_pod_uid"]
                and metadata.get("container_hash") == evidence["source_image"]
                and metadata.get("container_name") == "paid-worker"
                and isinstance(metadata.get("namespace_name"), str)
                and metadata["namespace_name"]
                and re.fullmatch(r"[a-f0-9]{64}", metadata.get("docker_id", ""))
            )
            try:
                value = json.loads(outer["log"])
            except (ValueError, KeyError):
                continue  # Non-JSON application output establishes no boundary.
            if not isinstance(value, dict):
                continue
            kind = value.get("event")
            if kind not in {"lifecycle_provider_failed", "paid_worker_stopped"}:
                continue
            when = instant(outer["time"])
            require(
                kind not in selected
                and isinstance(event.get("eventId"), str)
                and event["eventId"]
                and isinstance(event.get("logStreamName"), str)
                and event["logStreamName"].endswith(
                    f"{metadata['pod_name']}_{metadata['namespace_name']}_"
                    f"paid-worker-{metadata['docker_id']}.log"
                )
                and acquired <= when < checked
                and type(event.get("timestamp")) is int
                and int(when.timestamp() * 1000) == event["timestamp"]
            )
            selected[kind] = (event, outer, value, when)
    require(set(selected) == {"lifecycle_provider_failed", "paid_worker_stopped"})
    failure, failed_outer, diagnostic, failed_at = selected["lifecycle_provider_failed"]
    stopped, stop_outer, stop, stopped_at = selected["paid_worker_stopped"]
    verify_failure(diagnostic)
    require(
        set(stop) == {"event", "exception_type", "frames"}
        and stop["exception_type"] == "LifecycleRefused"
        and frames(stop["frames"])
        and failed_at < stopped_at
        and failure["eventId"] != stopped["eventId"]
        and failure["logStreamName"] == stopped["logStreamName"]
        and failed_outer["kubernetes"] == stop_outer["kubernetes"]
    )
    return {
        "kind": KIND,
        **{
            key: identity[key]
            for key in (
                "operation_id",
                "org_id",
                "workspace_id",
                "generation",
                "plan_sha256",
                "precomponent_progress_sha256",
            )
        },
        **{
            key: evidence[key]
            for key in (
                "source_image",
                "source_revision",
                "source_pod_name",
                "source_pod_uid",
            )
        },
        "source_hashes_sha256": digest(source["hashes"]),
        "source_audit_sha256": digest(evidence["source_audit"]),
        "prerequisite_inventory_sha256": digest(identity["prerequisite_inventory"]),
        "log_group": group,
        "failure_event_id": failure["eventId"],
        "failure_log_stream": failure["logStreamName"],
        "failure_message_sha256": hashlib.sha256(
            failure["message"].encode()
        ).hexdigest(),
        "stopped_event_id": stopped["eventId"],
        "stopped_message_sha256": hashlib.sha256(
            stopped["message"].encode()
        ).hexdigest(),
        "failed_at": failed_at.isoformat(),
        "stopped_at": stopped_at.isoformat(),
    }
