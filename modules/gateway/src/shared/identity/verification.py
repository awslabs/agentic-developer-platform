"""Which verification methods are proof of ownership — #5664 (A10).

`user_identities.verification_method` records HOW a link between a platform user
and an external account was established. The column is a bare ``String(20)`` with
no enum, no CHECK constraint and no ORM validator, and five different strings
circulate in it (``VerificationMethod`` in ``models/vault.py`` names only three;
``org_placement`` is written by ``identity/workspaces.py`` and is absent from the
enum entirely). So the column already drifts from its own declared type.

Why that matters more than tidiness
-----------------------------------
Consumers read `user_identities` to answer "who is this external account?", and
that answer grants real authority: whether a GitHub comment may approve work,
which platform user an internal endpoint resolves, and which tenant a webhook
event acts on. Before this module, those consumers did not distinguish a link
established by a completed provider handshake from one a user simply asserted
about themselves — so an unproven self-assertion was indistinguishable from proof
at the point where it mattered.

This module makes the distinction explicit and central, so a consumer decides
whether to trust a row by asking rather than by hard-coding a string comparison
each caller can get subtly wrong.

The split
---------
``PROVEN_METHODS`` — the provider or an administrator established the link:

* ``oauth`` — the user completed a sign-in at the provider and the account id came
  out of the resulting token, not out of the request body.
* ``org_placement`` — written by the platform's own org-placement path
  (``identity/workspaces.py``), never by a user-facing route.
* ``admin_manual`` — an authenticated administrator asserted it, which is an
  accountable act attributable to a named principal.

``UNPROVEN_METHODS`` — the subject of the claim was never contacted:

* ``self_asserted`` — recorded when a user claims an account and nothing has yet
  demonstrated they control it.

``magic_link`` is deliberately absent from both sets, because the name alone does
not say what happened. A magic link that was DELIVERED to the claimed account and
clicked there proves control; one handed straight back to the person who asked
for it proves only that they can read their own API response. Those two are not
the same fact and must not share a label, so the flow records
``magic_link_confirmed`` (proven) or ``self_asserted`` (unproven) instead of the
ambiguous ``magic_link``. Rows written before this distinction existed carry the
bare string and are treated as UNPROVEN by ``is_proven`` — fail-closed, because
for those rows the platform genuinely cannot tell which of the two happened.

Nothing here retro-actively rewrites stored rows: a migration that "upgraded"
historical ``magic_link`` rows to a proven value would be inventing evidence that
was never collected.
"""

from __future__ import annotations

from typing import Final

# Established by the provider, or by an accountable administrator.
PROVEN_METHODS: Final[frozenset[str]] = frozenset(
    {
        "oauth",
        "org_placement",
        "admin_manual",
        # A magic link that was delivered OUT-OF-BAND to the claimed account and
        # confirmed from there. Distinct from the legacy bare "magic_link".
        "magic_link_confirmed",
    }
)

# The claimed account was never contacted. Recorded so the claim is visible and
# auditable, rather than silently absent — but it is not proof of anything.
UNPROVEN_METHODS: Final[frozenset[str]] = frozenset(
    {
        "self_asserted",
        # Pre-#5664 rows. Could be either case; treated as unproven because the
        # platform cannot tell which, and guessing in the permissive direction is
        # what this issue is fixing.
        "magic_link",
    }
)

# Values written by the org-placement path specifically. workspaces.py filters on
# this exact value in several predicates; named here so the two agree.
PLACEMENT_VERIFICATION: Final[str] = "org_placement"

# What a freshly-created, unproven claim is recorded as.
SELF_ASSERTED: Final[str] = "self_asserted"

# What an out-of-band-confirmed magic link is recorded as.
MAGIC_LINK_CONFIRMED: Final[str] = "magic_link_confirmed"


assert not (PROVEN_METHODS & UNPROVEN_METHODS), f"A method cannot be both proven and unproven; overlap: {sorted(PROVEN_METHODS & UNPROVEN_METHODS)}"

assert PLACEMENT_VERIFICATION in PROVEN_METHODS, "org-placement links are written by the platform itself and must count as proven"

assert SELF_ASSERTED in UNPROVEN_METHODS, "a self-asserted claim must never count as proof"

assert MAGIC_LINK_CONFIRMED in PROVEN_METHODS, "an out-of-band-confirmed magic link is proof of control"


def is_proven(verification_method: str | None) -> bool:
    """True when this method demonstrates the user controls the external account.

    Fail-closed by construction: anything not explicitly listed in
    ``PROVEN_METHODS`` is untrusted, including ``None``, the empty string, and any
    value a future writer introduces without updating this module. A new
    verification method is therefore inert until it is deliberately declared
    proven — the safe direction for a default.
    """
    return verification_method in PROVEN_METHODS
