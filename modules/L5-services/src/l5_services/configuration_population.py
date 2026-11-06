"""Build a Security draft from a scoped Product Profile and its authorized sources."""

from copy import deepcopy

from l5_services.configuration_readiness import transform_capability_readiness
from l5_services.devops_configuration import validate_devops_selection
from l5_services.run_input import WellArchitectedSettings
from l5_services.transform_contract import TransformConfigurationDraftInput
from l5_services.well_architected_sources import canonical_choices, canonical_source, repository_pin_missing

REFERENCE_FIELDS = frozenset({
    "tenant_id", "project_id", "source_connection_id",
    "record_type", "record_id", "record_revision",
})
SOURCE_TYPES = frozenset({"document", "repository", "repository_commit"})
CHOICE_FIELDS = frozenset({
    "source_refs", "connection_ref", "region", "capabilities", "enabled",
    "visibility", "exclusions", "overrides", "triggers", "limits",
    "purpose", "classification", "monitoring", "service_id", "devops", "finops", "well_architected", "transform",
})


def _scoped_ref(reference, tenant_id, project_id):
    return (
        isinstance(reference, dict)
        and set(reference) == REFERENCE_FIELDS
        and all(isinstance(value, str) and value and value not in {"latest", "null", "HEAD"} for value in reference.values())
        and reference["tenant_id"] == tenant_id
        and reference["project_id"] == project_id
    )


def _ref_key(reference):
    return tuple(reference[field] for field in sorted(REFERENCE_FIELDS))


def authorized_connection_refs(available_sources: dict, tenant_id: str, project_id: str) -> list[dict]:
    if available_sources.get("coverage") != "complete" or available_sources.get("next_cursor") is not None:
        return []
    references = []
    for item in available_sources.get("items", []):
        connection = item.get("connection_ref")
        reference = item.get("record_ref")
        if (isinstance(connection, dict) and isinstance(connection.get("id"), str)
            and _scoped_ref(reference, tenant_id, project_id)
            and reference["record_type"] == "connection"
            and reference["record_id"] == connection["id"]
            and reference["source_connection_id"] == connection["id"]
            and reference not in references):
            references.append(deepcopy(reference))
    return references


def finops_configuration_options(profile: dict, available_sources: dict) -> dict:
    tenant_id, project_id = profile["tenant_id"], profile["project_id"]
    selections = (profile.get("configuration") or {}).get("selected_capabilities") or []
    selected = any(
        isinstance(item, dict) and item.get("capability") == "finops"
        and item.get("ownership_mode") != "not_applicable"
        for item in selections
    )
    connections = authorized_connection_refs(available_sources, tenant_id, project_id)
    reasons = []
    if not selected:
        reasons.append("capability_not_selected")
    if available_sources.get("coverage") != "complete" or available_sources.get("next_cursor") is not None:
        reasons.append("connection_options_incomplete")
    if not connections:
        reasons.append("connection_revision_unavailable")
    reasons.extend(("region_selection_required", "billing_scope_unverified", "native_unqualified"))
    return {
        "service_id": "finops", "connections": connections, "sources": [],
        "regions": available_sources.get("regions", []) if connections else [],
        "capabilities": ["cost_inquiry"] if selected else [],
        "unavailable_reasons": reasons, "provider_contacted": False,
        "readiness": {"capability": "cost_inquiry", "status": "blocked", "gaps": reasons},
        "catalog_mapping": {"status": "unresolved", "module_id": None, "feature_id": None,
                            "owner": "FO01 and PranavSharma1000 with SA01 #8683"},
    }


def configuration_options(profile: dict, available_sources: dict, service_id: str = "security") -> dict:
    if service_id == "finops":
        return finops_configuration_options(profile, available_sources)
    tenant_id, project_id = profile["tenant_id"], profile["project_id"]
    source_types = ({"environment"} if service_id == "devops" else
                    {"repository", "repository_commit"} if service_id == "transform" else SOURCE_TYPES)
    complete = available_sources.get("coverage") == "complete" and available_sources.get("next_cursor") is None
    connections = authorized_connection_refs(available_sources, tenant_id, project_id)
    sources = []
    if complete:
        for item in available_sources.get("items", []):
            for reference in item.get("resources", []):
                if service_id in {"security", "well_architected"}:
                    reference = canonical_source(reference)
                if (_scoped_ref(reference, tenant_id, project_id)
                    and reference["record_type"] in source_types and reference not in sources):
                    sources.append(deepcopy(reference))
    selections = (profile.get("configuration") or {}).get("selected_capabilities") or []
    if service_id == "devops":
        selected = any(isinstance(selection, dict) and selection.get("capability") == "devops"
                       and selection.get("ownership_mode") != "not_applicable" for selection in selections)
        capabilities = (["investigation", "release_readiness", "release_verification", "prevention"]
                        if selected else [])
    elif service_id == "well_architected":
        selected = any(isinstance(selection, dict) and selection.get("capability") == "well_architected"
                       and selection.get("ownership_mode") != "not_applicable" for selection in selections)
        capabilities = ["architecture_review"] if selected else []
    elif service_id == "transform":
        capabilities = [capability for capability in ("refactor", "dependency_upgrade")
                        if any(isinstance(selection, dict) and selection.get("capability") == capability
                               and selection.get("ownership_mode") != "not_applicable" for selection in selections)]
    else:
        capabilities = [capability for capability in ("threat_model", "code_review")
                        if any(isinstance(selection, dict) and selection.get("capability") == capability
                               for selection in selections)]
    reasons = []
    if service_id == "well_architected":
        if not capabilities:
            reasons.append("capability_not_selected")
        if repository_pin_missing(sources):
            reasons.append("repository_revision_unpinned")
        reasons.append("provider_unqualified")
    if not complete:
        reasons.append("source_coverage_incomplete")
    if not connections and service_id != "transform":
        reasons.append("connection_revision_unavailable")
    if not sources:
        reasons.append("source_options_unavailable")
    if service_id == "transform":
        readiness = transform_capability_readiness({"capabilities": capabilities})
        reasons.extend(dict.fromkeys(gap["code"] for item in readiness for gap in item["gaps"]))
    elif not (complete and connections and available_sources.get("regions")):
        reasons.append("region_options_unavailable")
    options = {"connections": connections, "sources": sources,
               "regions": available_sources.get("regions", []) if complete and connections else [],
               "capabilities": capabilities, "unavailable_reasons": reasons}
    if service_id == "transform":
        options["service_id"] = "transform"
        options["readiness"] = readiness
    return options


def populate_settings(
    profile: dict, profile_ref: dict, available_sources: dict, choices: dict, service_id: str = "security",
    *, refreshing: bool = False,
) -> tuple[dict, list[str]]:
    """Inherit only currently authorized Profile references; never guess a connection."""
    if service_id in {"security", "well_architected"}:
        choices = canonical_choices(choices)
    source_types = ({"environment"} if service_id == "devops" else
                    {"repository", "repository_commit"} if service_id == "transform" else SOURCE_TYPES)
    if service_id == "devops" and not any(
        choice.get("capability") == "devops" and choice.get("ownership_mode") != "not_applicable"
        for choice in (profile.get("configuration") or {}).get("selected_capabilities") or []
    ):
        raise ValueError("DevOps must be explicitly selected")

    if service_id == "finops" and not any(
        isinstance(choice, dict) and choice.get("capability") == "finops"
        and choice.get("ownership_mode") != "not_applicable"
        for choice in (profile.get("configuration") or {}).get("selected_capabilities") or []
    ):
        raise ValueError("FinOps must be explicitly selected")
    tenant_id, project_id = profile["tenant_id"], profile["project_id"]
    if not _scoped_ref(profile_ref, tenant_id, project_id) or (
        profile_ref["record_type"] != "product"
        or profile_ref["record_id"] != profile["product_id"]
        or profile_ref["record_revision"] != str(profile["revision"])
    ):
        raise ValueError("profile_ref must identify the authorized Profile revision")
    if set(choices) - CHOICE_FIELDS:
        raise ValueError("unsupported configuration choice")
    if service_id == "finops":
        if choices.get("service_id") != "finops" or not isinstance(choices.get("finops"), dict):
            raise ValueError("FinOps settings and service_id are required")
        if "transform" in choices:
            raise ValueError("Transform choices require the Transform service")
        if "devops" in choices:
            raise ValueError("DevOps choices require the DevOps service")
        if "well_architected" in choices:
            raise ValueError("Well-Architected choices require the Well-Architected service")
        finops = choices["finops"]
        if set(finops) - {"billing_scope", "agent_mode", "native_agent_ref", "native_agent_display_name", "data_role_ref"}:
            raise ValueError("unsupported FinOps setting")
        if "agent_mode" in finops and finops["agent_mode"] not in ("create", "adopt"):
            raise ValueError("unsupported FinOps agent mode")
        if "native_agent_ref" in finops and (
            finops.get("agent_mode") != "adopt" or not isinstance(finops["native_agent_ref"], str)
            or not finops["native_agent_ref"].strip()
        ):
            raise ValueError("native_agent_ref requires an explicit adopt selection")
        if "native_agent_display_name" in finops and (not isinstance(finops["native_agent_display_name"], str)
                or not finops["native_agent_display_name"].strip() or len(finops["native_agent_display_name"]) > 256):
            raise ValueError("native_agent_display_name must be a bounded console selection")
        if "data_role_ref" in finops and (
            not isinstance(finops["data_role_ref"], str) or not finops["data_role_ref"].strip()
        ):
            raise ValueError("data_role_ref must be an explicit selection")
        scope = finops.get("billing_scope")
        if scope is not None and (
            not isinstance(scope, dict) or set(scope) != {"account_ids", "view_ref"}
            or not isinstance(scope["account_ids"], list) or not scope["account_ids"]
            or any(not isinstance(account, str) or not account.isascii() or not account.isdigit()
                   or len(account) != 12 for account in scope["account_ids"])
            or len(set(scope["account_ids"])) != len(scope["account_ids"])
            or not isinstance(scope["view_ref"], str) or not scope["view_ref"].strip()
        ):
            raise ValueError("billing_scope requires distinct account IDs and an explicit view")
    elif service_id == "devops":
        if choices.get("service_id") != "devops" or not isinstance(choices.get("devops"), dict):
            raise ValueError("DevOps settings and service_id are required")
    elif service_id == "well_architected":
        if choices.get("service_id") != "well_architected" or not isinstance(choices.get("well_architected"), dict):
            raise ValueError("Well-Architected settings and service_id are required")
        if "devops" in choices:
            raise ValueError("DevOps choices require the DevOps service")
        if "finops" in choices:
            raise ValueError("FinOps choices require the FinOps service")
    elif service_id == "transform":
        if choices.get("service_id") != "transform" or not isinstance(choices.get("transform"), dict):
            raise ValueError("Transform settings and service_id are required")
        if "devops" in choices or "finops" in choices or "well_architected" in choices:
            raise ValueError("other service settings are not Transform choices")
    elif "devops" in choices or "finops" in choices or "well_architected" in choices or "transform" in choices or "service_id" in choices:
        raise ValueError("service-specific choices require their selected service")
    overrides = choices.get("overrides", [])
    if not isinstance(overrides, list) or any(
        not isinstance(override, dict)
        or set(override) != {"field", "reason", "value"}
        or override["field"] not in {"purpose", "classification", "source_refs", "connection_ref", "region", "visibility"}
        or not isinstance(override["reason"], str) or not override["reason"].strip()
        or choices.get(override["field"]) != override["value"]
        for override in overrides
    ):
        raise ValueError("overrides must cite their explicit choices and reasons")
    override_fields = [override["field"] for override in overrides]
    if len(set(override_fields)) != len(override_fields) or any(
        field in choices and field not in override_fields
        for field in ("purpose", "classification", "source_refs", "connection_ref", "region")
    ):
        raise ValueError("explicit changes require a single reasoned override")
    if available_sources.get("coverage") != "complete" or available_sources.get("next_cursor") is not None:
        raise ValueError("authorized source listing is incomplete")

    authorized = set()
    for item in available_sources.get("items", []):
        for reference in item.get("resources", []):
            if service_id in {"security", "well_architected"}:
                reference = canonical_source(reference)
            if _scoped_ref(reference, tenant_id, project_id) and reference["record_type"] in source_types:
                authorized.add(_ref_key(reference))

    configuration = profile.get("configuration") or {}
    section = (configuration.get("advanced") if service_id == "devops" else configuration.get("basic")) or {}
    inherited_sources = []
    for field in (("environments",) if service_id == "devops" else () if service_id == "finops" else ("repositories", "documents")):
        for reference in section.get(field) or []:
            if service_id in {"security", "well_architected"}:
                reference = canonical_source(reference)
            if _scoped_ref(reference, tenant_id, project_id) and _ref_key(reference) in authorized and reference not in inherited_sources:
                inherited_sources.append(deepcopy(reference))
    if service_id == "devops" and not refreshing and len(inherited_sources) > 1 and "source_refs" not in choices:
        raise ValueError("multiple DevOps environments require explicit selection")

    selected_sources = choices.get("source_refs", inherited_sources)
    if not isinstance(selected_sources, list) or any(
        not _scoped_ref(reference, tenant_id, project_id) or _ref_key(reference) not in authorized
        for reference in selected_sources
    ):
        raise ValueError("source_refs must be currently authorized")
    if len(selected_sources) != len({_ref_key(reference) for reference in selected_sources}):
        raise ValueError("duplicate source_ref")
    if service_id == "transform":
        if len(selected_sources) != 1 or selected_sources[0]["record_type"] not in {"repository", "repository_commit"}:
            raise ValueError("Transform requires exactly one authorized repository")
        selected_capabilities = {item.get("capability") for item in configuration.get("selected_capabilities") or []
                                 if isinstance(item, dict) and item.get("ownership_mode") != "not_applicable"}
        if not set(choices.get("capabilities", [])).issubset(selected_capabilities):
            raise ValueError("Transform capability must be selected on the Product")
        try:
            TransformConfigurationDraftInput.model_validate({
                "service_id": "transform", "profile_ref": profile_ref, "source_refs": selected_sources,
                "capabilities": choices.get("capabilities"), "enabled": choices.get("enabled", False),
                "transform": choices["transform"],
            })
        except ValueError as error:
            raise ValueError("invalid Transform settings") from error
        transform = choices["transform"]
        for field, kind in (("connection_ref", "connection"),
                            ("executor_binding_ref", "executor_binding"),
                            ("test_policy_ref", "test_policy"), ("definition_ref", "transform_definition")):
            reference = transform.get(field)
            if reference is not None and (not _scoped_ref(reference, tenant_id, project_id)
                                          or reference["record_type"] != kind):
                raise ValueError(f"{field} must be a scoped {kind} reference")
        if transform["connection_ref"] not in authorized_connection_refs(available_sources, tenant_id, project_id):
            raise ValueError("Transform connection_ref must be currently authorized")
    if service_id == "devops":
        validate_devops_selection(choices["devops"], tenant_id, project_id, selected_sources,
                                  authorized_connection_refs(available_sources, tenant_id, project_id), enabled=choices.get("enabled") is True)
    elif service_id == "well_architected":
        if choices.get("capabilities") != ["architecture_review"] or not selected_sources:
            raise ValueError("architecture_review requires pinned sources")
        try:
            well_architected = WellArchitectedSettings.model_validate(choices["well_architected"])
        except ValueError as error:
            raise ValueError("invalid source-only review settings") from error
        authorized_refs = { _ref_key(reference) for item in available_sources.get("items", [])
                            for reference in item.get("resources", [])
                            if _scoped_ref(reference, tenant_id, project_id) }
        for reference in (*well_architected.goal_refs, well_architected.execution_role_ref,
                          well_architected.source_store_ref):
            if reference is not None and _ref_key(reference.model_dump()) not in authorized_refs:
                raise ValueError("review reference must be currently authorized")

    connection = choices.get("connection_ref")
    if connection is not None and (
        not _scoped_ref(connection, tenant_id, project_id)
        or connection not in authorized_connection_refs(available_sources, tenant_id, project_id)
    ):
        raise ValueError("connection_ref must be an explicit authorized choice")

    settings = {
        "profile_ref": deepcopy(profile_ref),
        "source_refs": deepcopy(selected_sources),
        "capabilities": deepcopy(choices.get("capabilities", [])),
        "enabled": choices.get("enabled", False),
        "visibility": choices.get("visibility", "only_me"),
        "exclusions": deepcopy(choices.get("exclusions", [])),
        "overrides": deepcopy(choices.get("overrides", [])),
        "triggers": deepcopy(choices.get("triggers", [])),
    }
    inherited_fields = []
    inherited = {
        "purpose": (profile.get("product") or {}).get("purpose"),
        "classification": ((profile.get("configuration") or {}).get("governance") or {}).get("data_classification"),
        "source_refs": inherited_sources,
    }
    for field, value in inherited.items():
        if field not in choices:
            if value is not None:
                settings[field] = deepcopy(value)
                inherited_fields.append(field)
        elif field != "source_refs":
            settings[field] = deepcopy(choices[field])
    for field in ("connection_ref", "region", "limits", "monitoring"):
        if field in choices:
            settings[field] = deepcopy(choices[field])
    if service_id == "devops":
        settings["service_id"] = "devops"
        settings["devops"] = deepcopy(choices["devops"])
    elif service_id == "finops":
        settings["service_id"] = "finops"
        settings["finops"] = deepcopy(choices["finops"])
    elif service_id == "well_architected":
        settings["service_id"] = "well_architected"
        settings["well_architected"] = deepcopy(choices["well_architected"])
    elif service_id == "transform":
        settings["service_id"] = "transform"
        settings["transform"] = deepcopy(choices["transform"])
    return settings, inherited_fields
