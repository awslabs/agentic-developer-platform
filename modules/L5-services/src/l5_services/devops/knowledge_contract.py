"""Documented AWS asset metadata, not ADP provenance used as provider policy.

https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-managing-assets.html
"""

import re


AGENT_TYPES = frozenset({"GENERIC", "CHAT", "INCIDENT_TRIAGE", "INCIDENT_RCA",
                         "INCIDENT_MITIGATION", "INCIDENT_UI", "PREVENTION",
                         "RELEASE_READINESS_REVIEW", "RELEASE_TESTING"})
MAPPING_VERSION = 2


def native_contract(kind, scope, active, visibility, metadata):
    """Return the supported native type/path/metadata and conservative byte bound."""
    if visibility != "product_team":
        raise ValueError("knowledge_private_visibility_unqualified")
    if kind not in {"skill", "instruction"}:
        raise ValueError("knowledge_context_contract_unqualified")
    if scope not in {"global", "agent"} or type(active) is not bool or not isinstance(metadata, dict):
        raise ValueError("knowledge_native_selection_required")
    if kind == "skill":
        if (not {"name", "description", "agent_types"} <= set(metadata)
                or set(metadata) - {"name", "description", "agent_types", "status", "skill_type"}
                or not isinstance(metadata["name"], str)
                or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", metadata["name"])
                or not isinstance(metadata["description"], str)
                or not 1 <= len(metadata["description"]) <= 1024 or not metadata["description"].strip()):
            raise ValueError("knowledge_skill_metadata_required")
        targets = metadata["agent_types"]
        status = "ACTIVE" if active else "INACTIVE"
        if metadata.get("status", status) != status or metadata.get("skill_type", "USER") != "USER":
            raise ValueError("knowledge_skill_activation_mismatch")
        native_type, path, limit = "skill", "SKILL.md", 1024 * 1024
        native_metadata = {**metadata, "status": status, "skill_type": "USER"}
    else:
        if set(metadata) != {"agent_type"} or not active:
            raise ValueError("knowledge_instruction_target_required_or_always_active")
        targets = [metadata["agent_type"]]
        native_type, path, limit = "agents_md", "AGENTS.md", 25_000
        native_metadata = dict(metadata)
    if (not isinstance(targets, list) or not targets
            or any(not isinstance(target, str) or target not in AGENT_TYPES for target in targets)
            or len(set(targets)) != len(targets)
            or (scope == "global" and targets != ["GENERIC"])
            or (scope == "agent" and "GENERIC" in targets)):
        raise ValueError("knowledge_agent_target_mismatch")
    if kind == "skill":
        native_metadata["agent_types"] = sorted(targets)
    return native_type, path, native_metadata, limit
