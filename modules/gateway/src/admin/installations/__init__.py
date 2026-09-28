"""Canonical installation -> tenant ownership (Issue #4070, sub-EPIC #4068 ·A0)."""

from .guards import InstallationClaimError, assert_installation_claimable_by
from .resolver import (
    InstallationOwner,
    InstallationOwnershipError,
    OwnerState,
    assert_installation_owned_by,
    resolve_installation_owner,
)

__all__ = [
    "InstallationClaimError",
    "InstallationOwner",
    "InstallationOwnershipError",
    "OwnerState",
    "assert_installation_claimable_by",
    "assert_installation_owned_by",
    "resolve_installation_owner",
]
