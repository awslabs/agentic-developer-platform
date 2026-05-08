"""Canonical provider registry — single source of truth.

Issue #537: Identity projection redesign.

Every component that writes or validates a provider string imports from here.
Adding a new channel = one line in this set + OAuth/webhook wiring elsewhere.
"""

from __future__ import annotations

from enum import StrEnum


class IdentityProvider(StrEnum):
    """Supported identity providers.

    Used by Postgres ORM validation AND DDB write-through clients.
    """

    cognito = "cognito"
    github = "github"
    slack = "slack"
    teams = "teams"
    discord = "discord"
    email = "email"
    whatsapp = "whatsapp"


# Frozen set for O(1) membership checks in validation paths.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(IdentityProvider)
