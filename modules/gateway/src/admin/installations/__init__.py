"""Canonical installation -> tenant ownership (Issue #4070, sub-EPIC #4068 ·A0)."""

from .resolver import (
    InstallationOwner,
    InstallationOwnershipError,
    OwnerState,
    assert_installation_owned_by,
    resolve_installation_owner,
)

__all__ = [
    "InstallationOwner",
    "InstallationOwnershipError",
    "OwnerState",
    "assert_installation_owned_by",
    "resolve_installation_owner",
]
