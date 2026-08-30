"""Shared identity types and validation."""

from src.shared.identity.resolver import (
    UnresolvableUserEntityError,
    resolve_canonical_user_id,
    resolve_user_entity_id,
)

__all__ = [
    "UnresolvableUserEntityError",
    "resolve_canonical_user_id",
    "resolve_user_entity_id",
]
