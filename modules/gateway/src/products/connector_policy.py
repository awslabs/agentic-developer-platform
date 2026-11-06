"""Versioned, fail-closed policy requirements for cell-local evidence."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.products.models import ConnectorPolicyRealm

IDENTITY_FIELDS = ("tenant_id", "project_id", "source_connection_id", "record_type", "record_id", "record_revision")
MAX_DEPENDENCIES = 100
MAX_DEPTH = 32


class PolicyScopeError(ValueError):
    pass


@dataclass(frozen=True)
class PolicyDependency:
    identity: Mapping[str, str]
    policy_refs: tuple[str, ...]
    depends_on: tuple[Mapping[str, str], ...] = ()


def descriptor_digest(descriptor: dict) -> str:
    return hashlib.sha256(json.dumps(descriptor, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _identity(reference: Mapping[str, str], realm: ConnectorPolicyRealm) -> tuple[str, ...]:
    if not isinstance(reference, Mapping) or set(reference) != set(IDENTITY_FIELDS):
        raise PolicyScopeError("invalid_identity")
    if any(not isinstance(reference[key], str) or not reference[key] or reference[key] != reference[key].strip() for key in IDENTITY_FIELDS):
        raise PolicyScopeError("invalid_identity")
    if (reference["tenant_id"], reference["project_id"]) != (realm.tenant_id, realm.project_id):
        raise PolicyScopeError("foreign_dependency")
    return tuple(reference[key] for key in IDENTITY_FIELDS)


def _registered_policies(realm: ConnectorPolicyRealm) -> dict[str, dict]:
    descriptor = realm.descriptor
    if (
        realm.state != "active"
        or not isinstance(descriptor, dict)
        or set(descriptor) != {"version", "audience_expression", "policies"}
        or type(descriptor["version"]) is not int
        or descriptor["version"] != 1
        or not isinstance(descriptor["audience_expression"], str)
        or not descriptor["audience_expression"].strip()
        or not isinstance(descriptor["policies"], list)
        or not 2 <= len(descriptor["policies"]) <= MAX_DEPENDENCIES
        or descriptor_digest(descriptor) != realm.descriptor_digest
    ):
        raise PolicyScopeError("invalid_realm_descriptor")
    policies = {}
    for policy in descriptor["policies"]:
        if (
            not isinstance(policy, dict)
            or set(policy) != {"ref", "revision", "visibility", "audience_expression"}
            or not isinstance(policy["ref"], str)
            or not policy["ref"]
            or not isinstance(policy["revision"], int)
            or isinstance(policy["revision"], bool)
            or policy["revision"] < 1
            or policy["visibility"] not in ("shared", "private")
            or not isinstance(policy["audience_expression"], str)
            or not policy["audience_expression"].strip()
            or policy["ref"] in policies
        ):
            raise PolicyScopeError("invalid_policy_descriptor")
        policies[policy["ref"]] = policy
    if realm.source_policy_ref not in policies or realm.evidence_policy_ref not in policies:
        raise PolicyScopeError("missing_realm_policy")
    return policies


def derive_policy_envelope(
    realm: ConnectorPolicyRealm,
    root: Mapping[str, str],
    dependencies: Sequence[PolicyDependency],
    *,
    asserted_audience: str | None = None,
) -> dict:
    policies = _registered_policies(realm)
    if len(dependencies) > MAX_DEPENDENCIES:
        raise PolicyScopeError("too_many_dependencies")
    graph = {}
    for dependency in dependencies:
        key = _identity(dependency.identity, realm)
        if key in graph or not isinstance(dependency.policy_refs, tuple) or not isinstance(dependency.depends_on, tuple):
            raise PolicyScopeError("invalid_dependency")
        graph[key] = dependency
    root_key = _identity(root, realm)
    if root_key not in graph:
        raise PolicyScopeError("missing_dependency")

    visited = set()
    visiting = set()
    required = {realm.source_policy_ref, realm.evidence_policy_ref}

    def visit(key: tuple[str, ...], depth: int):
        if depth > MAX_DEPTH or key in visiting:
            raise PolicyScopeError("invalid_dependency_graph")
        if key in visited:
            return
        node = graph.get(key)
        if node is None:
            raise PolicyScopeError("missing_dependency")
        visiting.add(key)
        for ref in node.policy_refs:
            if not isinstance(ref, str) or ref not in policies:
                raise PolicyScopeError("unknown_policy")
            required.add(ref)
        for child in node.depends_on:
            visit(_identity(child, realm), depth + 1)
        visiting.remove(key)
        visited.add(key)

    visit(root_key, 0)
    requirements = [policies[ref].copy() for ref in sorted(required)]
    shared = all(
        requirement["visibility"] == "shared" and requirement["audience_expression"] == realm.descriptor["audience_expression"]
        for requirement in requirements
    )
    audience = "shared" if shared else "private"
    if asserted_audience is not None and asserted_audience != audience:
        raise PolicyScopeError("audience_mismatch")
    return {
        "envelope_version": 1,
        "identity": dict(zip(IDENTITY_FIELDS, root_key)),
        "realm_id": realm.id,
        "realm_revision": realm.revision,
        "descriptor_digest": realm.descriptor_digest,
        "required_policies": requirements,
        "dependency_identities": [dict(zip(IDENTITY_FIELDS, key)) for key in sorted(visited)],
        "audience": audience,
    }


def derive_current_envelope(realm: ConnectorPolicyRealm, root: dict, latest: dict, asserted_audience: str | None = None) -> dict:
    nodes = []
    for row in latest.values():
        if row.state != "active" or not isinstance(row.identity, dict) or row.identity_hash != descriptor_digest(row.identity):
            continue
        if not isinstance(row.policy_refs, list) or not isinstance(row.depends_on, list):
            raise PolicyScopeError("invalid_dependency")
        nodes.append(PolicyDependency(row.identity, tuple(row.policy_refs), tuple(row.depends_on)))
    envelope = derive_policy_envelope(realm, root, nodes, asserted_audience=asserted_audience)
    envelope["dependency_versions"] = []
    for identity in envelope["dependency_identities"]:
        row = latest[descriptor_digest(identity)]
        envelope["dependency_versions"].append(
            {
                "identity": identity,
                "revision": row.revision,
                "metadata_digest": descriptor_digest(
                    {
                        "identity": identity,
                        "policy_refs": row.policy_refs,
                        "depends_on": row.depends_on,
                        "revision": row.revision,
                    }
                ),
            }
        )
    return envelope
