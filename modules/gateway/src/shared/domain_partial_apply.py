"""Protected installation-owner partial state facts, distinct from absence proof."""

import re
from datetime import UTC, datetime, timedelta

from src.shared.domain_provider_contract import digest

IDENTITY = ("org_id", "workspace_id", "operation_id", "plan_digest", "source_key", "source_fence")


def record_id(value):
    identity = {key: value[key] for key in IDENTITY}
    if value.get("version") == 2:
        identity["inspection_id"] = value["inspection_id"]
    return "sppa1:" + digest(identity)


def validate(value, *, fresh=True):
    try:
        if not isinstance(value, dict):
            raise ValueError()
        now = datetime.now(UTC)
        checked, expiry = (datetime.fromisoformat(value[k]) for k in ("checked_at", "expires_at"))
        boundary = value["preparation_boundary"]
        observation = boundary["observation"]
        snapshot = value["snapshot"]
        files = observation["source_files"]
        retained = snapshot["resources"]
        cluster, eip = retained["aws_eks_cluster.workspace"], retained["aws_eip.nat[0]"]
        valid = (
            type(value.get("version")) is int
            and value["version"] in {1, 2}
            and (
                value["version"] == 1
                and "inspection_id" not in value
                or value["version"] == 2
                and re.fullmatch(r"[a-f0-9]{16}", value.get("inspection_id", ""))
            )
            and value.get("record_id") == record_id(value)
            and value.get("evidence_kind") == boundary.get("kind") == observation.get("kind") == "partial-apply-state-v1"
            and value.get("apply_started") is observation.get("apply_started") is True
            and value.get("planning_started") is True
            and "attempt_extension" not in value
            and type(value.get("source_fence")) is int
            and 1 <= value["source_fence"] <= 32
            and type(value.get("source_version")) is int
            and value["source_version"] > 0
            and checked.tzinfo is not None
            and expiry.tzinfo is not None
            and checked < expiry <= checked + timedelta(hours=1)
            and (not fresh or checked <= now < expiry)
            and snapshot == boundary["backend_observation"]
            and boundary["cluster_status"] == "ACTIVE"
            and boundary["sha256"] == digest(observation)
            and observation["source_operation_id"] == value["original_planning_operation_id"]
            and set(files)
            == {
                "workspace.tfplan",
                "workspace-plan.json",
                "workspace-inventory.json",
                "workspace-estimate.json",
                "workspace-authorization.proposed.json",
                "workspace-backend.json",
            }
            and all(
                isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v)
                for v in [
                    *files.values(),
                    *observation["apply_files"].values(),
                    observation["module_sha256"],
                    value["plan_digest"],
                    value["original_artifact_id"],
                    snapshot["sha256"],
                    value["inspection_sha256"],
                    boundary["lifecycle_policy_sha256"],
                ]
            )
            and type(snapshot["serial"]) is int
            and snapshot["serial"] > 0
            and snapshot["version_id"] not in {"", "null", None}
            and 2 <= len(retained) <= 500
            and cluster["type"] == "aws_eks_cluster"
            and cluster["tainted"] is False
            and snapshot["cluster_arn"] == f"arn:aws:eks:{value['region']}:{value['account_id']}:cluster/{cluster['id']}"
            and eip["type"] == "aws_eip"
            and eip["tainted"] is True
            and re.fullmatch(r"eipalloc-[a-f0-9]+", eip["id"])
            and all(resource["tainted"] is False for address, resource in retained.items() if address != "aws_eip.nat[0]")
            and all(
                isinstance(value[k], str) and value[k]
                for k in (
                    "source_principal",
                    "workload_binding",
                    "source_pod_name",
                    "source_acquired_at",
                    "source_image",
                    "storage_uid",
                    "volume_uid",
                    "probe_job_uid",
                    "credential_id",
                    "installation_id",
                    "adp_org_id",
                )
            )
        )
        if not valid:
            raise ValueError()
    except (KeyError, TypeError, AttributeError, ValueError):
        raise ValueError("partial apply owner evidence invalid or expired") from None
    return value
