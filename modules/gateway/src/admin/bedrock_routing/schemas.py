"""Wire shapes for the Bedrock routing admin API — Issue #4745 (#4692 · R4).

Two conventions inherited from the surrounding code, both load-bearing:

**No ``role_arn`` on any response model.** The redaction rule from
``assume_role_routes.py:241`` ("do NOT include role_arn in user-facing error") and
:mod:`src.proxy.bedrock_routing_errors` (which enforces it by having no parameter
for one) applies to this surface too. The role ARN goes to the audit row and the
server log; the *account id* is what an admin needs on screen, and ruling 1
requires that. Enforced here the same way R3 enforces it — the field does not
exist, so there is no attribute to populate by accident.

**``reason`` fields carry R3's vocabulary**
(:mod:`src.proxy.bedrock_routing_errors`), never a private one. §6.7 item 2: an
admin who sees ``assume_role_failed`` at save time and ``assume_role_failed`` at
request time must be able to connect them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: The rungs an admin may author. ``platform`` is absent because rung 4 is the
#: ABSENCE of a mapping (§1.2) — a platform row would be a second, contradictory
#: way to say "ambient IRSA".
MappingScopeType = Literal["user", "team", "org"]

#: Who authored the winning mapping, for the §1.4 display requirement. Derived, not
#: stored: see ``service.mapping_source``.
MappingSource = Literal["platform_admin", "self"]


class DestinationSummary(BaseModel):
    """A registry row as the panel's destinations table renders it.

    ``used_by`` is the count of mappings still pointing here. Under fail-closed
    (§2.5) deleting a referenced destination is an outage (§8.3), so the count is
    always shown rather than computed on demand — the reverse-lookup index
    ``ix_bedrock_account_mapping_destination`` exists for exactly this.
    """

    id: str
    connection_id: str | None = None
    account_id: str
    label: str
    region: str
    #: ``org-linked`` (promoted from a tenant's own connection) or
    #: ``admin-registered`` (platform-scoped, no owning tenant). An explicit string
    #: rather than a nullable ``owner_org_id`` the reader has to interpret — the
    #: same reason the model stores ``is_platform_registered`` as a boolean (§4.2
    #: requirement 2).
    source: Literal["org-linked", "admin-registered"]
    owner_org_id: str | None
    routing_capable: bool
    verified_at: datetime | None
    #: ``routing_capable and verified_at is not None`` — the model's own predicate,
    #: surfaced so the UI does not re-derive it and drift from the resolver.
    usable_for_routing: bool
    #: Why it is not usable, when it is not. R3's vocabulary plus R1's two probe
    #: codes. None when usable, or when it has simply never been verified.
    reason: str | None
    used_by: int


class MappingSummary(BaseModel):
    """A mapping row as the panel's rules table renders it."""

    id: str
    scope_type: MappingScopeType
    scope_id_org: str | None
    scope_id_team: str | None
    scope_id_user: str | None
    #: The wire scope string this row is addressed by — ``org:<id>``,
    #: ``team:<org>:<team>``, ``user:<user_id>``. Returned so the UI does not
    #: re-assemble it and risk producing a path the server would 422.
    scope: str
    destination_id: str
    destination_account_id: str
    destination_label: str
    destination_usable: bool
    #: Whether a platform admin or the user themselves authored this row (§1.4).
    #: Only ever ``self`` on a ``user`` rung; a team/org row has no self author.
    source: MappingSource
    updated_at: datetime


class MappingUpsertRequest(BaseModel):
    """Body for ``PUT /admin/bedrock-routing/mappings/{scope}``.

    Carries a ``destination_id`` — a reference to a registry row — and **never an
    account id**. That is design ruling 4a on the wire: an account number alone is
    unusable, because the platform needs an assumable role in the destination
    account, so accepting one would invite an admin to author a mapping that cannot
    possibly work.
    """

    destination_id: str = Field(min_length=1)


class EffectiveMappingResponse(BaseModel):
    """Answer to "who serves this person, and from which rule?" (§6.3 element 1).

    The source-rung fields are the point of this endpoint, not decoration. Showing
    a person's effective destination *without* saying which rung produced it invites
    the reader to "fix" the wrong row — the #4511 discipline applied to a UI, and
    the same labelled-source requirement #4691 applied to budget limits.
    """

    user_id: str
    #: The rung that won. ``platform`` means no mapping matched, which is an answer
    #: (today's ambient-IRSA behaviour), not an absence.
    rung: Literal["user", "team", "org", "platform"]
    account_id: str | None
    destination_id: str | None
    destination_label: str | None
    #: Who authored the winning rule; None on the platform rung, which nobody
    #: authors.
    source: MappingSource | None
    #: **The §1.4 SETTLED display.** True when the winning rule is a ``user`` rung
    #: row authored by a platform admin rather than by the person themselves — i.e.
    #: an admin has pinned this individual, which under "admin wins" is precisely the
    #: case §1.4 says the UI must state rather than imply. A setting shown as active
    #: while something else governs is the #4511 inert-config defect.
    overrides_self_selection: bool = False
    #: What would serve this person if the winning rule were removed — the mockup's
    #: "(would otherwise be ml-research via the team rule for ml-team)". The ladder
    #: walk already holds every candidate row, so this costs nothing extra, and it is
    #: what turns "remove this rule" from a guess into a known outcome. None when the
    #: platform rung already won (nothing is being shadowed).
    shadowed_rung: Literal["user", "team", "org", "platform"] | None = None
    shadowed_account_id: str | None = None


class SelectableConnection(BaseModel):
    """One of the caller's own AWS connections, as the §6.4 selector renders it.

    **Non-selectable rows are returned, not filtered out**, and that is the design
    note's requirement rather than a convenience. Per §5.0b every connection made with
    the v1 template is pinned to the person who created it and fails the assumability
    probe, so on most installs *most* of a person's connections are legitimately
    unselectable. Filtering them would leave the person an empty list and no
    explanation — the same "why is there nothing here" dead end §6.4's honest-display
    requirement exists to prevent. ``selectable`` plus ``reason`` says which and why.

    No ``role_arn`` (§2.6), and no ``secret_arn``: the account id is what identifies a
    destination to a human.
    """

    credential_id: str
    label: str
    account_id: str | None
    #: ``verified`` | ``pending`` | ``failed`` — the connection's own status, straight
    #: from ``user_credentials.scopes``. Reported even for a row that is not selectable,
    #: because "not verified yet" and "verified but not routing-capable" need different
    #: actions from the person.
    status: str
    #: May this connection be picked as a Bedrock destination? Requires ``verified``
    #: **and** routing-capable (§4.4, §5.0b). The list is an affordance — the server
    #: re-checks on write, so a caller ignoring this gets a 422, not a stored mapping.
    selectable: bool
    #: Why not, when not. R1/R3's shared vocabulary, so the same code means the same
    #: thing here, at save time, and in a runtime 502 (§6.7 item 2).
    reason: str | None = None


class MySelectionResponse(BaseModel):
    """What actually serves the caller's Bedrock calls, plus what they may pick (§6.4).

    **One response, because the screen must never show two answers.** The three facts
    it carries — the effective destination, the caller's own selection, and whether the
    two agree — are only meaningful together. A UI that read the person's stored pick
    from one endpoint and the effective destination from another would render them
    side by side and leave the reader to decide which is in force, which is the #4511
    inert-config defect with an extra step.

    ``own_selection_active`` is therefore stated by the server rather than derived by
    the client. It is False in two distinct situations that look identical from a stored
    row alone, and both are real:

    * **A platform admin has pinned the caller** (§1.4 "admin wins"), in which case
      ``effective`` reports the admin's destination and ``overrides_self_selection`` is
      true.
    * **The caller's own pick has stopped being usable** — its role was deleted, or the
      probe now fails — so the resolver skips it and walks on (§4.4). The row still
      exists and governs nothing.
    """

    #: Where the caller's calls actually go, from R4's ladder walk. ``rung`` is
    #: ``platform`` when nothing matched, which is an answer (ambient IRSA), not an
    #: absence.
    effective: EffectiveMappingResponse
    #: The caller's own user-rung selection, when they have authored one and it is still
    #: theirs. None when they have authored none, and None when a platform admin has
    #: since taken the row over — in that case the admin's choice IS the user rung and
    #: reporting the person's overwritten pick would be reporting something that no
    #: longer exists.
    own_selection_destination_id: str | None = None
    own_selection_account_id: str | None = None
    own_selection_label: str | None = None
    #: Which of the caller's ``connections`` the selection was made from, so the list can
    #: mark that row without guessing. Stated server-side because the two ids are NOT
    #: interchangeable — a mapping references a *destination* (ruling 4a) while the
    #: selector lists *credentials* — and matching on account id instead would light up
    #: the wrong row whenever one account is connected twice, which is a shape the
    #: registry explicitly allows. None when the destination predates the link.
    own_selection_credential_id: str | None = None
    #: True only when the caller's own selection is the destination in force. See the
    #: class docstring for the two different reasons it can be False.
    own_selection_active: bool = False
    #: True when a platform admin has taken the user rung. The caller may not change or
    #: clear the selection while this holds, and the screen must say so (§1.4).
    pinned_by_platform_admin: bool = False
    #: Everything the caller may pick, selectable or not. See
    #: :class:`SelectableConnection`.
    connections: list[SelectableConnection] = []


class MySelectionRequest(BaseModel):
    """Body for ``PUT /me/bedrock-routing/selection``.

    **Names a connection of the caller's own, and names no person.** There is no
    ``user_id`` field, and that absence is the access control rather than a check that
    could be dropped: the anchor is derived from the token, so a request pointing at
    somebody else cannot be formed (the ``person_cap_routes`` self-path argument).

    Takes a ``credential_id`` rather than R4's ``destination_id`` because a person owns
    connections, not registry rows — they have no way to learn a ``destination_id`` and
    no reason to. The server finds or creates the registry row for the connection, which
    is also what keeps the ownership check trivially total: the credential is looked up
    scoped to the caller, so an id belonging to anybody else simply does not resolve.
    """

    credential_id: str = Field(min_length=1)


class RegisterConnectionDestination(BaseModel):
    """Promote one of a tenant's own verified AWS connections into the registry.

    The resulting row is org-linked: ``owner_org_id`` is set from the credential's
    own tenant, which is what makes the §4.2 requirement-1 scope check meaningful
    later. An admin cannot pass the org — it is read from the credential, so there
    is no parameter with which to mislabel a connection as belonging to a tenant it
    does not.
    """

    source: Literal["connection"] = "connection"
    credential_id: str = Field(min_length=1)
    label: str | None = None


class RegisterNewDestination(BaseModel):
    """Register an account nobody has linked yet, via the Connect-AWS CFN flow.

    Ruling 4a allows this "for the case where nobody has linked the desired account
    yet", and §5.0b shows it is in fact the *only* workable path for team/org rungs
    today, because a tenant's own connection is user-owned and §4.3 forbids those.

    **``link_to_org_id`` is required, and that is the reconciliation of two
    documents that disagree.** The mockup's register modal has a "Link to org"
    select ("Rules for this org's teams and users may route to this destination"),
    while §4.2 requirement 2 describes an ``is_platform_registered`` row usable by
    *any* scope. ``ck_bedrock_destination_ownership`` permits only one of the two per
    row, so the choice is forced. Requiring the org is the safer of the two and
    loses nothing:

    * The §4.2 requirement-1 scope check stays **total** — every destination this
      API creates has an owning tenant to compare a scope against, so there is no
      row for which cross-tenant isolation is "not applicable".
    * The ruling-4a case is still served: the *admin* registers the account instead
      of waiting for a tenant to link it. Nobody had to link it first, which was the
      whole point.
    * No row is written whose NULL org means "allowed everywhere" — the leak shape
      §4.2 requirement 2 names as the thing to avoid.

    ``is_platform_registered = true`` rows remain readable (the resolver and the
    listing honour them), they are simply not something this endpoint mints.

    The response carries a **v2-template** quick-create URL. Per §5.0b the admin
    path must not use v1, or every destination it registers is born pinned to one
    user id and unusable for the team/org mappings it exists to serve.
    """

    source: Literal["new_account"] = "new_account"
    account_id: str = Field(min_length=12, max_length=12)
    label: str = Field(min_length=1, max_length=54, pattern=r"^[A-Za-z0-9-]+$")
    #: The org whose team/user/org rules may route here. Sets ``owner_org_id``.
    link_to_org_id: str = Field(min_length=1)
    region: str = Field(default="us-east-1", pattern=r"^[a-z]{2}(?:-[a-z]+)+-\d+$")

    #: **No ``role_name`` field, and the mockup's "Role name" input is therefore not
    #: wired to one.** ``aws_role_v2.yaml`` names the role ``ADP-Agent-${Nickname}``
    #: and declares no role-name parameter — deliberately, so the platform's existing
    #: IRSA grant on ``role/ADP-Agent-*`` matches without a Terraform change.
    #: Accepting a name here would let an admin type one that the stack then ignores,
    #: while ``compute_role_arn`` recorded the real ``ADP-Agent-<label>`` — a field
    #: that appears to configure something and does not. ``label`` is what names the
    #: role, which is why it is required.

    @field_validator("account_id")
    @classmethod
    def validate_account_id(cls, v: str) -> str:
        """AWS account IDs are 12 digits. Same check as ``ConnectStartRequest``."""
        if not v.isdigit():
            raise ValueError("AWS account IDs must be exactly 12 digits")
        return v


class RegisterSharedConnectionDestination(BaseModel):
    """Explicitly authorize an existing connection for one organization's Bedrock use."""

    source: Literal["shared_connection"]
    credential_id: str = Field(min_length=1)
    link_to_org_id: str = Field(min_length=1)


class ExistingAwsConnection(BaseModel):
    """Metadata only. Listing never reads a secret or assumes an AWS role."""

    credential_id: str
    label: str
    account_id: str | None
    org_id: str
    org_name: str
    owner_scope: str
    owner_name: str | None
    status: str
    selectable: bool
    reason: str | None


class RegisterDestinationResponse(BaseModel):
    """Result of registering a destination.

    ``launch_url`` is present only for the platform path, where the admin still has
    to create the role in the destination account. A promoted connection already has
    a proven role, so there is nothing to launch.

    A newly registered platform destination is **not usable until verified** — its
    ``verified_at`` is NULL and the resolver refuses those (§4.4). That is deliberate:
    merely *starting* a registration flow must not be able to reroute traffic onto an
    account that fails every call.
    """

    destination: DestinationSummary
    launch_url: str | None = None


class DestinationSetupResponse(BaseModel):
    """Sensitive, non-cacheable setup handoff; never included in list responses."""

    account_id: str
    role_arn: str
    region: str
    launch_url: str
    download_filename: str
    download_base64: str


class VerifyDestinationResponse(BaseModel):
    """Result of the on-demand re-validate action (§6.7 item 5).

    Exists so an admin diagnosing a fail-closed outage can distinguish "mapping
    wrong" from "Bedrock down" without waiting for a user to retry.
    """

    destination: DestinationSummary
    verified: bool
    reason: str | None = None
