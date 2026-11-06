"""Generate the proposed contract artifacts; not a service implementation."""

import csv
import io
import json
from pathlib import Path

HOME = Path(__file__).resolve().parent


def ref(name):
    return {"$ref": f"#/$defs/{name}"}


def text(maximum=512):
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def enum(*values):
    return {"type": "string", "enum": list(values)}


def array(item, maximum=100):
    return {"type": "array", "items": item, "maxItems": maximum}


def obj(properties, optional=()):
    return {"type": "object", "additionalProperties": False, "properties": properties,
            "required": [key for key in properties if key not in optional]}


def schemas():
    definitions = {}
    definitions["Id"] = text(255)
    definitions["Revision"] = {**text(), "not": {"enum": ["latest", "null", "HEAD"]}}
    definitions["Instant"] = {"type": "string", "format": "date-time", "pattern": "Z$"}
    definitions["Hash"] = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    definitions["Version"] = {"type": "integer", "minimum": 1}
    definitions["ETag"] = {"type": "string", "pattern": '^"[!#-~]+"$', "maxLength": 128}
    definitions["Visibility"] = enum("only_me", "product_team")
    definitions["Capability"] = enum("threat_model", "code_review")
    definitions["DevopsCapability"] = enum("investigation", "release_readiness", "release_verification", "prevention")
    definitions["TransformCapability"] = enum("refactor", "dependency_upgrade")
    definitions["WellArchitectedCapability"] = enum("architecture_review")
    definitions["SharedCapability"] = enum(*definitions["Capability"]["enum"], *definitions["DevopsCapability"]["enum"], *definitions["TransformCapability"]["enum"], *definitions["WellArchitectedCapability"]["enum"], "cost_inquiry")
    definitions["ServiceId"] = enum("security", "devops", "transform", "well_architected", "finops")
    definitions["MonitoringPolicy"] = obj({"poll_interval_seconds": {"type": "integer", "minimum": 60, "maximum": 900, "default": 600}, "poll_grace_seconds": {"type": "integer", "minimum": 60, "maximum": 900, "default": 300}, "stall_after_seconds": {"type": "integer", "minimum": 600, "maximum": 86400}}, optional=("poll_interval_seconds", "poll_grace_seconds", "stall_after_seconds"))
    definitions["EffectiveMonitoringPolicy"] = {**definitions["MonitoringPolicy"], "required": ["poll_interval_seconds", "poll_grace_seconds"]}
    definitions["RecordRef"] = obj({key: ref("Id") for key in ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id")} | {"record_revision": ref("Revision")})
    definitions["TestProfileRef"] = obj({
        key: ref("Id") for key in ("tenant_id", "project_id", "source_connection_id")
    } | {"record_type": {"const": "test_profile"},
         "record_id": {**text(128), "pattern": "^[a-zA-Z0-9_.-]+$"},
         "record_revision": {**text(10), "pattern": "^[1-9][0-9]*$"}})
    definitions["Scope"] = obj({key: ref("Id") for key in ("tenant_id", "product_id", "project_id", "relationship_revision", "owner_id")} | {"visibility": ref("Visibility")})
    definitions["ArtifactRef"] = obj({"artifact_id": ref("Id"), "version": ref("Revision"), "sha256": ref("Hash"), "media_type": text(128), "bytes": {"type": "integer", "minimum": 0}})
    definitions["Limits"] = obj({"deadline_seconds": {"type": "integer", "minimum": 60, "maximum": 86400}, "max_provider_attempts": {"type": "integer", "minimum": 1, "maximum": 10}, "max_polls": {"type": "integer", "minimum": 1, "maximum": 2880}, "concurrency": {"type": "integer", "minimum": 1, "maximum": 10}, "max_cost": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]{1,6})?$"}, "currency": enum("USD"), "policy_ref": ref("RecordRef")})
    definitions["Trigger"] = obj({"trigger_id": ref("Id"), "kind": enum("manual", "pull_request", "document_revision", "schedule"), "enabled": {"type": "boolean"}, "capability": ref("SharedCapability"), "binding_id": ref("Id"), "source_refs": array(ref("RecordRef")), "schedule": text(128), "debounce_seconds": {"type": "integer", "minimum": 0, "maximum": 300}}, optional=("schedule", "capability", "binding_id"))
    definitions["Trigger"]["dependentRequired"] = {"capability": ["binding_id"], "binding_id": ["capability"]}
    definitions["Trigger"]["allOf"] = [
        {"if": {"properties": {"enabled": {"const": True}}}, "then": {"required": ["capability", "binding_id"], "properties": {"source_refs": {"minItems": 1}}}},
        {"if": {"properties": {"kind": {"const": "schedule"}}}, "then": {"required": ["schedule"]}, "else": {"not": {"required": ["schedule"]}}},
    ]
    override_types = {"purpose": text(10000), "classification": enum("public", "internal", "confidential", "restricted"),
                      "visibility": ref("Visibility"), "source_refs": array(ref("RecordRef")),
                      "connection_ref": ref("RecordRef"), "region": text(64)}
    definitions["Override"] = obj({"field": enum(*override_types), "reason": text(1000),
                                   "value": {"oneOf": [text(10000), array(ref("RecordRef")), ref("RecordRef")]}})
    definitions["Override"]["allOf"] = [
        {"if": {"properties": {"field": {"const": field}}}, "then": {"properties": {"value": value}}}
        for field, value in override_types.items()
    ]
    definitions["DevopsEnvironment"] = obj({"environment_ref": ref("RecordRef"), "workload_kind": enum("eks", "aws_resource"),
                                               "resource_refs": {**array(ref("RecordRef")), "minItems": 1},
                                               "cluster_ref": ref("RecordRef"), "namespace": text(255)}, optional=("cluster_ref", "namespace"))
    definitions["DevopsEnvironment"]["allOf"] = [
        {"if": {"properties": {"workload_kind": {"const": "aws_resource"}}},
         "then": {"not": {"anyOf": [{"required": ["cluster_ref"]}, {"required": ["namespace"]}]}}},
    ]
    definitions["DevopsAccount"] = obj({"connection_ref": ref("RecordRef"), "purpose": enum("source", "monitor"),
                                          "resource_role_ref": ref("RecordRef"),
                                          "environments": {**array(ref("DevopsEnvironment")), "minItems": 1}})
    definitions["DevopsIntegration"] = obj({"kind": enum("github", "cloudwatch"), "connection_ref": ref("RecordRef"),
                                              "access_mode": enum("read", "read_write"), "event_support": {"type": "boolean"},
                                              "repository_refs": {**array(ref("RecordRef")), "minItems": 1},
                                              "resource_refs": {**array(ref("RecordRef")), "minItems": 1},
                                              "association_ref": ref("RecordRef")},
                                             optional=("repository_refs", "resource_refs", "association_ref"))
    definitions["DevopsIntegration"]["allOf"] = [
        {"if": {"properties": {"kind": {"const": "github"}}},
         "then": {"not": {"required": ["resource_refs"]}},
         "else": {"not": {"required": ["repository_refs"]}}},
    ]
    definitions["DevopsMaintenanceWindow"] = obj({"starts_at": ref("Instant"), "ends_at": ref("Instant"),
                                                    "environment_refs": {**array(ref("RecordRef")), "minItems": 1}})
    definitions["DevopsIncidentSource"] = obj({"source_id": ref("Id"), "secret_ref": text(2048),
                                               "source_ref": ref("RecordRef"), "environment_ref": ref("RecordRef"),
                                               "trigger_id": ref("Id")})
    definitions["DevopsTriggerPolicy"] = obj({"trigger_id": ref("Id"), "binding_id": ref("Id"),
                                               "capability": ref("DevopsCapability"),
                                               "kind": enum("manual", "native_event", "schedule"),
                                               "enabled": {"type": "boolean"}, "paused": {"type": "boolean"},
                                               "source_refs": array(ref("RecordRef")),
                                               "resource_refs": array(ref("RecordRef")),
                                               "telemetry_refs": array(ref("RecordRef")),
                                               "test_profile_ref": ref("TestProfileRef"),
                                               "severities": {**array(text(32), 20), "minItems": 1},
                                               "environment_refs": {**array(ref("RecordRef")), "minItems": 1},
                                               "maintenance_windows": array(ref("DevopsMaintenanceWindow"))},
                                              optional=("trigger_id", "binding_id", "paused", "source_refs",
                                                        "resource_refs", "telemetry_refs", "severities",
                                                        "environment_refs", "maintenance_windows", "test_profile_ref"))
    definitions["DevopsTriggerPolicy"]["allOf"] = [
        {"if": {"properties": {"kind": {"const": "native_event"}}},
         "then": {"required": ["trigger_id", "binding_id", "resource_refs", "telemetry_refs", "severities", "environment_refs"]}},
        {"if": {"properties": {"capability": {"const": "release_verification"}}},
         "then": {"required": ["test_profile_ref"], "properties": {"kind": {"const": "manual"}}},
         "else": {"not": {"required": ["test_profile_ref"]}}},
    ]
    definitions["DevopsKnowledgeAgentType"] = enum("GENERIC", "CHAT", "INCIDENT_TRIAGE", "INCIDENT_RCA", "INCIDENT_MITIGATION", "INCIDENT_UI", "PREVENTION", "RELEASE_READINESS_REVIEW", "RELEASE_TESTING")
    definitions["DevopsSkillMetadata"] = obj({
        "name": {"type": "string", "pattern": "^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$"},
        "description": text(1024), "agent_types": {**array(ref("DevopsKnowledgeAgentType"), 9), "minItems": 1, "uniqueItems": True},
        "status": enum("ACTIVE", "INACTIVE"), "skill_type": {"const": "USER"}}, optional=("status", "skill_type"))
    definitions["DevopsKnowledgePublication"] = obj({
        "source_ref": ref("RecordRef"), "kind": enum("instruction", "skill"), "scope": enum("global", "agent"),
        "active": {"type": "boolean"}, "native_metadata": {"type": "object"}})
    definitions["DevopsKnowledgePublication"]["allOf"] = [
        {"if": {"properties": {"kind": {"const": "skill"}}},
         "then": {"properties": {"native_metadata": ref("DevopsSkillMetadata")}},
         "else": {"properties": {"active": {"const": True}, "native_metadata": obj({"agent_type": ref("DevopsKnowledgeAgentType")})}}},
    ]
    definitions["DevopsKnowledgePolicy"] = obj({"source_refs": array(ref("RecordRef")), "visibility": ref("Visibility"),
                                                  "publish_results": {"type": "boolean"}, "refresh": enum("manual", "on_change"),
                                                  "publications": {**array(ref("DevopsKnowledgePublication"), 20), "minItems": 1}}, optional=("publications",))
    definitions["DevopsKnowledgePolicy"]["allOf"] = [
        {"if": {"required": ["publications"]}, "then": {"properties": {"visibility": {"const": "product_team"}, "refresh": {"const": "manual"}}}},
    ]
    definitions["DevopsPreventionPolicy"] = obj({"goal_id": ref("Id"),
                                                    "expression": {"type": "string", "pattern": r"^rate\(([1-9]|[12][0-9]|30) days\)$"},
                                                    "timezone": {"const": "UTC"},
                                                    "missed_run_policy": {"const": "skip"}},
                                                   optional=("timezone", "missed_run_policy"))
    definitions["DevopsPreventionInput"] = obj({"configuration_revision": ref("Version"),
                                                   "action": enum("enable", "pause", "resume")})
    definitions["DevopsPreventionGoal"] = obj({"goal_id": ref("Id"),
                                                 "goal_type": enum("CUSTOMER_DEFINED", "ONCALL_REPORT"),
                                                 "state": enum("ENABLED", "DISABLED"),
                                                 "expression": text(255),
                                                 "last_task_id": {"oneOf": [ref("Id"), {"type": "null"}]}})
    definitions["DevopsPreventionSchedule"] = obj({"enabled": {"type": "boolean"},
                                                     "goals": array(ref("DevopsPreventionGoal"))})
    definitions["DevopsPreventionDesired"] = obj({"enabled": {"type": "boolean"},
                                                    "goal_id": ref("Id"), "expression": text(255),
                                                    "timezone": {"const": "UTC"},
                                                    "missed_run_policy": {"const": "skip"},
                                                    "per_run_limits": {"oneOf": [ref("Limits"), {"type": "null"}]},
                                                    "cost_control": {"oneOf": [{"const": "observation_only"}, {"type": "null"}]}},
                                                   optional=("goal_id", "expression", "timezone",
                                                             "missed_run_policy", "per_run_limits", "cost_control"))
    definitions["DevopsPreventionStatus"] = obj({"binding_id": ref("Id"),
                                                   "configuration_revision": ref("Version"),
                                                   "desired": ref("DevopsPreventionDesired"),
                                                   "observed": {"oneOf": [ref("DevopsPreventionSchedule"), {"type": "null"}]},
                                                   "status": enum("ready", "blocked"),
                                                   "reason": {"oneOf": [enum(
                                                       "prevention_readback_missing", "native_schedule_unqualified",
                                                       "prevention_state_mismatch", "prevention_cadence_mismatch",
                                                       "prevention_change_pending", "native_schedule_readback_unavailable"),
                                                       {"type": "null"}]}})
    definitions["DevopsSettings"] = obj({"hosting_connection_ref": ref("RecordRef"), "region": text(64),
                                           "space_mode": enum("create", "adopt"), "existing_space_id": ref("Id"),
                                           "accounts": array(ref("DevopsAccount")), "integrations": array(ref("DevopsIntegration")),
                                           "access_policy_ref": ref("RecordRef"), "authentication_mode": enum("scoped_iam_federation"),
                                           "permitted_native_actions": array(text(128)), "knowledge_policy": ref("DevopsKnowledgePolicy"),
                                           "trigger_policies": array(ref("DevopsTriggerPolicy"), 20),
                                           "incident_sources": array(ref("DevopsIncidentSource")),
                                           "paused": {"type": "boolean"},
                                           "prevention_enabled": {"type": "boolean"}, "prevention_policy": ref("DevopsPreventionPolicy"),
                                           "retention_days": {"type": "integer", "minimum": 1, "maximum": 3650},
                                           "browser_launch_policy": enum("disabled", "scoped_session")}, optional=("existing_space_id", "incident_sources", "paused", "prevention_policy"))
    definitions["DevopsSettings"]["allOf"] = [
        {"if": {"properties": {"space_mode": {"const": "adopt"}}},
         "then": {"required": ["existing_space_id"]}, "else": {"not": {"required": ["existing_space_id"]}}},
    ]
    definitions["WellArchitectedSourceRef"] = {"oneOf": [ref("DocumentSourceRef"), ref("RepositorySourceRef")]}
    definitions["WellArchitectedSettings"] = obj({
        "review_mode": {"const": "source_only"},
        "pillars": {"type": "array", "items": enum("COST_OPTIMIZATION", "SECURITY", "RESILIENCE", "PERFORMANCE", "OPERATIONAL_EXCELLENCE"),
                    "minItems": 1, "maxItems": 5, "uniqueItems": True},
        "goal_refs": array(ref("DocumentSourceRef")),
        "execution_role_ref": ref("RecordRef"),
        "source_store_ref": ref("RecordRef"),
    }, optional=("goal_refs", "execution_role_ref", "source_store_ref"))
    definitions["DevopsReadinessKey"] = enum(*definitions["DevopsCapability"]["enum"], "native_ui", "knowledge")
    definitions["ReadinessReason"] = enum(*("needs_" + field for field in (
        "space", "membership", "qualified_session", "account", "resources", "telemetry",
        "cluster_ref", "namespace", "network_access", "repository", "base_revision", "head_revision",
        "native_eligibility", "source_deployment_link", "isolated_target", "credential_ref",
        "scenarios", "verified_schedule", "explicit_enablement", "entitled_sources",
        "publication_policy", "retrieval_evidence")), "unsupported_connector", "provider_unqualified", "stale_readback")
    definitions["CapabilityReadiness"] = obj({"capability": ref("DevopsReadinessKey"),
                                                "state": enum("ready", "needs_input", "verifying", "blocked", "degraded"),
                                                "reason_codes": array(ref("ReadinessReason")), "configuration_revision": ref("Version"),
                                                "binding_generation": ref("Version"), "observed_at": ref("Instant"),
                                                "valid_until": ref("Instant")}, optional=("valid_until",))
    definitions["CapabilityReadiness"]["allOf"] = [
        {"if": {"properties": {"state": {"const": "ready"}}},
         "then": {"required": ["valid_until"], "properties": {"reason_codes": {"maxItems": 0}}}},
        {"if": {"properties": {"state": {"enum": ["needs_input", "blocked", "degraded"]}}},
         "then": {"properties": {"reason_codes": {"minItems": 1}}}},
    ]
    definitions["ConfigurationInput"] = obj({"profile_ref": ref("RecordRef"), "source_refs": array(ref("RecordRef")), "connection_ref": ref("RecordRef"), "region": text(64), "capabilities": {**array(ref("SharedCapability"), 2), "minItems": 1, "uniqueItems": True}, "enabled": {"type": "boolean"}, "visibility": ref("Visibility"), "exclusions": array(text(1024)), "overrides": array(ref("Override")), "triggers": array(ref("Trigger"), 20), "limits": ref("Limits")})
    definitions["ConfigurationInput"]["properties"].update({"purpose": text(10000), "classification": enum("public", "internal", "confidential", "restricted")})
    definitions["ConfigurationInput"]["properties"]["monitoring"] = ref("MonitoringPolicy")
    definitions["ConfigurationInput"]["properties"].update({"service_id": ref("ServiceId"), "devops": ref("DevopsSettings"), "well_architected": ref("WellArchitectedSettings")})
    definitions["ConfigurationInput"]["required"] = [field for field in definitions["ConfigurationInput"]["required"] if field not in {"connection_ref", "region", "limits"}]
    definitions["ConfigurationInput"]["properties"]["capabilities"]["maxItems"] = 4
    definitions["ConfigurationInput"]["allOf"] = [
        {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
         "then": {"required": ["devops"], "not": {"required": ["well_architected"]},
                  "properties": {"capabilities": {"items": ref("DevopsCapability")},
                                                       "triggers": {"items": {"properties": {"capability": ref("DevopsCapability")}}}}},
         "else": {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "well_architected"}}},
                  "then": {"required": ["well_architected"], "not": {"required": ["devops"]},
                           "properties": {"capabilities": {"items": ref("WellArchitectedCapability"), "maxItems": 1},
                                          "source_refs": {"items": ref("WellArchitectedSourceRef"), "minItems": 1},
                                          "triggers": {"items": {"properties": {"capability": ref("WellArchitectedCapability"),
                                                                                  "source_refs": {"items": ref("WellArchitectedSourceRef")}}}}}},
                  "else": {"not": {"anyOf": [{"required": ["devops"]}, {"required": ["well_architected"]}]},
                           "properties": {"capabilities": {"items": ref("Capability"), "maxItems": 2},
                                          "triggers": {"items": {"properties": {"capability": ref("Capability")}}}}}}},
    ]
    definitions["TransformTarget"] = obj({"kind": enum("dependency", "runtime"), "name": ref("Id"), "version": ref("Id")})
    definitions["TransformGoal"] = obj({"kind": ref("TransformCapability"), "summary": text(4000),
                                        "target": ref("TransformTarget")}, optional=("target",))
    definitions["TransformGoal"]["allOf"] = [
        {"if": {"properties": {"kind": {"const": "dependency_upgrade"}}},
         "then": {"required": ["target"]}, "else": {"not": {"required": ["target"]}}},
    ]
    definitions["TransformSettings"] = obj({"connection_ref": ref("RecordRef"),
        "executor_binding_ref": ref("RecordRef"), "test_policy_ref": ref("RecordRef"),
        "definition_ref": ref("RecordRef"), "region": ref("Id"),
        "read_paths": {**array(text(1024)), "minItems": 1},
        "write_paths": {**array(text(1024)), "minItems": 1},
        "excluded_paths": array(text(1024)), "goal_defaults": ref("TransformGoal")}, optional=("definition_ref",))
    definitions["TransformDraftSettings"] = {**definitions["TransformSettings"],
        "required": [field for field in definitions["TransformSettings"]["required"]
                     if field not in ("executor_binding_ref", "test_policy_ref")],
        "properties": {**definitions["TransformSettings"]["properties"],
                       "executor_binding_ref": {"oneOf": [ref("RecordRef"), {"type": "null"}]},
                       "test_policy_ref": {"oneOf": [ref("RecordRef"), {"type": "null"}]}}}
    definitions["TransformConfigurationInput"] = obj({"service_id": {"const": "transform"},
        "profile_ref": ref("RecordRef"), "source_refs": {**array(ref("RepositorySourceRef")), "minItems": 1},
        "capabilities": {**array(ref("TransformCapability"), 2), "minItems": 1, "uniqueItems": True},
        "enabled": {"type": "boolean"}, "transform": ref("TransformSettings")})
    definitions["TransformConfigurationDraftInput"] = {**definitions["TransformConfigurationInput"],
        "properties": {**definitions["TransformConfigurationInput"]["properties"],
                       "transform": ref("TransformDraftSettings")}}
    definitions["LegacyConfigurationInput"] = definitions.pop("ConfigurationInput")
    definitions["LegacyConfigurationInput"]["allOf"].append(
        {"not": {"required": ["service_id"], "properties": {"service_id": {"const": "transform"}}}})
    definitions["LegacyConfigurationInput"]["allOf"].append(
        {"not": {"required": ["service_id"], "properties": {"service_id": {"const": "finops"}}}})
    definitions["ConfigurationInput"] = {"oneOf": [ref("LegacyConfigurationInput"), ref("TransformConfigurationDraftInput"), ref("FinopsConfigurationInput")]}
    definitions["Configuration"] = obj({"configuration_id": ref("Id"), "revision": ref("Version"), "scope": ref("Scope"), "state": enum("draft", "needs_input", "ready", "disabled"), "settings": ref("ConfigurationInput"), "inherited_fields": array(text(255)), "gaps": array(text(255)), "handoff_state": enum("pending", "linked", "conflict", "unavailable"), "hash": ref("Hash")})
    definitions["RefreshInput"] = obj({"profile_ref": ref("RecordRef")})
    definitions["TransformReadinessGap"] = obj({"code": text(255), "field": text(255),
        "owner": enum("product", "executor"), "affected_step": enum("test", "execute")})
    definitions["TransformCapabilityReadiness"] = obj({"capability": ref("TransformCapability"),
        "status": enum("ready", "missing_input", "denied", "unsupported", "unqualified", "stale"),
        "gaps": array(ref("TransformReadinessGap"))})
    definitions["Check"] = obj({"ready": {"type": "boolean"}, "gaps": array(text(255)), "provider_contacted": {"const": False},
        "readiness": array(ref("TransformCapabilityReadiness"))}, optional=("readiness",))
    definitions["Options"] = obj({"connections": array(ref("RecordRef")), "sources": array(ref("RecordRef")), "regions": array(text(64)), "capabilities": array(ref("SharedCapability"), 4), "unavailable_reasons": array(text(255))})
    definitions["Options"]["properties"]["service_id"] = ref("ServiceId")
    definitions["Options"]["properties"]["readiness"] = array(ref("TransformCapabilityReadiness"))
    definitions["Options"]["allOf"] = [
        {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
         "then": {"properties": {"capabilities": {"items": ref("DevopsCapability")}}},
         "else": {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "well_architected"}}},
                  "then": {"properties": {"capabilities": {"items": ref("WellArchitectedCapability"), "maxItems": 1}}},
                  "else": {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "transform"}}},
                           "then": {"properties": {"capabilities": {"items": ref("TransformCapability"), "maxItems": 2}}},
                           "else": {"properties": {"capabilities": {"items": ref("Capability"), "maxItems": 2}}}}}},
    ]
    definitions["BindingAction"] = enum("create", "link", "check", "update", "suspend", "resume", "unlink", "cleanup", "sync_knowledge")
    definitions["BindingInput"] = obj({"action": ref("BindingAction"), "configuration_revision": ref("Version"), "binding_id": ref("Id"), "native_resource_ref": ref("RecordRef"), "owned_cleanup_confirmed": {"const": True}, "binding_revision": ref("Version"), "binding_generation": ref("Version")}, optional=("binding_id", "native_resource_ref", "owned_cleanup_confirmed", "binding_revision", "binding_generation"))
    definitions["BindingInput"]["allOf"] = [
        {"if": {"properties": {"action": {"const": "link"}}}, "then": {"required": ["native_resource_ref"]}},
        {"if": {"properties": {"action": {"enum": ["check", "update", "suspend", "resume", "unlink", "cleanup", "sync_knowledge"]}}}, "then": {"required": ["binding_id"]}},
        {"if": {"properties": {"action": {"const": "cleanup"}}}, "then": {"required": ["owned_cleanup_confirmed"]}},
    ]
    definitions["BindingInput"]["allOf"].append(
        {"if": {"properties": {"action": {"const": "sync_knowledge"}}},
         "then": {"required": ["binding_revision", "binding_generation"], "not": {"anyOf": [{"required": ["native_resource_ref"]}, {"required": ["owned_cleanup_confirmed"]}]}},
         "else": {"not": {"anyOf": [{"required": ["binding_revision"]}, {"required": ["binding_generation"]}]}}})
    definitions["Binding"] = obj({"binding_id": ref("Id"), "scope": ref("Scope"), "configuration_id": ref("Id"), "revision": ref("Version"), "generation": ref("Version"), "state": enum("unconfigured", "checking", "provisioning", "ready", "blocked", "outcome_unknown", "suspended", "unlinking", "unlinked", "cleanup_pending", "cleaned", "cleanup_failed"), "ownership": enum("adp_created", "customer_linked"), "native_resource_ref": ref("RecordRef"), "connection_ref": ref("RecordRef"), "gaps": array(text(255))}, optional=("native_resource_ref",))
    definitions["Configuration"]["properties"]["service_id"] = ref("ServiceId")
    definitions["Binding"]["properties"].update({"service_id": ref("ServiceId"), "readiness": array(ref("CapabilityReadiness"))})
    definitions["DevopsAsset"] = obj({"asset_id": ref("Id"), "binding_id": ref("Id"), "scope": ref("Scope"),
                                       "ownership": enum("adp_created", "adopted", "shared"),
                                       "state": enum("active", "suspended", "retained", "removed", "outcome_unknown"),
                                       "native_resource_ref": ref("RecordRef"), "ownership_receipt": ref("ArtifactRef")},
                                      optional=("ownership_receipt",))
    definitions["DevopsAsset"]["allOf"] = [
        {"if": {"properties": {"state": {"const": "removed"}}},
         "then": {"properties": {"ownership": {"const": "adp_created"}}, "required": ["ownership_receipt"]}},
    ]
    definitions["Binding"]["properties"]["assets"] = array(ref("DevopsAsset"))
    definitions["Operation"] = obj({"operation_id": ref("Id"), "action": ref("BindingAction"), "state": enum("accepted", "running", "waiting", "succeeded", "blocked", "failed", "outcome_unknown", "cancelled", "abandoned"), "binding_id": ref("Id"), "revision": ref("Version"), "gaps": array(text(255))}, optional=("binding_id",))
    definitions["Operation"]["properties"].update({"service_id": ref("ServiceId"), "readback_ref": ref("RecordRef"), "next_action": text(1000)})
    definitions["Operation"]["allOf"] = [
        {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"},
                                                        "state": {"const": "succeeded"}}},
         "then": {"required": ["readback_ref"]}},
        {"if": {"properties": {"state": {"const": "outcome_unknown"}}},
         "then": {"not": {"required": ["readback_ref"]}}},
    ]
    definitions["SetupAuthorization"] = obj({"actor_id": ref("Id"), "delegation_id": ref("Id"), "delegation_revision": ref("Version"), "authorized_at": ref("Instant")})
    definitions["SetupIntent"] = obj({"operation_id": ref("Id"), "scope": ref("Scope"), "configuration_id": ref("Id"), "request": ref("BindingInput"), "binding_generation": ref("Version"), "authorization": ref("SetupAuthorization"), "owned_resource_receipt": ref("ArtifactRef"), "intent_hash": ref("Hash")}, optional=("binding_generation", "owned_resource_receipt"))
    definitions["SetupIntent"]["allOf"] = [
        {"if": {"properties": {"request": {"properties": {"action": {"enum": ["check", "update", "suspend", "resume", "unlink", "cleanup", "sync_knowledge"]}}}}}, "then": {"required": ["binding_generation"]}},
        {"if": {"properties": {"request": {"properties": {"action": {"const": "cleanup"}}}}}, "then": {"required": ["owned_resource_receipt"]}},
    ]
    definitions["WorkerAttempt"] = obj({"attempt_id": ref("Id"), "lease_fence": ref("Version"), "generation": ref("Version"), "deadline_at": ref("Instant")})
    definitions["DocumentSourceRef"] = obj({**definitions["RecordRef"]["properties"], "record_type": {"const": "document"}})
    definitions["RepositorySourceRef"] = obj({**definitions["RecordRef"]["properties"], "record_type": {"const": "repository_commit"}})
    definitions["DevopsInvestigationInput"] = obj({"kind": {"const": "investigation"},
                                                    "resource_refs": {**array(ref("RecordRef")), "minItems": 1},
                                                    "telemetry_refs": array(ref("RecordRef")), "question": text(4000)})
    definitions["DevopsReadinessInput"] = obj({"kind": {"const": "release_readiness"},
                                                "repository_ref": ref("RecordRef"), "base_revision": ref("Revision"),
                                                "head_revision": ref("Revision"), "eligibility_ref": ref("RecordRef")},
                                               optional=("eligibility_ref",))
    definitions["DevopsVerificationInput"] = obj({"kind": {"const": "release_verification"},
                                                   "source_ref": ref("RecordRef"), "deployment_ref": ref("RecordRef"),
                                                   "target_ref": ref("RecordRef"), "credential_ref": ref("RecordRef"),
                                                   "test_profile_ref": ref("TestProfileRef"),
                                                   "scenarios": {**array(ref("ArtifactRef"), 20), "minItems": 1}})
    definitions["HistoricalDevopsVerificationInput"] = obj({
        key: value for key, value in definitions["DevopsVerificationInput"]["properties"].items()
        if key != "test_profile_ref"})
    definitions["StoredRunInput"] = {"oneOf": [ref("RunInput"), obj({
        "service_id": {"const": "devops"}, "configuration_revision": ref("Version"),
        "binding_id": ref("Id"), "capability": {"const": "release_verification"},
        "devops_input": ref("HistoricalDevopsVerificationInput")})]}
    definitions["DevopsRunOptions"] = {"oneOf": [ref(name) for name in
                                             ("DevopsInvestigationInput", "DevopsReadinessInput", "DevopsVerificationInput")]}
    definitions["RunInput"] = obj({"configuration_revision": ref("Version"), "binding_id": ref("Id"), "capability": ref("SharedCapability"), "sources": {**array(ref("RecordRef")), "minItems": 1}, "pr_base": ref("Revision"), "pr_head": ref("Revision"), "parent_task_ref": ref("RecordRef")}, optional=("pr_base", "pr_head", "parent_task_ref"))
    definitions["RunInput"]["properties"].update({"service_id": ref("ServiceId"), "devops_input": ref("DevopsRunOptions")})
    definitions["RunInput"]["required"].remove("sources")
    definitions["RunInput"]["dependentRequired"] = {"pr_base": ["pr_head"], "pr_head": ["pr_base"]}
    definitions["RunInput"]["allOf"] = [
        {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
         "then": {"required": ["devops_input"],
                  "properties": {"capability": {"enum": ["investigation", "release_readiness", "release_verification"]}},
                  "not": {"anyOf": [{"required": ["pr_base"]}, {"required": ["pr_head"]}]}},
         "else": {"required": ["sources"], "not": {"required": ["devops_input"]},
                  "if": {"required": ["service_id"], "properties": {"service_id": {"const": "well_architected"}}},
                  "then": {"properties": {"capability": ref("WellArchitectedCapability"),
                                         "sources": {"minItems": 1, "items": ref("WellArchitectedSourceRef")}}},
                  "else": {"properties": {"capability": ref("Capability"), "sources": {"minItems": 1}}}}},
        {"if": {"properties": {"capability": {"const": "threat_model"}}},
         "then": {"properties": {"sources": {"items": ref("DocumentSourceRef")}}, "not": {"anyOf": [{"required": ["pr_base"]}, {"required": ["pr_head"]}]}},
         "else": {"if": {"properties": {"capability": {"const": "code_review"}}},
                  "then": {"properties": {"sources": {"items": ref("RepositorySourceRef")}}}}},
        *({"if": {"properties": {"capability": {"const": capability}}},
           "then": {"properties": {"devops_input": ref(input_name)}}}
          for capability, input_name in (("investigation", "DevopsInvestigationInput"),
                                         ("release_readiness", "DevopsReadinessInput"),
                                         ("release_verification", "DevopsVerificationInput"))),
    ]
    definitions["TransformRunOptions"] = obj({"goal": ref("TransformGoal")})
    definitions["TransformRunInput"] = obj({"service_id": {"const": "transform"},
        "configuration_revision": ref("Version"), "binding_id": ref("Id"),
        "capability": ref("TransformCapability"),
        "sources": {**array(ref("RepositorySourceRef"), 1), "minItems": 1},
        "transform_input": ref("TransformRunOptions"), "parent_task_ref": ref("RecordRef")},
        optional=("parent_task_ref",))
    definitions["TransformRunInput"]["allOf"] = [
        {"if": {"properties": {"capability": {"const": capability}}},
         "then": {"properties": {"transform_input": {"properties": {"goal": {"properties": {"kind": {"const": capability}}}}}}}}
        for capability in definitions["TransformCapability"]["enum"]]
    definitions["LegacyRunInput"] = definitions.pop("RunInput")
    definitions["LegacyRunInput"]["allOf"].append(
        {"not": {"required": ["service_id"], "properties": {"service_id": {"enum": ["transform", "finops"]}}}})
    definitions["RunInput"] = {"oneOf": [ref("LegacyRunInput"), ref("TransformRunInput")]}
    definitions["VerificationScenarioSpec"] = obj({
        "artifact": ref("ArtifactRef"), "max_requests": {"type": "integer", "minimum": 1, "maximum": 20},
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 300},
        "network_scope": ref("Id"), "positive_expected": text(128), "negative_expected": text(128),
        "cleanup_ref": ref("RecordRef")})
    definitions["VerificationObservation"] = obj({
        "source_ref": ref("RecordRef"), "deployment_ref": ref("RecordRef"),
        "target_ref": ref("RecordRef"), "credential_ref": ref("RecordRef"),
        "deployed_sha": ref("Revision"), "isolated": {"const": True}, "admin": {"const": False},
        "network_scope": ref("Id"), "deployment_adapter": ref("Id"),
        "checked_at": ref("Instant"), "expires_at": ref("Instant"),
        "scenario_specs": {**array(ref("VerificationScenarioSpec"), 20), "minItems": 1}})
    definitions["VerificationCleanup"] = obj({
        "fixture_ref": ref("RecordRef"), "state": {"const": "absent"},
        "owner_run_id": ref("Id"), "deletion_token": ref("Hash")})
    definitions["VerificationCheck"] = obj({
        "expected_outcome": text(128), "observed_outcome": text(128),
        "evidence_ref": ref("ArtifactRef")})
    definitions["VerificationScenarioResult"] = obj({
        "scenario_id": ref("Id"), "status": enum("passed", "failed"),
        "evidence_ref": ref("ArtifactRef"), "positive": ref("VerificationCheck"),
        "negative": ref("VerificationCheck"), "deployment_ref": ref("RecordRef"),
        "deployed_sha": ref("Revision")},
        optional=("positive", "negative", "deployment_ref", "deployed_sha"))
    definitions["Admission"] = obj({"scope": ref("Scope"), "profile_ref": ref("RecordRef"), "connection_ref": ref("RecordRef"), "delegation_id": ref("Id"), "delegation_version": ref("Version"), "binding_generation": ref("Version"), "actor_id": ref("Id"), "configuration_hash": ref("Hash"), "adapter_revision": ref("Revision"), "limits": ref("Limits"), "visibility": ref("Visibility"), "classification": enum("public", "internal", "confidential", "restricted"), "event_id": ref("Id"), "causal_origin": ref("Id"), "admitted_at": ref("Instant"), "expires_at": ref("Instant"), "exclusions": array(text(1024))}, optional=("event_id",))
    definitions["Admission"]["properties"].update({"native_space_ref": ref("RecordRef"),
                                                    "native_model_ref": ref("RecordRef"),
                                                     "provider_identity": ref("ProviderIdentity")})
    definitions["Admission"]["properties"]["monitoring_policy"] = ref("EffectiveMonitoringPolicy")
    definitions["Admission"]["properties"].update({"configuration_id": ref("Id"),
                                                 "configuration_revision": ref("Version")})
    definitions["Admission"]["required"].append("monitoring_policy")
    definitions["FileEntry"] = obj({"source_ref": ref("RecordRef"), "path": text(1024), "sha256": ref("Hash"), "bytes": {"type": "integer", "minimum": 0}})
    definitions["FileEntry"]["properties"]["mode"] = enum("100644", "100755")
    definitions["SourceSnapshotRequest"] = obj({"delegation_id": ref("Id"), "delegation_revision": ref("Version"), "source_ref": ref("RecordRef"), "exclusions": array(text(1024)), "max_bytes": {"type": "integer", "minimum": 1, "maximum": 1073741824}, "max_files": {"type": "integer", "minimum": 1, "maximum": 100000}, "retain_until": ref("Instant")})
    definitions["SourceSnapshot"] = obj({"snapshot_id": ref("Id"), "source_ref": ref("RecordRef"), "content": ref("ArtifactRef"), "file_manifest": ref("ArtifactRef"), "file_count": {"type": "integer", "minimum": 1, "maximum": 100000}, "exclusions": array(text(1024)), "expires_at": ref("Instant")})
    definitions["SourceFileManifest"] = obj({"source_ref": ref("RecordRef"), "files": {**array(ref("FileEntry"), 100000), "minItems": 1}, "exclusions": array(text(1024))})
    definitions["SourceReadRequest"] = obj({"delegation_id": ref("Id"), "delegation_revision": ref("Version"), "operation_id": ref("Id"), "attempt_id": ref("Id"), "lease_fence": ref("Version")})
    definitions["Admission"]["properties"].update({"source_snapshots": array(ref("SourceSnapshot")),
                                                     "service_id": ref("ServiceId"),
                                                     "verification": ref("VerificationObservation")})
    definitions["Admission"]["required"].append("source_snapshots")
    definitions["Admission"]["allOf"] = [
        {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
         "else": {"properties": {"source_snapshots": {"minItems": 1}}}},
    ]
    definitions["Manifest"] = obj({"schema_version": {"const": "1.0"}, "scope": ref("Scope"), "run_id": ref("Id"), "configuration_revision": ref("Version"), "configuration_hash": ref("Hash"), "binding_generation": ref("Version"), "profile_ref": ref("RecordRef"), "sources": array(ref("RecordRef")), "files": array(ref("FileEntry"), 100000), "archive": ref("ArtifactRef"), "coverage_mode": enum("documents", "full_snapshot", "selected_files"), "exclusions": array(text(1024)), "materializer_revision": ref("Revision"), "adapter_revision": ref("Revision"), "sealed_at": ref("Instant")})
    definitions["Run"] = obj({"run_id": ref("Id"), "scope": ref("Scope"), "revision": ref("Version"), "input": ref("RunInput"), "state": enum("admitted", "materializing", "queued", "submitting", "running", "collecting", "succeeded", "partial", "failed", "cancel_requested", "cancelled", "outcome_unknown", "expired", "blocked"), "provider_state": enum("not_started", "running", "stopped", "completed", "failed", "unknown"), "manifest_ref": ref("ArtifactRef"), "native_job_ref": ref("RecordRef"), "deadline_at": ref("Instant"), "publication_id": ref("Id"), "retry_of": ref("Id")}, optional=("manifest_ref", "native_job_ref", "publication_id", "retry_of"))
    definitions["WellArchitectedSourceManifest"] = obj({
        "archive": ref("ArtifactRef"), "provider_source_uri": {**text(2048), "pattern": "^s3://"},
        "provider_coverage": enum("verified", "provider_coverage_unverified"),
        "architecture": obj({"schema_version": {"const": "1.0"},
            "sources": array(obj({"source_ref": ref("WellArchitectedSourceRef"), "snapshot_id": ref("Id"),
                "revision": ref("Revision"), "selected_paths": array(text(1024), 100000),
                "selected_hashes": {"type": "object", "additionalProperties": ref("Hash")},
                "unresolved": array(text(2048), 100000)}), 100000),
            "files": array(obj({"source_ref": ref("WellArchitectedSourceRef"), "snapshot_id": ref("Id"),
                "revision": ref("Revision"), "path": text(1024), "sha256": ref("Hash"),
                "bytes": {"type": "integer", "minimum": 0}, "entry_point": {"type": "boolean"},
                "component": {"type": ["string", "null"]}, "exclusion": {"type": ["string", "null"]},
                "archive_path": {"type": ["string", "null"]}}), 100000)})})
    definitions["Run"]["properties"] = {"source_manifest": ref("WellArchitectedSourceManifest"),
                                         **definitions["Run"]["properties"]}
    definitions["Run"]["properties"].update({"service_id": ref("ServiceId"),
                                                 "origin": enum("adp_submitted", "native"),
                                                 "native_event_ref": ref("RecordRef"),
                                                 "provider_readback_ref": ref("ArtifactRef"),
                                                 "attribution": enum("observed", "unverified")})
    definitions["MonitoringError"] = obj({"code": text(128), "message": text(1000)}, optional=("code", "message"))
    definitions["MonitoringError"]["properties"].update({
        "action": text(1000), "next_action": text(1000), "stage": text(128), "cause": text(128),
        "http_status": {"type": "integer", "minimum": 100, "maximum": 599},
    })
    definitions["MonitoringError"]["anyOf"] = [{"required": ["code"]}, {"required": ["message"]}]
    definitions["PhaseObservation"] = obj({"name": text(128), "native_status": text(128), "provider_updated_at": ref("Instant")}, optional=("native_status", "provider_updated_at"))
    definitions["TaskSummary"] = obj({key: {"type": "integer", "minimum": 0} for key in ("running", "completed", "failed", "stopped", "unknown")})
    definitions["ProviderObservation"] = obj({"observed_at": ref("Instant"), "native_job_ref": ref("RecordRef"), "native_status": text(128), "outcome": enum("running", "completed", "failed", "stopped", "unknown"), "provider_updated_at": ref("Instant"), "execution_started_at": ref("Instant"), "execution_ended_at": ref("Instant"), "phases": array(ref("PhaseObservation"), 20), "tasks": ref("TaskSummary"), "progress_coverage": enum("complete", "partial", "unavailable"), "progress_fingerprint": ref("Hash"), "error": ref("MonitoringError"), "details_ref": ref("ArtifactRef")}, optional=("native_status", "provider_updated_at", "execution_started_at", "execution_ended_at", "phases", "tasks", "progress_fingerprint", "error", "details_ref"))
    definitions["RunMonitoring"] = obj({"state": enum("not_started", "current", "overdue", "degraded", "finished", "recovery_pending"), "progress_state": enum("unknown", "observed", "suspected_stalled"), "poll_attempts": {"type": "integer", "minimum": 0}, "consecutive_poll_errors": {"type": "integer", "minimum": 0}, "last_poll_attempt_at": ref("Instant"), "last_successful_poll_at": ref("Instant"), "last_progress_at": ref("Instant"), "next_poll_at": ref("Instant"), "last_poll_error": ref("MonitoringError"), "latest_observation": ref("ProviderObservation")}, optional=("last_poll_attempt_at", "last_successful_poll_at", "last_progress_at", "next_poll_at", "last_poll_error", "latest_observation"))
    definitions["Run"]["properties"]["monitoring"] = ref("RunMonitoring")
    definitions["RunMonitoring"]["properties"]["manual_recovery_attempts"] = {"type": "integer", "minimum": 0}
    definitions["RunMonitoring"]["properties"]["progress_baseline_at"] = ref("Instant")
    definitions["Run"]["properties"]["admission"] = ref("Admission")
    # Readback retains historical accepted input. Submit operations continue to
    # use only RunInput; an old shape cannot authorize a new native effect.
    definitions["Run"]["properties"]["input"] = ref("StoredRunInput")
    definitions["Run"]["required"].remove("input")
    definitions["Run"]["allOf"] = [
        {"if": {"required": ["origin"], "properties": {"origin": {"const": "native"}}},
         "then": {"required": ["service_id", "attribution"],
                  "properties": {"service_id": {"const": "devops"},
                                 "state": {"enum": ["running", "collecting", "succeeded", "partial", "failed",
                                                     "outcome_unknown", "expired", "blocked", "cancel_requested", "cancelled"]}},
                  "not": {"anyOf": [{"required": ["admission"]}, {"required": ["manifest_ref"]}]}},
         "else": {"required": ["input", "admission"], "not": {"required": ["native_event_ref"]}}},
        {"if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
         "then": {"properties": {"input": {"required": ["service_id"],
                                           "properties": {"service_id": {"const": "devops"}}},
                                  "admission": {"required": ["service_id"],
                                                "properties": {"service_id": {"const": "devops"}}}}}},
        {"if": {"required": ["origin"], "properties": {"origin": {"const": "native"},
                                                     "state": {"const": "succeeded"}}},
         "then": {"required": ["provider_readback_ref"], "properties": {"provider_state": {"const": "completed"}}}},
        {"if": {"required": ["origin"], "properties": {"origin": {"const": "native"},
                                                     "state": {"const": "failed"}}},
         "then": {"required": ["provider_readback_ref"], "properties": {"provider_state": {"const": "failed"}}}},
        {"if": {"properties": {"state": {"const": "outcome_unknown"}}},
         "then": {"properties": {"provider_state": {"enum": ["unknown", "running"]}},
                  "not": {"required": ["provider_readback_ref"]}}},
    ]
    definitions["Manifest"]["properties"]["admission_hash"] = ref("Hash")
    definitions["Manifest"]["required"].append("admission_hash")
    definitions["Manifest"]["properties"]["source_snapshot_ids"] = {**array(ref("Id")), "minItems": 1, "uniqueItems": True}
    definitions["Manifest"]["required"].append("source_snapshot_ids")
    definitions["Reason"] = obj({"reason": text(1000)})
    definitions["Finding"] = obj({"finding_id": ref("Id"), "native_id": ref("Id"), "native_job_ref": ref("RecordRef"), "kind": enum("candidate_threat", "static_finding"), "claim": text(10000), "provider_severity": text(64), "validation": enum("candidate", "supported", "contradicted", "unverified"), "source_refs": array(ref("RecordRef")), "evidence": array(ref("ArtifactRef")), "locations": array(text(1024)), "assumptions": array(text(2000)), "mitigations": array(text(2000))})
    definitions["TriageInput"] = obj({"decision": enum("open", "accepted_risk", "false_positive", "resolved"), "reason": text(2000)})
    definitions["Triage"] = obj({"finding_id": ref("Id"), "revision": ref("Version"), "actor_id": ref("Id"), "observed_at": ref("Instant"), "decision": ref("TriageInput")})
    definitions["Cost"] = obj({"kind": enum("unknown", "estimated", "observed"), "currency": enum("USD"), "amount": {"type": "string", "pattern": "^[0-9]+(\\.[0-9]{1,6})?$"}, "evidence_ref": ref("ArtifactRef")}, optional=("amount", "evidence_ref"))
    definitions["Cost"]["allOf"] = [{"if": {"properties": {"kind": {"const": "unknown"}}}, "then": {"not": {"required": ["amount"]}}, "else": {"required": ["amount"]}}]
    definitions["Result"] = obj({"result_id": ref("Id"), "run_id": ref("Id"), "scope": ref("Scope"), "revision": ref("Version"), "manifest_ref": ref("ArtifactRef"), "native_job_ref": ref("RecordRef"), "observed_at": ref("Instant"), "coverage": enum("complete", "partial", "unknown"), "coverage_mode": enum("documents", "full_snapshot", "selected_files", "native_observation", "deployment_verification"), "findings_artifact": ref("ArtifactRef"), "finding_count": {"type": "integer", "minimum": 0}, "costs": array(ref("Cost"), 3), "publication_id": ref("Id")})
    definitions["Result"]["properties"].update({"service_id": ref("ServiceId"),
                                                    "origin": enum("adp_submitted", "native"),
                                                    "admission": ref("Admission"),
                                                    "native_event_ref": ref("RecordRef"),
                                                    "provider_outcome": enum("completed", "failed", "stopped", "outcome_unknown"),
                                                    "scenarios": array(ref("VerificationScenarioResult"), 20),
                                                    "independent_observations": array({"allOf": [
                                                        ref("VerificationScenarioResult"),
                                                        {"required": ["positive", "negative", "deployment_ref", "deployed_sha"]}]}, 20),
                                                    "cleanup": {**array(ref("VerificationCleanup"), 20), "minItems": 1},
                                                    "deployment_ref": ref("RecordRef"), "source_ref": ref("RecordRef"),
                                                    "deployed_sha": ref("Revision"), "native_task_id": ref("Id"),
                                                    "evidence_ref": ref("ArtifactRef")})
    definitions["Result"]["required"] = [key for key in definitions["Result"]["required"]
                                          if key not in {"manifest_ref", "findings_artifact", "finding_count", "publication_id"}]
    definitions["Result"]["allOf"] = [
        {"if": {"required": ["provider_outcome", "coverage_mode", "coverage"],
                "properties": {"provider_outcome": {"const": "failed"},
                               "coverage_mode": {"const": "deployment_verification"},
                               "coverage": {"enum": ["partial", "unknown"]}}},
         "else": {"properties": {"scenarios": {"minItems": 1},
                                  "independent_observations": {"minItems": 1}}}},
        {"if": {"required": ["origin"], "properties": {"origin": {"const": "native"}}},
         "then": {"required": ["service_id", "provider_outcome"],
                  "properties": {"service_id": {"const": "devops"}},
                  "not": {"anyOf": [{"required": ["manifest_ref"]}, {"required": ["admission"]}]}},
         "else": {"required": ["finding_count"],
                  "not": {"required": ["native_event_ref"]},
                  "if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
                  "then": {"required": ["admission", "provider_outcome"],
                           "properties": {"admission": {"required": ["service_id"],
                                                        "properties": {"service_id": {"const": "devops"}}}},
                           "if": {"properties": {"admission": {"properties": {"source_snapshots": {"minItems": 1}}}}},
                           "then": {"required": ["manifest_ref"]}},
                  "else": {"not": {"required": ["admission"]},
                           "if": {"required": ["provider_outcome"]},
                           "then": {"required": ["manifest_ref"]}}}},
        {"if": {"properties": {"provider_outcome": {"enum": ["failed", "outcome_unknown"]}},
                "required": ["provider_outcome"]},
         "then": {"if": {"required": ["coverage_mode"],
                         "properties": {"provider_outcome": {"const": "failed"},
                                        "coverage_mode": {"const": "deployment_verification"}}},
                  "else": {"properties": {"coverage": {"enum": ["partial", "unknown"]}}}}},
        {"if": {"properties": {"provider_outcome": {"const": "completed"}},
                "required": ["provider_outcome"]},
         "then": {"if": {"required": ["coverage_mode"],
                         "properties": {"coverage_mode": {"const": "deployment_verification"}}},
                  "then": {"required": ["scenarios", "independent_observations", "cleanup",
                                        "source_ref", "deployment_ref", "deployed_sha", "native_task_id"]},
                  "else": {"required": ["evidence_ref"]}}},
        {"if": {"properties": {"provider_outcome": {"const": "outcome_unknown"}},
                "required": ["provider_outcome"]},
         "then": {"properties": {"coverage": {"const": "unknown"}},
                  "not": {"anyOf": [{"required": ["findings_artifact"]}, {"required": ["publication_id"]}]}}},
    ]
    definitions["TransformCheck"] = obj({"name": ref("Id"), "status": enum("passed", "failed", "unknown"),
                                         "evidence_ref": ref("RecordRef")}, optional=("evidence_ref",))
    definitions["TransformResultDetail"] = obj({"service_id": {"const": "transform"},
        "original_patch_ref": ref("RecordRef"), "baseline_checks": array(ref("TransformCheck")),
        "candidate_checks": array(ref("TransformCheck")), "source_ref": ref("RepositorySourceRef"),
        "limitations": array(text(1000)),
        "provider_state": enum("not_started", "running", "stopped", "completed", "failed", "unknown"),
        "cost_status": enum("observed", "unknown")}, optional=("original_patch_ref",))
    definitions["TransformResultDetail"]["allOf"] = [
        {"if": {"required": ["original_patch_ref"]},
         "then": {"properties": {"provider_state": {"const": "completed"}}}}]
    definitions["TransformResult"] = obj({"service_id": {"const": "transform"},
        "result_id": ref("Id"), "run_id": ref("Id"), "scope": ref("Scope"),
        "revision": ref("Version"), "observed_at": ref("Instant"),
        "transform_result": ref("TransformResultDetail")})
    definitions["LegacyResult"] = definitions.pop("Result")
    definitions["LegacyResult"]["allOf"].append(
        {"not": {"required": ["service_id"], "properties": {"service_id": {"enum": ["transform", "finops"]}}}})
    definitions["Result"] = {"oneOf": [ref("LegacyResult"), ref("TransformResult"), ref("FinopsResult")]}
    definitions["Publication"] = obj({"publication_id": ref("Id"), "result_id": ref("Id"), "state": enum("pending", "publishing", "published", "retry_wait", "unavailable", "invalidation_pending", "invalidated"), "contribution_hash": ref("Hash"), "receipt_id": ref("Id"), "consumer_revision": ref("Revision"), "invalidation_watermark": {"type": "integer", "minimum": 0}}, optional=("receipt_id", "consumer_revision"))
    definitions["Publication"]["allOf"] = [{"if": {"properties": {"state": {"const": "published"}}}, "then": {"required": ["receipt_id", "consumer_revision"]}}]
    definitions["ResultPublicationConsumer"] = obj({
        "destination": enum("product-context", "engineering-insights"), "state": {"type": "string", "minLength": 1},
        "projection_verified_on_ack": {"type": "boolean"}, "publication_id": ref("Id"),
        "last_error": {"type": "string"}, "receipt_id": ref("Id"),
        "consumer_revision": {"type": "string"},
    }, optional=("publication_id", "last_error", "receipt_id", "consumer_revision"))
    definitions["ResultPublicationStatus"] = obj({"result_id": ref("Id"), "revision": ref("Version"),
                                                    "consumers": {"type": "array", "items": ref("ResultPublicationConsumer")}})
    definitions["Contribution"] = obj({"schema_version": {"const": "1.0"}, "contribution_id": ref("Id"), "scope": ref("Scope"), "result_ref": ref("RecordRef"), "run_id": ref("Id"), "native_job_ref": ref("RecordRef"), "manifest_ref": ref("ArtifactRef"), "source_refs": array(ref("RecordRef")), "classification": enum("public", "internal", "confidential", "restricted"), "validation": enum("candidate", "supported", "contradicted", "unverified"), "observed_at": ref("Instant"), "causal_origin": ref("Id"), "evidence": array(ref("ArtifactRef")), "invalidation_watermark": {"type": "integer", "minimum": 0}})
    definitions["Contribution"]["properties"]["claims"] = array(obj({
        "finding_id": ref("Id"), "native_id": ref("Id"), "kind": enum("candidate_threat", "static_finding"),
        "claim": {"type": "string", "minLength": 1}, "provider_severity": {"type": "string", "minLength": 1},
        "validation": enum("candidate", "supported", "contradicted", "unverified"),
        "visibility": ref("Visibility"), "source_refs": array(ref("RecordRef")),
        "evidence": array(ref("ArtifactRef")), "locations": {"type": "array"},
        "assumptions": {"type": "array"}, "mitigations": {"type": "array"},
    }))
    definitions["Contribution"]["required"].append("claims")
    definitions["Contribution"]["properties"]["claim_page"] = obj({
        "index": {"type": "integer", "minimum": 0}, "count": {"type": "integer", "minimum": 2},
        "total_claims": {"type": "integer", "minimum": 1}, "contribution_hash": ref("Hash"),
    })
    definitions["ComparisonInput"] = obj({"baseline_result_id": ref("Id"), "candidate_result_id": ref("Id")})
    definitions["Comparison"] = obj({"comparability": enum("comparable", "partial", "incomparable"),
                                      "reasons": array(text(1000)), "artifact_ref": ref("ArtifactRef"),
                                      "baseline_revision": ref("Version"), "candidate_revision": ref("Version"),
                                      "new_findings": array(ref("Id")), "unchanged_findings": array(ref("Id")),
                                      "resolved_findings": array(ref("Id"))},
                                     optional=("artifact_ref", "baseline_revision", "candidate_revision",
                                               "new_findings", "unchanged_findings", "resolved_findings"))
    definitions["DevopsEvent"] = obj({"event_id": ref("Id"), "scope": ref("Scope"), "binding_id": ref("Id"),
                                       "origin": {"const": "native_event"}, "native_event_ref": ref("RecordRef"),
                                       "received_at": ref("Instant"),
                                       "state": enum("received", "processing", "reconciled", "unavailable"),
                                       "result_ref": ref("RecordRef")}, optional=("result_ref",))
    definitions["DevopsEventList"] = obj({"items": array(ref("DevopsEvent")), "next_cursor": text(2048)},
                                           optional=("next_cursor",))
    definitions["BrowserLaunchInput"] = obj({"configuration_revision": ref("Version")})
    definitions["BrowserLaunch"] = obj({"binding_id": ref("Id"), "launch_url": {**text(2048), "format": "uri", "pattern": "^https://"},
                                         "expires_at": ref("Instant")})
    definitions["Error"] = obj({"code": text(128), "message": text(1000), "retryable": {"type": "boolean"}, "request_id": ref("Id"), "operation_ref": ref("Id")}, optional=("operation_ref",))
    definitions["McpError"] = obj({"http_status": {"type": "integer", "minimum": 400, "maximum": 599}, "error": ref("Error")})
    definitions["ArtifactAccess"] = obj({"artifact": ref("ArtifactRef"), "download_path": {**text(1024), "pattern": "^/v1/"}, "expires_at": ref("Instant")})
    definitions["ProviderIdentity"] = obj({"account_id": {"type": "string", "pattern": "^[0-9]{12}$"}, "role_arn": {**text(2048), "pattern": "^arn:aws:iam::[0-9]{12}:role/"}, "region": text(64), "verified_at": ref("Instant")})
    definitions["FinopsProviderIdentity"] = obj({
        "account_id": {"type": "string", "pattern": "^[0-9]{12}$"},
        "role_arn": {**text(2048), "pattern": "^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$"},
        "region": {**text(64), "pattern": "^[a-z]{2}-[a-z]+-[0-9]+$"},
    })
    definitions["Binding"]["properties"].update({
        "provider_identity": {"anyOf": [ref("ProviderIdentity"), ref("FinopsProviderIdentity")]},
        "native_agent_display_name": text(256),
        "native_configuration_revision": ref("Version"),
        "billing_scope": ref("FinopsBillingScope"),
    })
    definitions["Service"] = obj({"service_id": ref("ServiceId"), "availability": enum("available", "unavailable"), "capabilities": array(ref("SharedCapability"), 4), "contract_version": {"const": "1.0"}, "reason": text(1000)}, optional=("reason",))
    definitions["Service"]["allOf"] = [
        {"if": {"properties": {"service_id": {"const": "devops"}}},
         "then": {"properties": {"capabilities": {"items": ref("DevopsCapability")}}},
         "else": {"if": {"properties": {"service_id": {"const": "well_architected"}}},
                  "then": {"properties": {"capabilities": {"items": ref("WellArchitectedCapability"), "maxItems": 1}}},
                  "else": {"properties": {"capabilities": {"items": ref("Capability")}}}}},
    ]
    definitions["ServiceList"] = obj({"items": array(ref("Service"))})
    definitions["AdapterOutcome"] = obj({"state": enum("accepted", "definitive_failure", "outcome_unknown", "pending", "completed", "cancelled", "unsupported"), "native_refs": array(ref("RecordRef")), "complete_pages": {"type": "boolean"}, "next_poll_at": ref("Instant"), "gaps": array(text(255)), "artifacts": array(ref("ArtifactRef"))}, optional=("next_poll_at",))
    definitions["AdapterOutcome"]["properties"]["observation"] = ref("ProviderObservation")
    definitions["CredentialSessionRequest"] = obj({"delegation_id": ref("Id"), "delegation_revision": ref("Version"), "operation_id": ref("Id"), "attempt_id": ref("Id"), "connection_ref": ref("RecordRef")})
    definitions["Delegation"] = obj({"delegation_id": ref("Id"), "revision": ref("Version"), "scope": ref("Scope"), "registered_service_id": ref("Id"), "credential_id": ref("Id"), "operations": array(text(128)), "sources": array(ref("RecordRef")), "capabilities": array(ref("SharedCapability"), 4), "expires_at": ref("Instant"), "revoked_at": ref("Instant")}, optional=("revoked_at",))
    definitions["CredentialSession"] = obj({"access_key_id": text(255), "secret_access_key": text(4096), "session_token": text(16384), "expires_at": ref("Instant"), "provider_identity": ref("ProviderIdentity"), "delegation_revision": ref("Version"), "operation_id": ref("Id"), "attempt_id": ref("Id")})
    definitions["ContextReceipt"] = obj({"receipt_id": ref("Id"), "contribution_id": ref("Id"), "admitted_schema": ref("Revision"), "consumer_revision": ref("Revision"), "record_refs": array(ref("RecordRef")), "contribution_hash": ref("Hash"), "claim_count": {"type": "integer", "minimum": 1}}, optional=("contribution_hash", "claim_count"))
    definitions["ContextReceipt"]["dependentRequired"] = {"contribution_hash": ["claim_count"], "claim_count": ["contribution_hash"]}
    definitions["ContextInvalidation"] = obj({"source_refs": array(ref("RecordRef")), "invalidation_watermark": {"type": "integer", "minimum": 0}, "reason": text(1000)})
    definitions["InvalidationReceipt"] = obj({"receipt_id": ref("Id"), "applied_watermark": {"type": "integer", "minimum": 0}, "record_refs": array(ref("RecordRef"))})
    definitions["ContextQuery"] = obj({"topic": text(1000), "source_refs": array(ref("RecordRef")), "max_records": {"type": "integer", "minimum": 1, "maximum": 100}, "required_freshness_seconds": {"type": "integer", "minimum": 0, "maximum": 86400}})
    definitions["ContextAnswer"] = obj({"snapshot_ref": ref("RecordRef"), "record_refs": array(ref("RecordRef")), "evidence_refs": array(ref("ArtifactRef")), "gaps": array(text(1000)), "observed_at": ref("Instant")})
    definitions["WorkEnvelope"] = obj({"schema_version": {"const": "1.0"}, "work_id": ref("Id"), "generation": ref("Version")})
    for name in ("Configuration", "Binding", "Run", "Result", "Finding", "Triage"):
        definitions[name + "List"] = obj({"items": array(ref(name)), "next_cursor": text(2048)}, optional=("next_cursor",))
    finops = json.loads((HOME.parents[1] / "finops/contract.json").read_text())["$defs"]
    definitions.update({name: definition for name, definition in finops.items() if name != "RecordRef"})
    definitions["FinopsSettings"] = obj({
        "agent_mode": enum("create", "adopt"), "native_agent_ref": ref("Id"),
        "native_agent_display_name": text(256),
        "data_role_ref": ref("Id"),
        "billing_scope": obj({"account_ids": {**array({"type": "string", "pattern": "^[0-9]{12}$"}), "minItems": 1, "uniqueItems": True},
                              "view_ref": text(2048)}),
    }, optional=("agent_mode", "native_agent_ref", "native_agent_display_name", "data_role_ref", "billing_scope"))
    definitions["FinopsTrigger"] = {
        **definitions["Trigger"],
        "properties": {**definitions["Trigger"]["properties"], "capability": {"const": "cost_inquiry"}},
        "required": [field for field in definitions["Trigger"]["required"] if field != "debounce_seconds"],
        "allOf": [
            {"if": {"properties": {"enabled": {"const": True}}}, "then": {"required": ["capability", "binding_id"]}},
            definitions["Trigger"]["allOf"][1],
        ],
    }
    definitions["FinopsConfigurationInput"] = obj({
        **{name: value for name, value in definitions["LegacyConfigurationInput"]["properties"].items()
           if name not in {"devops", "well_architected", "service_id", "capabilities"}},
        "service_id": {"const": "finops"}, "finops": ref("FinopsSettings"),
        "triggers": array(ref("FinopsTrigger"), 20),
        "capabilities": {**array({"const": "cost_inquiry"}, 1), "uniqueItems": True},
    }, optional=tuple(name for name in definitions["LegacyConfigurationInput"]["properties"]
                      if name not in {"devops", "well_architected", "service_id", "capabilities"}) + ("capabilities",))
    definitions["FinopsResult"] = obj({
        "result_id": ref("Id"), "revision": ref("Version"), "scope": ref("Scope"),
        **finops["FinopsCostResult"]["properties"], "publication_id": ref("Id"),
    }, optional=("publication_id",))
    definitions["RunInput"] = {"oneOf": [definitions["RunInput"], ref("FinopsRunInput")]}
    definitions["FinopsAgentRef"] = obj({
        **definitions["RecordRef"]["properties"], "record_type": {"const": "finops_agent"},
    }, optional=("source_connection_id", "record_revision"))
    definitions["FinopsBillingScope"] = obj({
        "scope_generation": {"type": "integer"},
        "account_ids": definitions["FinopsSettings"]["properties"]["billing_scope"]["properties"]["account_ids"],
        "view_ref": text(2048), "filter_digest": ref("Hash"), "role_policy_digest": ref("Hash"),
        "observed_at": ref("Instant"), "scope_digest": ref("Hash"), "snapshot_artifact": ref("Artifact"),
    }, optional=("account_ids", "view_ref", "filter_digest", "role_policy_digest", "observed_at",
                 "scope_digest", "snapshot_artifact"))
    definitions["FinopsAdmission"] = obj({
        **{name: value for name, value in definitions["Admission"]["properties"].items()
           if name in definitions["Admission"]["required"] or name == "event_id"},
        "service_id": {"const": "finops"}, "native_agent_ref": ref("FinopsAgentRef"),
        "native_agent_display_name": text(256), "provider_identity": ref("FinopsProviderIdentity"),
        "billing_scope": ref("FinopsBillingScope"),
    }, optional=("event_id", "native_agent_display_name", "provider_identity"))
    definitions["LegacyAdmission"] = definitions.pop("Admission")
    definitions["LegacyAdmission"]["allOf"].append(
        {"not": {"required": ["service_id"], "properties": {"service_id": {"const": "finops"}}}})
    definitions["DevopsLimits"] = obj(dict(definitions["Limits"]["properties"]), optional=("policy_ref",))
    definitions["DevopsLimits"]["description"] = "DevOps execution limits. Manual investigation costs are planning estimates; automated work still requires a budget policy at admission."
    for name in ("LegacyConfigurationInput", "LegacyAdmission"):
        definitions[name]["properties"]["limits"] = ref("DevopsLimits")
        definitions[name]["allOf"].append({
            "if": {"required": ["service_id"], "properties": {"service_id": {"const": "devops"}}},
            "then": {"properties": {"limits": ref("DevopsLimits")}},
            "else": {"properties": {"limits": ref("Limits")}},
        })
    definitions["Admission"] = {"oneOf": [ref("LegacyAdmission"), ref("FinopsAdmission")]}
    definitions["Run"]["allOf"].append({
        "if": {"required": ["input"], "properties": {
            "input": {"required": ["service_id"], "properties": {"service_id": {"const": "finops"}}}}},
        "then": {"properties": {"admission": ref("FinopsAdmission"), "service_id": {"const": "finops"}}},
        "else": {"properties": {"admission": ref("LegacyAdmission")}},
    })
    definitions["FinopsCatalogMapping"] = obj({"status": {"const": "unresolved"},
                                                 "module_id": {"type": "null"}, "feature_id": {"type": "null"},
                                                 "owner": text(255)})
    definitions["FinopsOptions"] = obj({"service_id": {"const": "finops"},
                                         "connections": array(ref("RecordRef")),
                                         "sources": array(ref("RecordRef")),
                                         "regions": array(text(64)),
                                         "capabilities": {**array({"const": "cost_inquiry"}, 1), "uniqueItems": True},
                                         "unavailable_reasons": array(text(255)),
                                         "provider_contacted": {"const": False},
                                         "readiness": obj({"capability": {"const": "cost_inquiry"},
                                                           "status": {"const": "blocked"},
                                                           "gaps": array(text(255))}),
                                         "catalog_mapping": ref("FinopsCatalogMapping")})
    definitions["Options"] = {"oneOf": [definitions["Options"], ref("FinopsOptions")]}
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": "https://example.org/adp/l5/v1/schemas.json", "$defs": definitions}


OPERATIONS = [
    ("listServices", "get", "", None, "ServiceList", "read", "list", "Available services"),
    ("getConfiguration", "get", "/configuration", None, "Configuration", "read", "config show", "Configuration"),
    ("listConfigurationVersions", "get", "/configuration/versions", None, "ConfigurationList", "read", "config history", "Configuration history"),
    ("getConfigurationVersion", "get", "/configuration/versions/{revision}", None, "Configuration", "read", "config version", "Pinned configuration version"),
    ("saveConfiguration", "put", "/configuration", "ConfigurationInput", "Configuration", "conditional", "config update", "Save / enable / disable / triggers"),
    ("getOptions", "get", "/configuration/options", None, "Options", "read", "options", "Authorized choices"),
    ("refreshConfiguration", "post", "/configuration/refresh", "RefreshInput", "Configuration", "conditional", "config refresh", "Refresh inherited values"),
    ("checkConfiguration", "post", "/configuration/check", None, "Check", "read", "check", "Check configuration only"),
    ("listBindings", "get", "/bindings", None, "BindingList", "read", "binding list", "Bindings"),
    ("getBinding", "get", "/bindings/{binding_id}", None, "Binding", "read", "binding show", "Binding detail"),
    ("getDevopsPrevention", "get", "/bindings/{binding_id}/prevention", None, "DevopsPreventionStatus", "read", "prevention status", "Selected native goal schedule and observed state"),
    ("configureDevopsPrevention", "post", "/bindings/{binding_id}/prevention", "DevopsPreventionInput", "DevopsPreventionStatus", "conditional", "prevention configure", "Enable, pause or resume selected native goal"),
    ("launchNativeBrowser", "post", "/bindings/{binding_id}/browser-launch", "BrowserLaunchInput", "BrowserLaunch", "launch", "-", "Launch scoped native browser session (UI only)"),
    ("listBindingEvents", "get", "/bindings/{binding_id}/events", None, "DevopsEventList", "read", "binding events", "Native event receipts and reconciliation"),
    ("startBindingOperation", "post", "/binding-operations", "BindingInput", "Operation", "binding", "binding create|link|check|update|suspend|resume|unlink|cleanup|sync_knowledge", "Create / link / verify / update / suspend / resume / unlink / owned cleanup / explicit knowledge synchronization; may provision billable resources"),
    ("getBindingOperation", "get", "/binding-operations/{operation_id}", None, "Operation", "read", "binding status", "Setup progress and uncertain outcome"),
    ("reconcileBindingOperation", "post", "/binding-operations/{operation_id}/reconcile", "Reason", "Operation", "mutate", "binding reconcile", "Reconcile original setup without another create"),
    ("submitRun", "post", "/runs", "RunInput", "Run", "mutate", "run", "Submit selected revision; may incur paid provider work. Reuse the idempotency key to recover the original run"),
    ("listRuns", "get", "/runs", None, "RunList", "read", "runs", "Run history"),
    ("getRun", "get", "/runs/{run_id}", None, "Run", "read", "status", "Run detail"),
    ("cancelRun", "post", "/runs/{run_id}/cancel", "Reason", "Run", "mutate", "cancel", "Request cancellation"),
    ("reconcileRun", "post", "/runs/{run_id}/reconcile", "Reason", "Run", "mutate", "reconcile", "Recover uncertain outcome"),
    ("retryRun", "post", "/runs/{run_id}/retry", "Reason", "Run", "mutate", "retry", "Security-only retry after definitive no-effect proof; DevOps run retry is unavailable"),
    ("listResults", "get", "/results", None, "ResultList", "read", "results", "Results"),
    ("getResult", "get", "/results/{result_id}", None, "Result", "read", "result show", "Result detail and coverage"),
    ("getResultPublicationStatus", "get", "/results/{result_id}/publication-status", None, "ResultPublicationStatus", "read", "result publication", "Publication debt and acknowledged consumer receipts for one result revision"),
    ("listFindings", "get", "/results/{result_id}/findings", None, "FindingList", "read", "findings", "Findings"),
    ("setTriage", "put", "/results/{result_id}/findings/{finding_id}/triage", "TriageInput", "Triage", "conditional", "triage set", "Human triage"),
    ("listTriage", "get", "/results/{result_id}/findings/{finding_id}/triage", None, "TriageList", "read", "triage history", "Triage history"),
    ("compareResults", "post", "/comparisons", "ComparisonInput", "Comparison", "read", "compare", "Compare compatible results"),
    ("getPublication", "get", "/publications/{publication_id}", None, "Publication", "read", "publication show", "Publication status"),
    ("retryPublication", "post", "/publications/{publication_id}/retry", "Reason", "Publication", "mutate", "publication retry", "Retry publication only"),
    ("getArtifactAccess", "get", "/artifacts/{artifact_id}", None, "ArtifactAccess", "read", "artifact", "Authorized artifact download path and expiry; no bearer session URL"),
    ("downloadArtifact", "get", "/artifacts/{artifact_id}/content", None, "ArtifactRef", "read", "artifact download", "Return an authorized artifact reference; download bytes through the scoped HTTP path"),
]


def with_definitions(value, definitions):
    """Make each MCP input/output schema independently resolvable."""
    import re

    required = {}
    pending = re.findall(r'"\$ref": "#/\$defs/([^\"]+)"', json.dumps(value))
    while pending:
        name = pending.pop()
        if name not in required:
            required[name] = definitions[name]
            pending.extend(re.findall(r'"\$ref": "#/\$defs/([^\"]+)"', json.dumps(definitions[name])))
    return {**value, **({"$defs": required} if required else {})}


def artifacts():
    import re

    schema = schemas()
    paths = {}
    tools = []
    matrix = [["operation_id", "method", "path", "input", "output", "effect", "UI", "CLI (adp service)", "MCP"]]
    prefix = "/v1/products/{product_id}/projects/{project_id}/services/{service_id}"
    for name, method, suffix, request, response, effect, cli, ui in OPERATIONS:
        path = prefix.rsplit("/", 1)[0] if name == "listServices" else prefix + suffix
        parameters = [{"name": field, "in": "path", "required": True, "schema": text(255)} for field in re.findall(r"{([^}]+)}", path)]
        if response.endswith("List"):
            parameters += [{"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25}}, {"name": "cursor", "in": "query", "schema": text(2048)}]
        if effect != "read":
            parameters.append({"name": "Idempotency-Key", "in": "header", "required": True, "schema": text(255)})
        if effect in {"conditional", "binding"}:
            parameters.append({"name": "If-Match", "in": "header", "required": effect == "conditional" and name != "saveConfiguration", "schema": schema["$defs"]["ETag"], "description": "Exact strong ETag from the target read. Required for existing-resource changes; binding create/link use pinned configuration revision instead."})
        if name == "saveConfiguration":
            parameters.append({"name": "If-None-Match", "in": "header", "required": False, "schema": {"const": "*"}, "description": "First create requires this; existing save requires If-Match, never both."})
        status = "202" if effect in {"mutate", "binding"} else "200"
        operation = {"operationId": name, "summary": ui, "parameters": parameters, "x-effect": effect,
                     "responses": {status: {"description": "Durable accepted intent" if status == "202" else "Authorized result", "headers": {"ETag": {"schema": schema["$defs"]["ETag"], "description": "Opaque strong token copied verbatim into MCP etag. Required for Configuration, Binding, Triage and TriageList; triage history uses the current triage head, including its empty initial state."}}, "content": {"application/json": {"schema": {"$ref": f"./schemas.json#/$defs/{response}"}}}}}}
        if name == "getResultPublicationStatus":
            operation["responses"][status].pop("headers")
        if name == "saveConfiguration":
            operation["responses"]["201"] = operation["responses"]["200"]
        if request:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": f"./schemas.json#/$defs/{request}"}}}}
        if name == "downloadArtifact":
            operation["responses"][status]["content"] = {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}
        for code in (401, 403, 404, 409, 410, 412, 422, 429, 503):
            operation["responses"][str(code)] = {"description": "See architecture error contract", "content": {"application/json": {"schema": {"$ref": "./schemas.json#/$defs/Error"}}}}
        paths.setdefault(path, {})[method] = operation
        tool_name = "-" if effect == "launch" else "l5___" + re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
        arguments = {("assessment_ref" if parameter["name"] == "run_id" else parameter["name"]): parameter["schema"] for parameter in parameters if parameter["in"] == "path"}
        optional = []
        if request:
            arguments["input"] = ref(request)
        if effect != "read":
            arguments["idempotency_key"] = text(255)
        if effect in {"conditional", "binding"}:
            arguments["if_match"] = ref("ETag")
            if effect == "binding" or name == "saveConfiguration":
                optional.append("if_match")
        if name == "saveConfiguration":
            arguments["create_only"] = {"type": "boolean"}
            optional.append("create_only")
        if response.endswith("List"):
            arguments.update({"limit": {"type": "integer", "minimum": 1, "maximum": 100}, "cursor": text(2048)})
            optional.extend(["limit", "cursor"])
        input_schema = with_definitions(obj(arguments, optional), schema["$defs"])
        output_definitions = schema["$defs"]
        description = ui
        if name == "downloadArtifact":
            output_definitions = {**output_definitions, "ArtifactRef": obj({
                **output_definitions["ArtifactRef"]["properties"],
                "content_base64": {"type": "string", "contentEncoding": "base64", "maxLength": 349528},
            }, optional=("content_base64",))}
            description = "Return an authorized artifact reference; Transform also returns at most 256 KiB of verified original patch bytes as base64. Larger artifacts require scoped CLI download"
        output_schema = with_definitions(obj({"data": ref(response), "http_status": {"enum": [200, 201]} if name == "saveConfiguration" else {"const": int(status)}, "etag": ref("ETag")}, optional=() if response in {"Configuration", "Binding", "Triage", "TriageList"} else ("etag",)), output_definitions)
        if effect != "launch":
            tools.append({"name": tool_name, "description": description, "inputSchema": input_schema, "outputSchema": output_schema, "annotations": {"readOnlyHint": effect == "read", "destructiveHint": name in {"startBindingOperation", "cancelRun"}}, "x-http-operation": name})
        matrix.append([name, method.upper(), suffix or "../services", request or "-", "binary" if name == "downloadArtifact" else response, effect, ui, cli, tool_name])
    openapi = {"openapi": "3.1.0", "info": {"title": "Proposed ADP L5 API", "version": "1.0.0"}, "servers": [{"url": "https://api.example.org"}], "security": [{"platformAuth": []}], "components": {"securitySchemes": {"platformAuth": {"type": "http", "scheme": "bearer", "description": "Existing gateway identity middleware; verified internal IAM transport is separately enforced for service callers."}}}, "paths": paths}
    record = {"tenant_id": "example-team", "project_id": "project-example", "source_connection_id": "connection-example", "record_type": "repository_commit", "record_id": "repository-example", "record_revision": "a" * 40}
    examples = [
        {"schema": "RecordRef", "valid": True, "value": record},
        {"schema": "RecordRef", "valid": False, "value": record | {"record_revision": "latest"}},
        {"schema": "RunInput", "valid": True, "value": {"configuration_revision": 1, "binding_id": "binding-example", "capability": "code_review", "sources": [record]}},
        {"schema": "RunInput", "valid": False, "value": {"configuration_revision": 1, "binding_id": "binding-example", "capability": "pentest", "sources": [record]}},
        {"schema": "RunInput", "valid": True, "value": {"configuration_revision": 1, "binding_id": "binding-example", "service_id": "devops", "capability": "investigation", "devops_input": {"kind": "investigation", "resource_refs": [record], "telemetry_refs": [], "question": "What changed?"}}},
        {"schema": "RunInput", "valid": False, "value": {"configuration_revision": 1, "binding_id": "binding-example", "service_id": "devops", "capability": "prevention", "devops_input": {"kind": "investigation", "resource_refs": [record], "telemetry_refs": [], "question": "What changed?"}}},
        {"schema": "RunInput", "valid": True, "value": {"configuration_revision": 1, "binding_id": "binding-example", "service_id": "well_architected", "capability": "architecture_review", "sources": [record, record | {"record_type": "document", "record_revision": "document-version-1"}]}},
        {"schema": "RunInput", "valid": False, "value": {"configuration_revision": 1, "binding_id": "binding-example", "service_id": "well_architected", "capability": "architecture_review", "sources": [record | {"record_revision": "HEAD"}]}},
        {"schema": "RunInput", "valid": True, "value": {"service_id": "finops", "capability": "cost_inquiry", "configuration_revision": 1, "binding_id": "binding-example", "question": "How much did January cost?", "query": {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost", "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD"}}},
        {"schema": "RunInput", "valid": False, "value": {"service_id": "finops", "capability": "cost_inquiry", "configuration_revision": 1, "binding_id": "binding-example", "question": "How much did January cost?", "query": {"start": "2026-01-01", "end": "2026-02-01", "metric": "UnblendedCost", "granularity": "MONTHLY", "group_by": ["SERVICE"], "currency": "USD", "billing_view_ref": "caller-selected-view"}}},
        {"schema": "BindingInput", "valid": False, "value": {"action": "cleanup", "configuration_revision": 1, "binding_id": "binding-example"}},
        {"schema": "BindingInput", "valid": True, "value": {"action": "cleanup", "configuration_revision": 1, "binding_id": "binding-example", "owned_cleanup_confirmed": True}},
        {"schema": "BindingInput", "valid": True, "value": {"action": "sync_knowledge", "configuration_revision": 1, "binding_id": "binding-example", "binding_revision": 2, "binding_generation": 1}},
        {"schema": "BindingInput", "valid": False, "value": {"action": "sync_knowledge", "configuration_revision": 1, "binding_id": "binding-example"}},
        {"schema": "DevopsKnowledgePolicy", "valid": True, "value": {"source_refs": [record | {"record_type": "document"}], "visibility": "product_team", "publish_results": False, "refresh": "manual", "publications": [{"source_ref": record | {"record_type": "document"}, "kind": "instruction", "scope": "agent", "active": True, "native_metadata": {"agent_type": "CHAT"}}]}},
        {"schema": "DevopsKnowledgePublication", "valid": False, "value": {"source_ref": record, "kind": "context", "scope": "agent", "active": True, "native_metadata": {"agent_type": "CHAT"}}},
        {"schema": "Publication", "valid": False, "value": {"publication_id": "publication-example", "result_id": "result-example", "state": "published", "contribution_hash": "0" * 64, "invalidation_watermark": 0}},
        {"schema": "Cost", "valid": True, "value": {"kind": "unknown", "currency": "USD"}},
        {"schema": "Cost", "valid": False, "value": {"kind": "unknown", "currency": "USD", "amount": "0"}},
    ]
    output = io.StringIO()
    csv.writer(output, delimiter="\t", lineterminator="\n").writerows(matrix)
    return {"schemas.json": json.dumps(schema, indent=2) + "\n", "openapi.json": json.dumps(openapi, indent=2) + "\n", "mcp-tools.json": json.dumps(tools, indent=2) + "\n", "examples.json": json.dumps(examples, indent=2) + "\n", "operations.tsv": output.getvalue()}


if __name__ == "__main__":
    for filename, content in artifacts().items():
        (HOME / filename).write_text(content)
