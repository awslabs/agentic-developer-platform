"""Offline, capability-specific DevOps configuration checks."""

from decimal import Decimal, InvalidOperation

from l5_services.devops.prevention import PreventionPolicy
from l5_services.devops.source_readiness import qualify_source_readiness

REQUIRED_INPUTS = {
    "investigation": ("space", "account", "resources", "telemetry"),
    "release_readiness": ("space", "repository", "base_revision", "head_revision", "native_eligibility"),
    "release_verification": ("space", "source_deployment_link", "isolated_target", "credential_ref", "scenarios"),
    "prevention": ("space", "verified_schedule", "explicit_enablement"),
    "knowledge": ("entitled_sources", "publication_policy", "retrieval_evidence"),
}


def readiness_gaps(settings: dict) -> list[str]:
    """Report missing inputs without treating selected, unverified assets as ready."""
    if settings.get("enabled") is True:
        manual_investigation_selection(settings)
        return ["investigation.setup_activation_required"]
    capabilities = settings.get("capabilities")
    devops = settings.get("devops")
    if (
        not isinstance(capabilities, list) or not capabilities
        or len(capabilities) != len(set(capabilities))
        or any(capability not in REQUIRED_INPUTS for capability in capabilities)
        or settings.get("enabled") is not False
        or settings.get("triggers")
        or not isinstance(devops, dict)
    ):
        raise ValueError("DevOps requires distinct capabilities and disabled automation")

    accounts = devops.get("accounts") or []
    integrations = devops.get("integrations") or []
    if not isinstance(accounts, list) or not isinstance(integrations, list):
        raise ValueError("DevOps accounts and integrations must be lists")
    environments = [environment for account in accounts if isinstance(account, dict)
                    for environment in account.get("environments", []) if isinstance(environment, dict)]
    repositories = [reference for integration in integrations if isinstance(integration, dict)
                    and integration.get("kind") == "github" for reference in integration.get("repository_refs", [])]
    telemetry = [reference for integration in integrations if isinstance(integration, dict)
                 and integration.get("kind") == "cloudwatch" for reference in integration.get("resource_refs", [])]
    available = {
        "space": bool(devops.get("existing_space_id")) if devops.get("space_mode") == "adopt" else False,
        "account": bool(accounts),
        "resources": any(environment.get("resource_refs") for environment in environments),
        "telemetry": bool(telemetry),
        "cluster_ref": all(environment.get("cluster_ref") for environment in environments
                           if environment.get("workload_kind") == "eks"),
        "namespace": all(environment.get("namespace") for environment in environments
                         if environment.get("workload_kind") == "eks"),
        "repository": bool(repositories),
        "entitled_sources": bool((devops.get("knowledge_policy") or {}).get("source_refs")),
        "publication_policy": bool((devops.get("knowledge_policy") or {}).get("publish_results")),
        "explicit_enablement": bool(devops.get("prevention_enabled")),
    }
    gaps = []
    for capability in capabilities:
        requirements = REQUIRED_INPUTS[capability] + (
            ("cluster_ref", "namespace", "network_access") if capability == "investigation"
            and any(environment.get("workload_kind") == "eks" for environment in environments) else ()
        )
        missing = [f"{capability}.needs_{field}" for field in requirements if not available.get(field, False)]
        gaps.extend(missing or [f"{capability}.provider_unqualified"])
    return gaps


def integration_readiness(settings: dict, observations: list[dict] | None = None,
                          *, configuration_revision: int | None = None) -> list[dict]:
    """Keep saved choices distinct from revision-matched native readbacks."""
    integrations = (settings.get("devops") or {}).get("integrations") or []
    observations = observations or []
    results = []
    for selection in integrations:
        kind = selection.get("kind")
        if kind not in {"github", "cloudwatch"}:
            results.append({"version": 1, "kind": kind, "status": "blocked",
                            "reason": "unsupported_connector"})
            continue
        matches = [observation for observation in observations
                   if observation.get("selection") == selection
                   and configuration_revision is not None
                   and observation.get("configuration_revision") == configuration_revision]
        if len(matches) > 1:
            results.append({"version": 1, "kind": kind, "status": "blocked",
                            "reason": "ambiguous_integration_observations"})
        elif not matches:
            results.append({"version": 1, "kind": kind, "status": "verifying",
                            "reason": "integration_readback_missing",
                            "events": {"status": "verifying", "reason": "event_support_unverified"}})
        else:
            if isinstance(matches[0].get("unavailable"), dict):
                failure = matches[0]["unavailable"]
                results.append({"version": 1, "kind": kind, "status": "blocked",
                                "reason": failure.get("code", "integration_readback_missing"),
                                "message": failure.get("message", "Recheck the selected integration's current provider access.")})
                continue
            try:
                results.append(qualify_source_readiness(selection, matches[0]["evidence"], matches[0]["native"]))
            except (ValueError, KeyError, TypeError, AttributeError):
                results.append({"version": 1, "kind": kind, "status": "blocked",
                                "reason": "integration_readback_mismatch"})
    return results


DEVOPS_FIELDS = frozenset({
    "hosting_connection_ref", "region", "space_mode", "existing_space_id", "accounts", "integrations",
    "access_policy_ref", "authentication_mode", "permitted_native_actions", "knowledge_policy",
    "trigger_policies", "incident_sources", "paused", "prevention_enabled", "prevention_policy", "retention_days", "browser_launch_policy",
    "launcher_role_arn", "operator_role_arn",
})
REFERENCE_FIELDS = frozenset({
    "tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision",
})


def _references(value):
    if isinstance(value, dict):
        if REFERENCE_FIELDS.intersection(value):
            if REFERENCE_FIELDS != set(value) or any(
                not isinstance(item, str) or not item or item in {"latest", "HEAD", "null"}
                for item in value.values()
            ):
                raise ValueError("DevOps references must be complete and pinned")
            yield value
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def validate_devops_selection(devops, tenant_id, project_id, selected_sources, connections, *, enabled=False):
    """Reject foreign or inferred runtime selections before storing a draft."""
    if set(devops) - DEVOPS_FIELDS:
        raise ValueError("unsupported DevOps choice")
    PreventionPolicy.from_configuration({"devops": devops})
    if devops.get("space_mode") not in {None, "create", "adopt"} or (
        bool(devops.get("existing_space_id")) != (devops.get("space_mode") == "adopt")
    ):
        raise ValueError("DevOps space adoption requires an explicit space ID")
    if devops.get("authentication_mode") not in {None, "scoped_iam_federation"}:
        raise ValueError("unsupported DevOps authentication mode")
    if devops.get("browser_launch_policy") not in {None, "disabled", "scoped_session"}:
        raise ValueError("unsupported DevOps browser policy")
    if devops.get("prevention_enabled") is True or not enabled and any(
        trigger.get("enabled") for trigger in devops.get("trigger_policies", [])
    ):
        raise ValueError("DevOps draft cannot enable native automation")
    accounts = devops.get("accounts", [])
    integrations = devops.get("integrations", [])
    if not isinstance(accounts, list) or not isinstance(integrations, list) or any(
        not isinstance(account, dict) or not isinstance(account.get("environments", []), list)
        or any(not isinstance(environment, dict) for environment in account.get("environments", []))
        for account in accounts
    ):
        raise ValueError("DevOps accounts and environments must be selected explicitly")
    if any(not isinstance(integration, dict) or integration.get("kind") not in {"github", "cloudwatch"}
           for integration in integrations):
        raise ValueError("unsupported DevOps connector")
    for account in accounts:
        for environment in account.get("environments", []):
            if environment.get("environment_ref") not in (None, *selected_sources):
                raise ValueError("DevOps workload must use a selected environment")
    for reference in _references(devops):
        if (reference["tenant_id"], reference["project_id"]) != (tenant_id, project_id):
            raise ValueError("DevOps reference must belong to selected project")
        if reference["record_type"] == "connection" and reference not in connections:
            raise ValueError("DevOps connection must be explicitly authorized")
    from l5_services.run_input import TestProfileRef

    for trigger in devops.get("trigger_policies", []):
        if trigger.get("capability") == "release_verification":
            profile = TestProfileRef.model_validate(trigger.get("test_profile_ref"))
            if (trigger.get("kind") != "manual" or profile.source_connection_id !=
                    (devops.get("hosting_connection_ref") or {}).get("source_connection_id")):
                raise ValueError("verification profile must use the selected AWS connection")
        elif "test_profile_ref" in trigger:
            raise ValueError("test profiles require release verification")


def manual_investigation_selection(settings):
    """Initial activation supports a bounded manual task in a dedicated space.

    A selected environment and nonempty authorized resource selection remain
    mandatory. Saving this mode does not qualify workload diagnosis or release gates.
    """
    selected = settings.get("devops") or {}
    triggers = selected.get("trigger_policies") or []
    accounts = selected.get("accounts") or []
    sources = settings.get("source_refs") or []
    if (
        settings.get("enabled") is not True
        or settings.get("capabilities") != ["investigation"]
        or not isinstance(settings.get("classification"), str) or not settings["classification"]
        or settings.get("triggers")
        or selected.get("space_mode") not in {"create", "adopt"}
        or bool(selected.get("existing_space_id")) != (selected.get("space_mode") == "adopt")
        or selected.get("integrations") or selected.get("incident_sources")
        or selected.get("prevention_enabled")
        or (selected.get("prevention_policy") or {}).get("enabled")
        or not (selected.get("browser_launch_policy") in {None, "disabled"}
                or selected.get("browser_launch_policy") == "scoped_session"
                and selected.get("authentication_mode") == "scoped_iam_federation")
        or selected.get("permitted_native_actions")
        or len(triggers) != 1 or not isinstance(triggers[0], dict)
        or len(accounts) != 1 or not isinstance(accounts[0], dict)
        or len(sources) != 1 or not isinstance(sources[0], dict)
        or sources[0].get("record_type") != "environment"
    ):
        raise ValueError("Enable only a bounded manual investigation with one selected environment and native automation disabled")
    trigger, environment = triggers[0], sources[0]
    environments = accounts[0].get("environments") or []
    if (
        trigger.get("kind") != "manual" or trigger.get("capability") != "investigation"
        or trigger.get("enabled") is not True or not isinstance(trigger.get("binding_id"), str)
        or not trigger["binding_id"] or trigger.get("environment_ref") != environment
        or not isinstance(trigger.get("resource_refs"), list) or not 1 <= len(trigger["resource_refs"]) <= 100
        or not isinstance(trigger.get("telemetry_refs"), list) or len(trigger["telemetry_refs"]) > 100
        or len(environments) != 1 or environments[0].get("environment_ref") != environment
        or environments[0].get("resource_refs") != trigger["resource_refs"]
        or environment.get("source_connection_id") != (selected.get("hosting_connection_ref") or {}).get("record_id")
    ):
        raise ValueError("Manual investigation requires the exact setup binding, selected environment and explicit authorized resources")
    limits = settings.get("limits") or {}
    try:
        cost = Decimal(limits["max_cost"])
    except (KeyError, InvalidOperation, TypeError, ValueError):
        raise ValueError("Manual investigation requires a finite USD planning estimate") from None
    if (
        not cost.is_finite() or cost <= 0 or limits.get("currency") != "USD"
        or type(limits.get("deadline_seconds")) is not int or not 60 <= limits["deadline_seconds"] <= 3600
        or limits.get("max_provider_attempts") != 1 or type(limits.get("max_provider_attempts")) is not int
        or type(limits.get("max_polls")) is not int or not 1 <= limits["max_polls"] <= 60
        or limits.get("concurrency") != 1 or type(limits.get("concurrency")) is not int
    ):
        raise ValueError("Manual investigation requires one provider attempt, concurrency one and finite polling")
    return trigger
