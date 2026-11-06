"""Local Security configuration readiness and effective monitoring policy."""

from l5_services.devops_configuration import readiness_gaps
from l5_services.run_input import ExecutionSettings
from l5_services.well_architected_sources import repository_pin_missing
from pydantic import ValidationError


SOURCE_TYPE = {"threat_model": {"document"}, "code_review": {"repository", "repository_commit"}}
MONITORING_DEFAULTS = {"poll_interval_seconds": 600, "poll_grace_seconds": 300}


def execution_input_gaps(settings: dict, *, scope: dict | None = None) -> list[str]:
    """Name missing or malformed execution fields without filling in policy choices."""
    try:
        parsed = ExecutionSettings.model_validate({field: settings[field] for field in ("classification", "limits")
                                                    if settings.get(field)})
    except ValidationError as error:
        return list(dict.fromkeys(".".join(str(part) for part in item["loc"])
                                  for item in error.errors(include_input=False, include_url=False)))
    policy = parsed.limits.policy_ref
    gaps = ["limits.policy_ref.record_type"] if policy.record_type != "policy" else []
    # Saved Profile scope is authoritative during configuration checks; callers
    # admitting work additionally supply the current configuration scope.
    expected = scope if scope is not None else settings.get("profile_ref") or {}
    for field in ("tenant_id", "project_id"):
        if field in expected and getattr(policy, field) != expected[field]:
            gaps.append(f"limits.policy_ref.{field}")
    return gaps


def transform_capability_readiness(settings: dict, *, qualified_executor_ref: dict | None = None) -> list[dict]:
    """Report selected capabilities; a saved reference alone is not qualification."""
    transform = settings.get("transform") or {}
    gaps = []
    if not transform.get("test_policy_ref"):
        gaps.append({"code": "test_policy_ref_missing", "field": "transform.test_policy_ref",
                     "owner": "product", "affected_step": "test"})
    if not transform.get("executor_binding_ref"):
        gaps.append({"code": "executor_binding_ref_missing", "field": "transform.executor_binding_ref",
                     "owner": "executor", "affected_step": "execute"})
    elif qualified_executor_ref is None:
        gaps.append({"code": "executor_unqualified", "field": "transform.executor_binding_ref",
                     "owner": "executor", "affected_step": "execute"})
    elif qualified_executor_ref != transform["executor_binding_ref"]:
        gaps.append({"code": "executor_qualification_stale", "field": "transform.executor_binding_ref",
                     "owner": "executor", "affected_step": "execute"})
    status = ("missing_input" if any(gap["code"].endswith("_missing") for gap in gaps)
              else "stale" if any(gap["code"] == "executor_qualification_stale" for gap in gaps)
              else "unqualified" if gaps else "ready")
    return [{"capability": capability, "status": status, "gaps": gaps}
            for capability in settings.get("capabilities") or []]


def effective_monitoring(settings: dict) -> dict:
    supplied = settings.get("monitoring") or {}
    if not isinstance(supplied, dict) or set(supplied) - {
        "poll_interval_seconds", "poll_grace_seconds", "stall_after_seconds"
    }:
        raise ValueError("unsupported monitoring policy")
    policy = {**MONITORING_DEFAULTS, **supplied}
    for field in ("poll_interval_seconds", "poll_grace_seconds"):
        value = policy[field]
        if type(value) is not int or not 60 <= value <= 900:
            raise ValueError(f"{field} must be an integer from 60 to 900")
    if "stall_after_seconds" in policy:
        stall = policy["stall_after_seconds"]
        if type(stall) is not int or not max(600, 2 * policy["poll_interval_seconds"]) <= stall <= 86400:
            raise ValueError("stall_after_seconds must allow two polls and be from 600 to 86400")
        limits = settings.get("limits")
        deadline = limits.get("deadline_seconds") if isinstance(limits, dict) else None
        if deadline is not None and (type(deadline) is not int or stall > deadline):
            raise ValueError("stall_after_seconds must not exceed the run deadline")
    return policy


def assess_readiness(
    settings: dict, configuration_id: str, bindings: list[dict], *, delegation_active: bool, service_id: str = "security"
) -> tuple[str, list[str], dict]:
    """Check the saved choices without contacting any provider or changing bindings."""
    policy = effective_monitoring(settings)
    if service_id == "finops":
        if settings.get("capabilities") != ["cost_inquiry"]:
            raise ValueError("FinOps requires the cost_inquiry capability")
        if type(settings.get("enabled")) is not bool:
            raise ValueError("FinOps enabled must be a boolean")
        gaps = [field for field in ("connection_ref", "region", "limits") if not settings.get(field)]
        finops = settings.get("finops") or {}
        qualified = [binding for binding in bindings if binding.get("configuration_id") == configuration_id
                     and binding.get("state") == "ready" and not binding.get("gaps")
                     and binding.get("connection_ref") == settings.get("connection_ref")
                     and (binding.get("native_resource_ref") or {}).get("record_id") == finops.get("native_agent_ref")
                     and binding.get("native_agent_display_name")
                     and all((binding.get("billing_scope") or {}).get(field) == (finops.get("billing_scope") or {}).get(field)
                             for field in ("account_ids", "view_ref"))]
        if not finops.get("billing_scope"):
            gaps.append("billing_scope")
        elif len(qualified) != 1:
            gaps.append("billing_scope_unverified")
        if not finops.get("data_role_ref"):
            gaps.append("data_role_ref")
        if not finops.get("agent_mode"):
            gaps.append("agent_mode")
        if not delegation_active:
            gaps.append("execution_delegation")
        if len(qualified) != 1:
            gaps.append("native_unqualified")
        return ("disabled" if not settings["enabled"] else "needs_input" if gaps else "ready"), gaps, policy
    if service_id == "devops":
        return ("needs_input" if settings.get("enabled") else "disabled"), readiness_gaps(settings), policy
    if service_id == "well_architected":
        from datetime import UTC, datetime
        from l5_services.events.cron import next_tick

        for trigger in settings.get("triggers") or []:
            if trigger.get("kind") == "schedule":
                next_tick(trigger.get("schedule"), datetime.now(UTC))
        gaps = ["catalog_mapping_unresolved", "provider_unqualified"]
        if repository_pin_missing(settings.get("source_refs") or []):
            gaps.append("repository_revision_unpinned")
        if settings.get("enabled"):
            for field in ("source_refs", "connection_ref", "region"):
                if not settings.get(field):
                    gaps.append(field)
            gaps.extend(execution_input_gaps(settings))
            review = settings.get("well_architected") or {}
            for field in ("execution_role_ref", "source_store_ref"):
                if not review.get(field):
                    gaps.append(f"well_architected.{field}")
            if not delegation_active:
                gaps.append("execution_delegation")
        return ("needs_input" if settings.get("enabled") else "disabled", gaps, policy)
    if service_id == "transform":
        gaps = list(dict.fromkeys(gap["code"] for item in transform_capability_readiness(settings)
                                  for gap in item["gaps"]))
        return ("needs_input" if settings.get("enabled") else "disabled", gaps, policy)
    if service_id != "security":
        raise ValueError("unsupported L5 service")

    gaps = []
    capabilities = settings.get("capabilities") or []
    if (
        not isinstance(capabilities, list) or not capabilities
        or any(cap not in SOURCE_TYPE for cap in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise ValueError("capabilities must name distinct supported Security capabilities")
    sources = settings.get("source_refs") or []
    if not isinstance(sources, list) or any(not isinstance(source, dict) or "record_type" not in source for source in sources):
        raise ValueError("source_refs must be typed references")
    execution_gaps = execution_input_gaps(settings)
    for field in ("purpose", "classification", "connection_ref", "region", "limits"):
        if field in {"classification", "limits"}:
            gaps.extend(gap for gap in execution_gaps if gap == field or gap.startswith(f"{field}."))
            continue
        if not settings.get(field):
            gaps.append(field)
    if not sources:
        gaps.append("source_refs")
    for capability in capabilities:
        if not any(source["record_type"] in SOURCE_TYPE[capability] for source in sources):
            gaps.append(f"source_refs.{capability}")
    if not delegation_active:
        gaps.append("execution_delegation")
    available_bindings = {binding["binding_id"]: binding for binding in bindings}
    for trigger in settings.get("triggers") or []:
        capability, binding_id = trigger.get("capability"), trigger.get("binding_id")
        if bool(capability) != bool(binding_id):
            raise ValueError("trigger capability and binding_id must be set together")
        if trigger.get("kind") == "schedule":
            from datetime import UTC, datetime
            from l5_services.events.cron import next_tick

            if not trigger.get("schedule"):
                raise ValueError("scheduled trigger needs a schedule expression")
            # An expression no UTC tick can match would silently never assess.
            next_tick(trigger["schedule"], datetime.now(UTC))
        if not trigger.get("enabled"):
            continue
        if not capability or capability not in capabilities:
            raise ValueError("enabled trigger must select a configured capability")
        if not binding_id:
            raise ValueError("enabled trigger must select a binding")
        binding = available_bindings.get(binding_id)
        if binding is None or binding.get("configuration_id") != configuration_id:
            raise ValueError("trigger binding must belong to this configuration")
        if binding.get("connection_ref") != settings.get("connection_ref"):
            raise ValueError("trigger binding must use the selected connection")
        if binding.get("state") != "ready":
            gaps.append("binding_not_ready")
        selected = trigger.get("source_refs") or []
        if not selected or any(
            source not in sources or source.get("record_type") not in SOURCE_TYPE[capability]
            for source in selected
        ):
            raise ValueError("trigger sources must be selected and compatible with its capability")
    if not settings.get("enabled", False):
        return "disabled", gaps, policy
    return ("needs_input" if gaps else "ready"), gaps, policy
