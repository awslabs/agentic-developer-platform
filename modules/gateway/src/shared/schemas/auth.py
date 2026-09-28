from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic import BaseModel, PrivateAttr, model_validator

if TYPE_CHECKING:
    # Issue #4323: type-only. A real import would cycle —
    # src.budget.reservations -> src.budget.__init__ -> src.budget.middleware ->
    # src.shared.schemas.auth.
    from src.budget.reservations import ReservationTarget
    from src.budget.run_binding import RunBinding
    from src.orchestration.dispatch import GraphAttribution
    from src.orchestration.provider_quotes import ProviderQuote
    from src.usage.persona_attribution import PersonaUsageAttribution


class AuthExchangeRequest(BaseModel):
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str


class AuthExchangeResponse(BaseModel):
    token: str
    expires_at: datetime
    user_id: str
    org_id: str
    team_id: str
    department_id: str
    account_type: str  # "human" or "service"


class TokenContext(BaseModel):
    """Attached to every authenticated request after token validation.

    Two org fields, with a deliberate split of duties (Issue #4132):

    - ``org_id`` is **authenticated-only**. It comes from the Cognito token
      claim or the agent_registry entry and is never writable by a request
      header. It is the *sole* field any authorization path may read.
    - ``attributed_org_id`` is **caller-influenced** and must never gate
      access. Internal-plane agents may point it at the tenant that triggered
      the run (Issue #747) so usage/billing lands on that tenant. It defaults
      to ``org_id``, so every non-internal caller sees the two fields agree.

    ``attributed_user_id`` (Issue #4300) is the third attribution field and the
    only one no caller can influence at all — see its own comment below.
    """

    user_id: str
    org_id: str
    team_id: str
    department_id: str
    account_type: str  # "human" or "service"
    is_admin: bool = False
    expires_at: datetime
    auth_source: str = "jwt"  # "jwt" (Cognito) or "iam" (API Gateway)
    # Signed Cognito username, used to prove GitHub broker identity for workspace
    # resolution. Never populated from an arbitrary request header or body.
    cognito_username: str = ""
    # Issue #3985 (A2): the caller's registered plane. Sourced from the
    # agent_registry entry for IAM callers; empty for human/JWT callers, which
    # are never internal-plane principals. Only scopes in INTERNAL_PLANE_SCOPES
    # may act on the internal plane.
    scope: str = ""
    # Registry-owned requirement, never accepted from a worker request/header.
    requires_run_identity: bool = False
    # Issue #4131: the credential scopes this caller has actually been granted,
    # resolved server-side from the agent_registry entry. Empty for human/JWT
    # callers and for any agent that has not been granted one. This is the
    # authoritative source for credential-scope decisions — never a
    # caller-supplied header.
    credential_scopes: list[str] = []
    # Issue #4132: the org that usage/billing is *attributed* to. Attribution
    # only — never an authorization input. See the class docstring.
    attributed_org_id: str = ""
    # Issue #4300: the HUMAN who set this agent chain in motion, as a canonical
    # `users.id`. Attribution only — never an authorization input. If this is
    # ever read as authz, a sub-agent can act as the human who triggered it.
    #
    # WRITTEN BY THE BUDGET MIDDLEWARE, not by token validation: the value comes
    # off the server-resolved run row (src/budget/run_binding.py), which is only
    # available once the budget layer has verified the binding. Read only by
    # budget/attribution paths (the entity hierarchy and the chat-log write
    # sites, which feed the settled ledger).
    #
    # NOT settable from any request header by any caller, internal-plane or
    # otherwise — there is deliberately no header for it. Empty means "not
    # human-rooted, or not resolvable", which adds no budget entity and writes
    # no ledger row. Empty must never become a row keyed on "": that would
    # collapse every non-human-rooted request in a tenant into one bogus
    # shared ledger line.
    attributed_user_id: str = ""
    # Issue #5419 (PMM-02): the canonical service principal this service caller
    # resolves to, via a registered, tenant-scoped `(org_id, alias_source,
    # alias_id)` row in `service_principal_aliases`. Server-resolved at
    # authentication time; there is deliberately no header for it.
    #
    # ADDITIVE and OPTIONAL: `user_id` semantics are unchanged, so every existing
    # caller is unaffected. Empty means "this caller has no registered alias" —
    # which for a service caller is a refusal, never a fallback to the raw
    # subject. The raw subject is a different namespace per auth path
    # (agent_registry `agent_name`, a `service_accounts` UUID, a Cognito
    # `client_id`), and the approved design is explicit that none of them may
    # own a preference.
    #
    # Empty for human callers, which resolve through `resolve_canonical_user_id`
    # to a canonical `users.id` instead.
    canonical_service_principal_id: str = ""
    # Issue #5419: which alias source the above was resolved through. Needed
    # because `auth_source` is too coarse to distinguish a Cognito M2M client
    # from a legacy service-account exchange — both present as
    # account_type="service", auth_source="jwt".
    canonical_alias_source: str = ""
    # Issue #5420 (PMM-03): model restrictions resolved from the authenticated
    # Agent Registry row.  This is populated only by the IAM authentication
    # adapter; request headers and bodies have no path to set it.  ``None``
    # means the caller did not arrive through Agent Registry, while an empty
    # list means the registry row has no explicit restriction and therefore
    # inherits the versioned platform baseline.
    registered_allowed_models: list[str] | None = None
    # Issue #5420 (PMM-03): immutable primary key of the authenticated Agent
    # Registry row.  ``user_id`` remains the human-readable ``agent_name`` for
    # compatibility and is neither immutable nor unique, so privileged
    # internal routes must bind to this field instead.  Populated only by the
    # IAM registry adapter; no request header or body can set it.
    agent_registry_id: str = ""

    # Issue #4323: the run/chain reservation targets this request actually
    # reserved against, stashed by the budget check so the reconcile on the way
    # out can RELEASE them. Without it the run/chain reservations leak for their
    # full 24h TTL whenever a run or chain fails, and the cap reads as exhausted
    # while nothing is spending.
    #
    # It carries the very ``ReservationTarget`` objects the reserve path built
    # rather than the ids needed to rebuild them, because a released key that
    # does not byte-match the reserved key is a silent no-op. Deriving the key
    # once and carrying it forward has no drift surface; deriving it twice does.
    #
    # A PrivateAttr, not a field, on purpose:
    #
    # * pydantic does NOT populate private attributes from constructor input, so
    #   no ``TokenContext(**caller_data)`` site can inject one. That closes the
    #   #3985 class of defect by construction — this cannot become a
    #   caller-supplied cap key the way ``X-Agent-BudgetConfigId`` was.
    # * it stays out of ``model_dump()`` and the JSON schema, so no API response,
    #   log line, or serialized context changes shape.
    # * it is internal budget plumbing. Only ``enforcement_service`` writes it
    #   and only ``enforcement_service`` reads it.
    #
    # Written by the budget middleware from the server-resolved ``RunBinding``
    # (see ``src/budget/run_binding.py``), never from ``X-Agent-RunId``. Empty is
    # the norm — human/JWT callers, the feature disabled, shadow mode, and a
    # degraded registry lookup all reserve no run/chain target and so release
    # none, reconciling exactly as they did before this issue.
    _run_scope_reservations: "list[ReservationTarget]" = PrivateAttr(default_factory=list)
    _budget_admission_targets: "list[ReservationTarget] | None" = PrivateAttr(default=None)
    # Set after protected pod/grant verification, or authenticated shared-run
    # assignment and accepted policy verification in the model middleware.
    # Neither model parsing nor headers can supply a pydantic private attribute.
    _protected_run_binding: "RunBinding | None" = PrivateAttr(default=None)
    # Issue #4898: the graph node this request's model spend is attributable to,
    # for `usage_logs.graph_address`. Written ONLY by
    # `AgentModelIdentityMiddleware`, from the assignment
    # `validate_engine_authority` just proved against live SQL; read only by the
    # shared usage writer.
    #
    # A PrivateAttr for the same reason as `_protected_run_binding`, and here the
    # reason is the whole security property rather than tidiness: pydantic does
    # not populate private attributes from constructor input, so no
    # `TokenContext(**caller_data)` site, request header, body field or query
    # parameter can inject one. The address is unforgeable *by construction*
    # instead of by a validation someone must remember to write — which is what
    # #3985 (`X-Agent-BudgetConfigId`) and `draft_binding.py`'s refusal of
    # `attributed_org_id` both established as the rule on this path.
    #
    # Request-owned, and that is what makes concurrency and streaming safe: the
    # value lives on the per-request context object rather than in a contextvar
    # or any shared map, so two in-flight calls for different nodes have no
    # common state to exchange, and a stream that finalizes late reads the
    # assignment verified when it started rather than re-resolving a node that
    # may since have completed or been reassigned. A contextvar would also need
    # unconditional per-request reset and is lost across the threadpool boundary
    # (#1755); this needs neither.
    #
    # None is the norm and is honest: human/JWT/CLI/chat traffic, a flow-level or
    # wave coordinator that owns no single node, and any degraded lookup all
    # leave it None, which persists as a NULL address meaning "unavailable" —
    # never zero spend, and never a guessed node.
    _graph_attribution: "GraphAttribution | None" = PrivateAttr(default=None)
    # Issue #5426: persona/chain/preference-owner evidence derived from PMM-06's
    # protected model-policy snapshot.  A private attribute is the security
    # boundary: headers, bodies and ``TokenContext(**caller_data)`` cannot stamp
    # a persona or service principal onto their own usage rows.
    _persona_usage_attribution: "PersonaUsageAttribution | None" = PrivateAttr(default=None)
    _budget_observation_scope: str | None = PrivateAttr(default=None)
    _budget_enforcement_enabled: bool = PrivateAttr(default=True)
    _budget_provider_started: bool = PrivateAttr(default=False)
    _budget_request_timestamp: datetime = PrivateAttr(default_factory=lambda: datetime.now(UTC))
    _budget_accounting_incomplete: bool = PrivateAttr(default=False)
    _policy_flow_target: "ReservationTarget | None" = PrivateAttr(default=None)
    _policy_scope_caps: tuple[Decimal, Decimal] | None = PrivateAttr(default=None)
    _policy_estimated_cost: Decimal | None = PrivateAttr(default=None)
    _policy_request_id: str | None = PrivateAttr(default=None)
    # Issue #5225: the typed quote whose total became _policy_estimated_cost. Kept
    # alongside the amount so the reservation can be audited against the request
    # bytes, billing model and pricing revision that were actually priced —
    # rather than a number whose provenance is gone. A PrivateAttr for the same
    # reasons as the fields above: no caller can inject one.
    _policy_quote: "ProviderQuote | None" = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _default_attributed_org_id(self) -> "TokenContext":
        """Default attribution to the authenticated org when unset.

        Keeps every existing construction site and every non-internal caller
        behaving exactly as before: absent an explicit attribution override,
        attributed_org_id == org_id.
        """
        if not self.attributed_org_id:
            # Bypass validation re-entry (model_validator(mode="after") would
            # otherwise recurse on assignment when validate_assignment is on).
            object.__setattr__(self, "attributed_org_id", self.org_id)
        return self
