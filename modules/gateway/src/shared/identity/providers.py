"""Canonical provider registry — single source of truth.

Issue #537: Identity projection redesign.

Every component that writes or validates a provider string imports from here.

**Adding a provider is TWO changes, not one.** The docstring here used to say "one
line in this set + OAuth/webhook wiring elsewhere", and that was false in a way
that fails at runtime rather than at review: ``user_identities.provider`` also
carries a Postgres CHECK constraint, baked from a hand-copied tuple in migration
``009_provider_check_constraint`` at a pinned revision. A value added to this enum
alone passes every SQLite-backed test (SQLite does not enforce the CHECK) and then
rejects every INSERT on Postgres. So a provider addition ships:

1. the member below, and
2. a **new** CHECK-constraint migration re-stating the constraint with the new
   value (``041_directory_provider`` is the worked example) — never an edit to
   009, which has already run everywhere.

Corrected as part of #4843; the false "one line" claim is called out by name in
``docs/design-notes/4828-platform-native-org-team-user.md`` §4.
"""

from __future__ import annotations

from enum import StrEnum


class IdentityProvider(StrEnum):
    """Supported identity providers.

    Used by Postgres ORM validation AND DDB write-through clients.
    """

    cognito = "cognito"
    github = "github"
    # Instance-qualified immutable user ID, e.g. https://gitlab.example#42.
    gitlab = "gitlab"
    slack = "slack"
    teams = "teams"
    discord = "discord"
    email = "email"
    whatsapp = "whatsapp"
    # An AD / Entra directory object id (#4843, note §4). Distinct from `cognito`
    # (an authentication account this platform owns) and from `email` (mutable, and
    # the thing AD sync must NEVER dedupe on): this is the IdP's immutable
    # `objectId`, which survives a rename, an email change and a UPN change. No
    # writer creates these rows yet — Wave 2's directory sync is the writer. The
    # value exists now so the identity model is ready for it and so the person
    # anchor has a middle namespace to resolve (§4: "extend, don't redesign").
    directory = "directory"


# Frozen set for O(1) membership checks in validation paths.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset(IdentityProvider)


# ---------------------------------------------------------------------------
# Internal setup namespaces — NOT linkable identities (#5664, A10)
# ---------------------------------------------------------------------------

# `magic_link_nonces` is shared by two unrelated kinds of one-time credential:
# the user-facing identity-linking token, and the browser-redirect "state" token
# that protects the platform-admin GitHub App install / registration flows. The
# only thing distinguishing them is this `provider` string.
#
# Before #5664 the user-facing route took `provider` straight off the URL path
# and handed it to `store_nonce` unvalidated, so any signed-in user could mint a
# nonce labelled `github_app_register` — the sole authenticator on
# `register_app_callback`, which overwrites the shared GitHub App credentials,
# the webhook signing secret and the GitHub sign-in secret. Ordinary user ->
# whole-fleet credential replacement, via a text column.
#
# These names are deliberately absent from `IdentityProvider`: they are not
# identities and nothing may ever write a `user_identities` row carrying one.
# The assertion below makes the disjointness a startup invariant rather than a
# convention two modules have to remember — adding a colliding member to the
# enum fails at import, not in production.
INTERNAL_SETUP_NAMESPACES: frozenset[str] = frozenset(
    {
        "github_install",
        "github_app_register",
    }
)

assert not (INTERNAL_SETUP_NAMESPACES & SUPPORTED_PROVIDERS), (
    f"Internal setup namespaces must stay disjoint from linkable providers; overlap: {sorted(INTERNAL_SETUP_NAMESPACES & SUPPORTED_PROVIDERS)}"
)


def is_linkable_provider(provider: str) -> bool:
    """True when `provider` may be used on the user-facing identity-link surface.

    Rejects both unknown values and the internal setup namespaces above. Call
    this BEFORE persisting anything: the ORM validator on
    ``UserIdentity.provider`` only fires when the identity row is written, which
    on the magic-link flow is one request too late — the nonce has already been
    stored and the token already handed to the caller.
    """
    return provider in SUPPORTED_PROVIDERS and provider not in INTERNAL_SETUP_NAMESPACES
