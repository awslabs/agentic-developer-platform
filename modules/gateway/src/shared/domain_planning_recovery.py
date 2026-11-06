"""Protected installer evidence for an unstarted, expired initial plan.

Only the infrastructure owner can write this namespace in the provider authority
table. This record attests a retained-state inspection; it is not human approval,
credential non-issuance, or a general declaration that cloud resources are absent.
"""

from datetime import UTC, datetime, timedelta

from src.shared.domain_provider_contract import digest

IDENTITY = ("org_id", "workspace_id", "operation_id", "plan_digest", "source_key", "source_fence")


def record_id(identity):
    return "sppr1:" + digest({key: identity[key] for key in IDENTITY})


def _retained_saved_files(boundary, observation, value):
    """Derived pricing reports are distinct from the three retained artifacts."""
    import re

    files = observation.get("files", {})
    pricing = observation.get("pricing_revalidation")
    if pricing is None:
        return all(boundary.get("entries", {}).get("review/" + name) == {"kind": "file", "sha256": digest} for name, digest in files.items())
    original = {"workspace.tfplan", "workspace-plan.json", "workspace-inventory.json"}
    derived = {"workspace-estimate.json", "workspace-authorization.proposed.json"}
    if (
        not isinstance(pricing, dict)
        or set(pricing) != {"version", "original_files", "replacements", "original_module_sha256", "corrected_module_sha256", "refusal_sha256"}
        or pricing.get("version") != 1
        or value.get("region") != "us-west-2"
        or not isinstance(pricing.get("original_files"), dict)
        or set(pricing["original_files"]) != original
        or not isinstance(pricing.get("replacements"), dict)
        or set(pricing["replacements"]) != {"scripts/check_workspace_plan.py", "regional-pricing.json"}
        or any(
            not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v)
            for v in [
                pricing["original_module_sha256"],
                pricing["corrected_module_sha256"],
                pricing["refusal_sha256"],
                *pricing["replacements"].values(),
            ]
        )
        or pricing["original_module_sha256"] == pricing["corrected_module_sha256"]
        or any(pricing["original_files"][name] != files.get(name) for name in original)
        or any("review/" + name in boundary.get("entries", {}) for name in derived)
    ):
        return False
    return all(boundary.get("entries", {}).get("review/" + name) == {"kind": "file", "sha256": files[name]} for name in original)


def validate(value, *, completion=False, interrupted=False):
    if completion and interrupted:
        raise ValueError("planning scopes are distinct")
    if not isinstance(value, dict):
        raise ValueError("planning owner evidence unavailable")
    now = datetime.now(UTC)
    checked = datetime.fromisoformat(value["checked_at"])
    expiry = datetime.fromisoformat(value["expires_at"])
    if (
        value.get("version") != 1
        or value.get("evidence_kind")
        != ("interrupted-read-only-plan-v1" if interrupted else "saved-plan-finalization-v1" if completion else "planning-start-barrier-v1")
        or value.get("planning_started") is not (completion or interrupted)
        or value.get("record_id") != record_id(value)
        or checked.tzinfo is None
        or expiry.tzinfo is None
        or not checked <= now < expiry <= checked + timedelta(hours=1)
        or type(value.get("source_fence")) is not int
        or not 1 <= value["source_fence"] <= 32
        or not value.get("source_principal")
        or not value.get("workload_binding")
        or not value.get("storage_uid")
        or not value.get("volume_uid")
        or not value.get("inspection_sha256")
        or not value.get("source_image")
    ):
        raise ValueError("planning owner evidence invalid or expired")
    if interrupted:
        from src.shared.interrupted_plan_evidence import validate_boundary

        validate_boundary(value)
    if completion:
        import re

        boundary = value.get("preparation_boundary", {})
        observation = boundary.get("observation", {})
        files = observation.get("files", {})
        if (
            value.get("apply_started") is not False
            or "attempt_extension" in value
            or boundary.get("kind") != "saved-plan-finalization-v1"
            or observation.get("version") != 1
            or observation.get("terraform_version") != "1.9.8"
            or observation.get("initial_create_only") is not True
            or observation.get("offline_guard_passed") is not True
            or set(files)
            != {
                "workspace.tfplan",
                "workspace-plan.json",
                "workspace-inventory.json",
                "workspace-estimate.json",
                "workspace-authorization.proposed.json",
            }
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in files.values())
            or not _retained_saved_files(boundary, observation, value)
            or any(observation.get("target", {}).get(k) != value[k] for k in ("account_id", "org_id", "workspace_id"))
            or observation.get("target", {}).get("aws_region") != value["region"]
        ):
            raise ValueError("saved plan completion evidence is incomplete")
    return value
