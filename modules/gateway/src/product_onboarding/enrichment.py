"""Map selected connector observations to bounded Profile source facts."""

import hashlib
import re

from src.product_onboarding.discovery import safe_candidate
from src.products.source_routes import collection_connection_access, connection_access

FIELD_MAP_VERSION = "devlake-profile-v1"
OBSERVATION_REVISION = re.compile(r"observation-sha256:[0-9a-f]{64}\Z")
DESTINATIONS = {
    ("github", "repository"): "/configuration/basic/repositories",
    ("jira", "tracker"): "/configuration/basic/trackers",
    ("atlassian-mcp", "tracker"): "/configuration/basic/trackers",
    ("jenkins", "ci_job"): "/configuration/advanced/pipelines",
    ("sonarqube", "quality_project"): "/configuration/advanced/observability",
    ("argocd", "deployment_application"): "/configuration/advanced/deployments",
    ("gitlab", "repository"): "/configuration/basic/repositories",
    ("pagerduty", "incident_service"): "/configuration/advanced/support",
    ("linear", "tracker_team"): "/configuration/basic/trackers",
    ("slack", "communication_channel"): "/configuration/basic/collaboration",
}


def selected_native_id(binding):
    if binding.provider != "atlassian-mcp":
        return binding.native_id
    desired = binding.desired if isinstance(binding.desired, dict) else {}
    project_id = desired.get("jira_project_id")
    board_id = desired.get("board_id")
    if not isinstance(project_id, str) or type(board_id) is not int or board_id < 1 or binding.native_id != f"{project_id}:{board_id}":
        return None
    return str(board_id)


async def selected_source_fact(db, actor, binding, item, coverage):
    """Return a source fact, never a proposed Product description or owner."""
    if (
        actor.org_id != binding.tenant_id
        or binding.unlinked_at is not None
        or binding.status in ("revoked", "unlinked")
        or coverage not in ("complete", "partial")
        or not isinstance(item, dict)
        or item.get("provenance") != "observation_only"
        or (binding.provider, item.get("kind")) not in DESTINATIONS
        or item.get("namespace") != binding.namespace
        or selected_native_id(binding) is None
        or item.get("native_id") != selected_native_id(binding)
    ):
        return None
    candidate = safe_candidate(item)
    if candidate is None or candidate["name"] != item.get("name"):
        return None
    revision = candidate["source_revision"]
    if not OBSERVATION_REVISION.fullmatch(revision):
        return None
    fields = (candidate["kind"], candidate["namespace"], candidate["native_id"], candidate["name"])
    if candidate["kind"] == "ci_job":
        fields += (candidate["full_name"],)
    if binding.provider == "gitlab":
        if candidate["kind"] != "repository" or not candidate.get("full_name"):
            return None
        fields += (candidate["full_name"],)
    digest = hashlib.sha256("\x00".join(fields).encode()).hexdigest()
    if revision != f"observation-sha256:{digest}":
        return None
    if binding.provider == "atlassian-mcp":
        product_id = binding.desired.get("product_id") if isinstance(binding.desired, dict) else None
        access = await collection_connection_access(db, actor, binding, product_id)
    else:
        access = await connection_access(db, actor, binding)
    if access is None:
        return None
    connection_id = binding.installation_id or binding.credential_id
    if connection_id is None:
        return None
    return {
        "field_path": DESTINATIONS[(binding.provider, candidate["kind"])],
        "value": {
            "tenant_id": binding.tenant_id,
            "project_id": binding.project_id,
            "source_connection_id": connection_id,
            "record_type": candidate["kind"],
            "record_id": candidate["native_id"],
            "record_revision": revision,
        },
        "display_name": candidate["name"],
        "provenance": {
            "binding_id": binding.id,
            "binding_generation": binding.generation,
            "field_map_version": FIELD_MAP_VERSION,
            "observed_at": candidate["observed_at"],
            "coverage": coverage,
            "revision_basis": "observation_only",
        },
    }


def source_changes(configuration, provenance, facts):
    """Auto-fill only unique, complete references into empty common fields."""
    from src.products.configuration import ProjectConfiguration, RecordRef, manually_confirmed_paths

    proposed = {path: [] for path in DESTINATIONS.values()}
    for fact in facts:
        path = fact["field_path"]
        reference = RecordRef.model_validate(fact["value"]).model_dump()
        if path not in proposed or path != {kind: target for (_, kind), target in DESTINATIONS.items()}.get(reference["record_type"]):
            raise ValueError("unsupported source fact")
        proposed[path].append((fact, reference))
    updated = {**configuration}
    proofs = list(provenance)
    protected_paths = manually_confirmed_paths(provenance)
    conflicts = []
    suggestions = []
    for path, entries in proposed.items():
        if not entries:
            continue
        section, field = path.removeprefix("/configuration/").split("/")
        existing = (configuration.get(section) or {}).get(field) or []
        if len(entries) != 1 or entries[0][0]["provenance"]["coverage"] != "complete":
            suggestions.extend(fact for fact, _ in entries)
            continue
        fact, reference = entries[0]
        if existing or path in protected_paths:
            if existing != [reference]:
                conflicts.append({"field_path": path, "reason": "confirmed_or_changed"})
            continue
        updated[section] = {**(updated.get(section) or {}), field: [reference]}
        proof = fact["provenance"]
        proofs.append(
            {
                "paths": [path],
                "state": "accepted",
                "method": "connector",
                "source_refs": [reference],
                "observed_at": proof["observed_at"],
                "coverage": proof["coverage"],
                "field_map_version": proof["field_map_version"],
                "revision_basis": proof["revision_basis"],
                "binding_id": proof["binding_id"],
                "binding_generation": proof["binding_generation"],
            }
        )
    ProjectConfiguration.model_validate(updated)
    return updated, proofs, conflicts, suggestions
