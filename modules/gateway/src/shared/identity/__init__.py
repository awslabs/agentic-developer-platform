"""Shared identity types and validation."""

from src.shared.identity.person_anchor import (
    PERSON_ANCHOR_AUTHORABLE_NAMESPACES,
    PERSON_ANCHOR_GITHUB_PREFIX,
    PERSON_ANCHOR_INTERNAL_NAMESPACE,
    PERSON_ANCHOR_NAMESPACES,
    PERSON_ANCHOR_PROVIDER_PRECEDENCE,
    UnresolvablePersonAnchorError,
    format_person_anchor,
    is_authorable_person_anchor,
    parse_person_anchor,
    resolve_caller_person_anchor,
    resolve_person_anchor,
)
from src.shared.identity.resolver import (
    UnresolvableUserEntityError,
    resolve_canonical_user_id,
    resolve_root_user_entity_id,
    resolve_user_entity_id,
)

__all__ = [
    "PERSON_ANCHOR_AUTHORABLE_NAMESPACES",
    "PERSON_ANCHOR_GITHUB_PREFIX",
    "PERSON_ANCHOR_INTERNAL_NAMESPACE",
    "PERSON_ANCHOR_NAMESPACES",
    "PERSON_ANCHOR_PROVIDER_PRECEDENCE",
    "UnresolvablePersonAnchorError",
    "UnresolvableUserEntityError",
    "format_person_anchor",
    "is_authorable_person_anchor",
    "parse_person_anchor",
    "resolve_caller_person_anchor",
    "resolve_canonical_user_id",
    "resolve_person_anchor",
    "resolve_root_user_entity_id",
    "resolve_user_entity_id",
]
