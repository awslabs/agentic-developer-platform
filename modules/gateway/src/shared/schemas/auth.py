from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, PrivateAttr, model_validator

if TYPE_CHECKING:
    # Issue #4323: type-only. A real import would cycle —
    # src.budget.reservations -> src.budget.__init__ -> src.budget.middleware ->
    # src.shared.schemas.auth.
    from src.budget.reservations import ReservationTarget


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
    # Issue #3985 (A2): the caller's registered plane. Sourced from the
    # agent_registry entry for IAM callers; empty for human/JWT callers, which
    # are never internal-plane principals. Only scopes in INTERNAL_PLANE_SCOPES
    # may act on the internal plane.
    scope: str = ""
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
