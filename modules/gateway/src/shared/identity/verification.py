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

# An administrator mapped a CHANNEL or WORKSPACE to a tenant, and an inbound event
# from that channel carried this external account id in its body. See
# CHANNEL_PLACEMENT below for why that is placement, not proof.
CHANNEL_PLACEMENT: Final[str] = "channel_placement"

# The claimed account was never contacted. Recorded so the claim is visible and
# auditable, rather than silently absent — but it is not proof of anything.
UNPROVEN_METHODS: Final[frozenset[str]] = frozenset(
    {
        "self_asserted",
        # Pre-#5664 rows. Could be either case; treated as unproven because the
        # platform cannot tell which, and guessing in the permissive direction is
        # what this issue is fixing.
        "magic_link",
        # Auto-provisioned by POST /internal/v1/resolve-user on a channel_tenant_map
        # hit. Previously written as ``admin_manual``, which put it in PROVEN_METHODS
        # and is the second half of the A10 finding: an administrator mapping a
        # workspace to a tenant is an accountable act, but it asserts a fact about
        # the WORKSPACE, not about who controls a particular account inside it. The
        # account id itself arrived in the request body and nobody verified it, so
        # this is structurally the same unproven claim as ``self_asserted`` — it just
        # happens to arrive through a mapped channel.
        CHANNEL_PLACEMENT,
    }
)

# Values written by the org-placement path specifically. workspaces.py filters on
# this exact value in several predicates; named here so the two agree.
PLACEMENT_VERIFICATION: Final[str] = "org_placement"

# ---------------------------------------------------------------------------
# Identification is not authorization — #5664 (A10)
# ---------------------------------------------------------------------------
#
# Two different questions get asked of `user_identities`, and collapsing them is
# what made the auto-provision path a proof-manufacturing route:
#
#   "WHICH platform user is this external account?"  — routing / attribution
#   "Has anyone demonstrated they CONTROL it?"       — authority
#
# A row can legitimately answer the first and not the second. The auto-provisioned
# shadow row is exactly that: the platform created it to have something stable to
# attribute a channel's messages to, and it must keep answering the routing
# question — otherwise every inbound message re-provisions another shadow user and
# re-issues another magic link forever, which is why the previous slice left the
# mislabel in place rather than relabelling it.
#
# So the fix is not to hide the row; it is to stop the ROUTING answer from carrying
# an unearned authority claim. Resolution looks up IDENTIFYING_METHODS (a superset
# of PROVEN_METHODS); anything that mints authority keeps asking `is_proven`.
IDENTIFYING_METHODS: Final[frozenset[str]] = PROVEN_METHODS | {CHANNEL_PLACEMENT}

# ---------------------------------------------------------------------------
# HOW a magic link reached the account it claims — #5664 (A10), second pass
# ---------------------------------------------------------------------------
#
# The first pass on this issue assumed the in-channel path was proof, on the
# reasoning that "only someone who can read that channel can complete it". That
# reasoning does not hold, and the gap it left is the reason this vocabulary
# exists.
#
# `_handle_unresolved_user` in the ingest Lambda does not send a direct message.
# It returns the link in the handler's HTTP response body, which the channel
# adapter posts back to the SAME conversation the triggering message arrived in.
# For a public Slack channel or a GitHub issue thread, that is a link readable by
# every member of the channel. "Can read the channel" is a far weaker fact than
# "controls the account", and it is not the fact an identity link asserts.
#
# Two independent conditions have to hold before a confirmed link is evidence of
# ownership:
#
# 1. DELIVERY was private to the claimed account — a provider DM, or an
#    assertion the provider itself signs. A post in a shared conversation is not.
# 2. The nonce was BOUND to a specific platform user. An internal nonce carries
#    `target_user_id=None` so that the recipient may pick their own account on the
#    landing page — which also means any signed-in user who obtains the link can
#    consume it. Unbound plus publicly-readable is precisely the squatting path.
#
# Both are recorded on the nonce so the consume path decides from stored facts
# rather than from the name of the route that happened to mint it.

# Delivered privately to the claimed account, or asserted by the provider.
DELIVERY_PROVIDER_DM: Final[str] = "provider_dm"
DELIVERY_PROVIDER_ASSERTED: Final[str] = "provider_asserted"

# Posted into a conversation that others can read. Confirms channel access only.
DELIVERY_SHARED_CHANNEL: Final[str] = "shared_channel"

# Minted before this distinction existed: the platform cannot tell which it was.
DELIVERY_UNKNOWN: Final[str] = "unknown"

# Only these establish that the confirming party controls the claimed account.
OWNERSHIP_PROVING_DELIVERY: Final[frozenset[str]] = frozenset(
    {
        DELIVERY_PROVIDER_DM,
        DELIVERY_PROVIDER_ASSERTED,
    }
)

# What a freshly-created, unproven claim is recorded as.
SELF_ASSERTED: Final[str] = "self_asserted"

# What an out-of-band-confirmed magic link is recorded as.
MAGIC_LINK_CONFIRMED: Final[str] = "magic_link_confirmed"


assert not (PROVEN_METHODS & UNPROVEN_METHODS), f"A method cannot be both proven and unproven; overlap: {sorted(PROVEN_METHODS & UNPROVEN_METHODS)}"

assert PLACEMENT_VERIFICATION in PROVEN_METHODS, "org-placement links are written by the platform itself and must count as proven"

assert SELF_ASSERTED in UNPROVEN_METHODS, "a self-asserted claim must never count as proof"

assert MAGIC_LINK_CONFIRMED in PROVEN_METHODS, "an out-of-band-confirmed magic link is proof of control"

assert CHANNEL_PLACEMENT in UNPROVEN_METHODS, "workspace-to-tenant placement is not proof that anyone controls a given account inside it"

assert CHANNEL_PLACEMENT not in PROVEN_METHODS, "channel placement must never mint protected authority"

# The superset direction is the load-bearing one: IDENTIFYING must cover every
# proven method, or a genuinely-proven row would stop resolving for routing.
assert PROVEN_METHODS < IDENTIFYING_METHODS, "identifying methods must be a strict superset of proven methods"

assert not (OWNERSHIP_PROVING_DELIVERY & {DELIVERY_SHARED_CHANNEL, DELIVERY_UNKNOWN}), (
    "a shared-channel or unknown delivery must never count as private delivery"
)


def delivery_proves_ownership(delivery_method: str | None) -> bool:
    """True when this delivery reached the claimed account and nobody else.

    Fail-closed for the same reason as ``is_proven``: ``None`` covers both a nonce
    row written before this column existed and a future minter that forgets to set
    it, and in neither case has the platform observed a private delivery. A new
    delivery channel is inert until it is declared proving here.
    """
    return delivery_method in OWNERSHIP_PROVING_DELIVERY


def identifies(verification_method: str | None) -> bool:
    """True when this row may be used to say WHICH platform user an account is.

    Weaker than ``is_proven`` on purpose, and the two must not be swapped: a caller
    that grants authority, spends budget, or attributes an approval wants
    ``is_proven``. This one answers only "is this row a usable routing target",
    which an auto-provisioned channel placement is even though it proves nothing.

    Fail-closed for the same reason as ``is_proven``: unknown values, ``None`` and
    ``""`` identify nothing, so a row whose provenance was never recorded does not
    become a routing target by default.
    """
    return verification_method in IDENTIFYING_METHODS


def is_proven(verification_method: str | None) -> bool:
    """True when this method demonstrates the user controls the external account.

    Fail-closed by construction: anything not explicitly listed in
    ``PROVEN_METHODS`` is untrusted, including ``None``, the empty string, and any
    value a future writer introduces without updating this module. A new
    verification method is therefore inert until it is deliberately declared
    proven — the safe direction for a default.
    """
    return verification_method in PROVEN_METHODS
