"""The CLI capability contract — Issue #5621 (CLI-08).

What a CLI needs before it sends a request is not one boolean but **four
independent facts**, and this module's entire reason for existing is that they
are kept apart:

* **supported** — this gateway release implements the operation at all.
* **enabled**   — this deployment has the module behind it switched on.
* **permitted** — *this* caller may perform this action in *this* tenant.
* **ready**     — the services the operation depends on are actually usable.

Collapsing any two of them produces a confidently wrong answer, and the three
collapses that matter are each a defect this platform has already shipped once:

1. **enabled → permitted.** A feature flag is a deployment-wide rollout control
   (`src/features/routes.py` is explicit that it is not tenant-scoped and not a
   security boundary). Reading `chat: true` as "you may chat" tells an ordinary
   member they can perform an operation the server will refuse — and worse, the
   inverse reading would let a CLI treat a flag as an authorization decision,
   which is precisely the control the server must keep.
2. **supported → ready.** A route existing proves the code is deployed, not that
   the queue, provider or worker behind it is reachable. `src/knowledge/routes.py`
   returns 503 for a registered route whose `INGESTION_QUEUE_URL` is unset; a
   client that inferred readiness from the route would report "ready" for an
   operation that cannot complete.
3. **unknown → false.** The most dangerous one. If "I could not determine this"
   renders as "no", a CLI refuses work the user is entitled to; if it renders as
   "yes", the CLI promises work that will fail. Either way the user is told a
   fact the server never asserted. So every axis is a THREE-valued answer and
   `UNKNOWN` is a first-class value that must be carried through to output.

Two further rules hold for every value produced here:

**This is discovery, never authorization.** Nothing in this module grants
anything. `permitted` is computed by asking the same `AccessControl` the real
routes ask, and it is advisory: the operation's own route still performs its own
check when the request actually arrives. A caller who lies about, caches, or
patches this response gains nothing — which is the property that makes it safe
to publish at all.

**Tenant scope is the shape of the call, not a parameter.** Every answer is for
the authenticated caller's own `org_id`. There is deliberately no target
argument anywhere in this module, so there is nothing for a caller to point at
another tenant. (Same reasoning as `src/budget/me_routes.py`.)
"""

from __future__ import annotations

import os

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.features.routes import _is_enabled, _is_enabled_strict
from src.shared.schemas.auth import TokenContext

# The contract version. A client compares this against the versions it
# understands; it is NOT the gateway's release. Bump it only for a change that an
# older client cannot read safely — adding an operation to the registry is not
# such a change, because an unrecognised operation ID is already required to read
# as "my CLI does not support this" rather than as an error.
SCHEMA_VERSION = "2026-09-21"

# The three-valued answer. Strings rather than `bool | None` so that the value
# survives JSON, a log line and a shell script intact: `null` invites a truthiness
# test that silently means "no", whereas an unrecognised "unknown" string breaks
# loudly in any client that forgot to handle it.
YES = "yes"
NO = "no"
UNKNOWN = "unknown"
TRISTATE = (YES, NO, UNKNOWN)


def tristate(value: bool | None) -> str:
    """Render a determination, keeping "I do not know" distinct from "no"."""
    if value is None:
        return UNKNOWN
    return YES if value else NO


class Operation:
    """One discoverable CLI operation and how each of its four axes is resolved.

    ``feature`` is the deployment flag gating the module, or None when the
    operation is part of the always-present core (sign-in, own-scope reads). A
    None feature means *enabled*, never *permitted* — see the module docstring.

    ``permission`` is the `AccessControl` permission the real route requires, or
    None when any authenticated caller in the tenant may perform it. None here is
    a deliberate, reviewed statement that the operation is self-scoped, not an
    "unset" default: an operation whose permission nobody has decided must not
    silently read as open.

    ``readiness`` names the dependency class whose availability is checked by
    `_READINESS`. None means the operation has no dependency beyond the gateway
    itself, so `supported` already covers it and readiness is reported YES.

    ``mutates`` classifies the operation for the command manifest. Read
    operations are safe to attempt in order to diagnose; mutations are the ones a
    CLI must refuse *before sending* when the evidence is definitive.
    """

    __slots__ = (
        "id",
        "feature",
        "permission",
        "platform_admin",
        "readiness",
        "mutates",
        "summary",
    )

    def __init__(
        self,
        operation_id,
        *,
        summary,
        feature=None,
        permission=None,
        platform_admin=False,
        readiness=None,
        mutates=False,
    ):
        if permission is not None and platform_admin:
            raise ValueError("an operation cannot require both a permission and platform-admin authority")
        self.id = operation_id
        self.summary = summary
        self.feature = feature
        self.permission = permission
        self.platform_admin = platform_admin
        self.readiness = readiness
        self.mutates = mutates


# Flags whose absence means OFF. Mirrors the strict set in src/features/routes.py:
# a rollout control whose documented rollback is "unset it" must not read as
# enabled when unset, or the rollback does not work. Any flag not listed here is
# resolved with the fail-open reader, matching that module's core-flag behaviour.
_STRICT_FEATURES = frozenset(
    {
        "FEATURE_ORCHESTRATION_ENGINE_ENABLED",
        "FEATURE_BUDGET_SPEND_ENABLED",
        "FEATURE_AGENT_CONTROL_ENABLED",
        "FEATURE_AGENT_MODELS_ENABLED",
        "FEATURE_SUPERPLANE_ENABLED",
        "FEATURE_GITLAB_ENABLED",
        "FEATURE_NEW_UI_ENABLED",
    }
)

# Fail-open flags that inherit from a fallback var when their own is unset,
# mirroring src/features/routes.py so discovery and the flags endpoint cannot
# disagree about the same deployment.
_FEATURE_FALLBACKS = {
    "FEATURE_KNOWLEDGE_ENABLED": "AGENT_CONTEXT_ENABLED",
    "FEATURE_INDEXING_ENABLED": "AGENT_CONTEXT_ENABLED",
}


def _feature_enabled(flag: str | None) -> bool:
    """Resolve a deployment feature flag exactly as the flags endpoint does."""
    if flag is None:
        # Core surface — no flag gates it. Enabled, which says nothing at all
        # about whether this caller may use it.
        return True
    if flag in _STRICT_FEATURES:
        return _is_enabled_strict(flag)
    return _is_enabled(flag, _FEATURE_FALLBACKS.get(flag))


# Readiness probes. Each returns True (usable), False (definitively not usable)
# or None (undeterminable from here, which must surface as UNKNOWN).
#
# Every probe is a configuration read. None of them calls a provider, starts a
# worker, or performs inference: a diagnostic that spends money or changes state
# to answer "are you ready?" is not a diagnostic. Where the only honest way to
# know would be to make such a call, the probe returns None on purpose — an
# explicit "unknown" is strictly more useful than a guess, because the client can
# report it as unknown rather than promising or denying the operation.


def _queue_ready() -> bool:
    """Knowledge ingestion needs its queue URL, or its route 503s (#2047)."""
    return bool(os.environ.get("INGESTION_QUEUE_URL"))


def _agent_worker_ready() -> bool | None:
    """Hosted agent readiness, as far as configuration can honestly establish it.

    A configured dispatch queue is a *precondition*, not proof: pods scale from
    zero and a queue can exist with nothing draining it. So a missing queue is a
    definite NO, while a present one is UNKNOWN rather than YES — the gateway
    cannot see whether a worker is actually consuming without asking one to run,
    which is exactly the paid/stateful probe this contract forbids by default.
    """
    if not os.environ.get("AGENT_DISPATCH_QUEUE_URL") and not os.environ.get("WEBHOOK_QUEUE_URL"):
        return False
    return None


def _model_route_ready() -> bool | None:
    """Model routing readiness is a per-caller resolution, not a global fact.

    Whether a usable route exists depends on the caller's own destination,
    org/team/platform fallbacks and certification evidence — resolved by the
    routing layer per request. Answering YES from deployment config here would be
    the `supported → ready` collapse this module exists to prevent, so this is
    reported UNKNOWN and `adp doctor` resolves the effective route through the
    existing per-caller endpoint instead.
    """
    return None


_READINESS = {
    "ingestion_queue": _queue_ready,
    "agent_worker": _agent_worker_ready,
    "model_route": _model_route_ready,
}


# The operation registry. Feature owners add their own operations here as they
# land; this is the checked list the CLI manifest is held against, which is why it
# names real permissions and real flags rather than free text.
#
# IDs are `area.action[.scope]` and are STABLE — a client keys off them, so an ID
# is renamed only with a schema version bump.
OPERATIONS = (
    Operation("vault.credentials.register", summary="Register an own credential; shared scopes require additional server authority", mutates=True),
    Operation("vault.credentials.metadata", summary="Update visible credential metadata with revision and ownership checks", mutates=True),
    Operation("vault.credentials.delete", summary="Delete an authorized credential; running work is not stopped", mutates=True),
    Operation("vault.identities.claim", summary="Record an own unverified identity claim; does not establish access", mutates=True),
    Operation("vault.identities.unlink", summary="Unlink an own external identity", mutates=True),
    Operation(
        "auth.session.read",
        summary="Read the signed-in session and its expiry",
    ),
    Operation(
        "capabilities.read",
        summary="Read this capability document",
    ),
    Operation(
        "budget.self.read",
        summary="Read your own cap and settled spend",
    ),
    Operation(
        "budget.managed.read",
        summary="Read budgets for entities you administer",
        permission=Permission.BUDGET_READ,
    ),
    Operation(
        "usage.self.read",
        summary="Read your own request history",
    ),
    Operation(
        "usage.managed.read",
        summary="Read tenant usage and request logs",
        permission=Permission.USAGE_READ,
    ),
    Operation(
        "logs.request.read",
        summary="Look up one tenant request by gateway request ID",
        feature="FEATURE_LOGS_ENABLED",
        permission=Permission.LOGS_READ,
    ),
    Operation(
        "models.catalog.read",
        summary="Read the persona model catalogue",
        feature="FEATURE_AGENT_MODELS_ENABLED",
        readiness="model_route",
    ),
    Operation(
        "models.mapping.self.write",
        summary="Set or reset your own persona model mapping",
        feature="FEATURE_AGENT_MODELS_ENABLED",
        readiness="model_route",
        mutates=True,
    ),
    Operation(
        "models.mapping.managed.write",
        summary="Set or reset a managed service principal model mapping",
        feature="FEATURE_AGENT_MODELS_ENABLED",
        permission=Permission.ORG_UPDATE,
        readiness="model_route",
        mutates=True,
    ),
    Operation(
        "agents.activity.read",
        summary="Read your own agent runs",
        readiness="agent_worker",
    ),
    Operation(
        "agents.control.write",
        summary="Send a live control command to a running agent",
        feature="FEATURE_AGENT_CONTROL_ENABLED",
        readiness="agent_worker",
        mutates=True,
    ),
    Operation(
        "knowledge.asset.write",
        summary="Register a knowledge asset for indexing",
        feature="FEATURE_KNOWLEDGE_ENABLED",
        readiness="ingestion_queue",
        mutates=True,
    ),
    Operation(
        "flows.read",
        summary="Read AI-DLC delivery flows",
        feature="FEATURE_ORCHESTRATION_ENGINE_ENABLED",
    ),
    Operation(
        "flows.approve.write",
        summary="Answer a delivery gate",
        feature="FEATURE_ORCHESTRATION_ENGINE_ENABLED",
        permission=Permission.PLAN_APPROVE,
        mutates=True,
    ),
    Operation(
        "connections.aws.read",
        summary="Read your own AWS connections",
        feature="FEATURE_CONNECTIONS_ENABLED",
    ),
    Operation(
        "connections.aws.verify.write",
        summary="Refresh stored verification evidence for your own AWS connection",
        feature="FEATURE_CONNECTIONS_ENABLED",
        mutates=True,
    ),
    Operation(
        "connections.aws.write",
        summary="Connect or disconnect your own AWS account",
        feature="FEATURE_CONNECTIONS_ENABLED",
        mutates=True,
    ),
    Operation(
        "routing.bedrock.own.read",
        summary="Read your effective Bedrock routing",
    ),
    Operation(
        "routing.bedrock.read",
        summary="Read managed Bedrock destinations and targeted routing",
        platform_admin=True,
    ),
    Operation(
        "routing.bedrock.verify.write",
        summary="Refresh stored verification and routing evidence for a Bedrock destination",
        platform_admin=True,
        mutates=True,
    ),
    Operation(
        "routing.bedrock.write",
        summary="Provision, verify and assign managed Bedrock routing",
        platform_admin=True,
        mutates=True,
    ),
    Operation(
        "github.app.admin.read",
        summary="Read deployment GitHub App status",
        platform_admin=True,
    ),
    Operation(
        "github.app.admin.setup.write",
        summary="Register or import the deployment GitHub App",
        platform_admin=True,
        mutates=True,
    ),
    Operation(
        "github.app.admin.revalidate.write",
        summary="Refresh stored GitHub App configuration evidence",
        platform_admin=True,
        mutates=True,
    ),
    Operation(
        "github.connection.read",
        summary="Read your GitHub connection status",
    ),
    Operation(
        "github.connection.write",
        summary="Connect an authorized GitHub repository",
        mutates=True,
    ),
    Operation(
        "flows.draft.write",
        summary="Create or refine an inert delivery flow draft",
        feature="FEATURE_ORCHESTRATION_ENGINE_ENABLED",
        permission=Permission.PLAN_DRAFT,
        mutates=True,
    ),
    Operation(
        "superplane.workspace.read",
        summary="Read Superplane workspace resources",
        feature="FEATURE_SUPERPLANE_ENABLED",
    ),
    Operation(
        "superplane.workspace.write",
        summary="Change Superplane workspace resources",
        feature="FEATURE_SUPERPLANE_ENABLED",
        mutates=True,
    ),
)

BY_ID = {operation.id: operation for operation in OPERATIONS}


def gateway_release() -> dict:
    """The deployed gateway revision, or an explicit statement that it is unknown.

    Nothing currently injects a release identifier into the pod environment, so
    the honest answer on a real deployment today is "unknown". That is reported as
    a state rather than an empty string or a placeholder version, because a client
    comparing versions must be able to tell "this gateway is older than the
    operation I want" from "I could not establish what this gateway is" — the
    second is not grounds for assuming either answer.
    """
    for variable in ("GATEWAY_RELEASE", "GIT_SHA", "IMAGE_TAG"):
        value = os.environ.get(variable, "").strip()
        if value:
            return {"state": YES, "release": value, "source": variable}
    return {"state": UNKNOWN, "release": "", "source": ""}


async def _permitted(operation: Operation, caller: TokenContext, access: AccessControl | None) -> bool | None:
    """Whether this caller may perform this operation, in their own tenant.

    Three outcomes, and the third is the one that matters: if the permission
    lookup itself fails (the role store is unreachable), this returns None. It
    does NOT return False. A transport fault is not a denial — reporting it as one
    would tell a properly authorized user they lack a permission they hold, and a
    client refusing a mutation on that basis would be refusing on evidence the
    server never gave.
    """
    if operation.permission is None:
        if operation.platform_admin:
            return caller.is_admin
        # Self-scoped: any authenticated caller may act on their own records. The
        # route still scopes the data to the token, which is what makes this true.
        return True
    if access is None:
        return None
    try:
        return await access.check_permission_for_discovery(caller, operation.permission, target_org_id=caller.org_id)
    except Exception as exc:  # noqa: BLE001 - denial is separated from an unavailable authority
        # AccessControl signals denial by RAISING AccessDeniedError, so a denial
        # and an outage arrive through the same channel and must be separated by
        # type. Any other failure falls through to UNKNOWN rather than being read
        # as "no".
        if isinstance(exc, AccessDeniedError):
            return False
        return None


def _ready(operation: Operation) -> bool | None:
    if operation.readiness is None:
        return True
    probe = _READINESS.get(operation.readiness)
    if probe is None:
        # An operation naming a dependency class with no probe is a registry
        # mistake. UNKNOWN is the fail-safe reading: it neither promises the
        # operation works nor denies a user access on the strength of a typo.
        return None
    try:
        return probe()
    except Exception:  # noqa: BLE001 - a probe fault is unknown, never "not ready"
        return None


async def describe(caller: TokenContext, access: AccessControl | None = None) -> dict:
    """The full capability document for one authenticated caller in one tenant.

    Read-only: this resolves flags, permissions and configuration, and writes
    nothing anywhere.
    """
    operations = []
    for operation in OPERATIONS:
        enabled = _feature_enabled(operation.feature)
        operations.append(
            {
                "id": operation.id,
                "summary": operation.summary,
                # Supported is unconditional here: the operation is in this
                # gateway's registry, so this release implements it. A client
                # learns "unsupported" from an ID being ABSENT — which is how an
                # older server correctly reports an operation it has never heard
                # of, without needing to know the newer client's vocabulary.
                "supported": YES,
                "enabled": tristate(enabled),
                "permitted": tristate(await _permitted(operation, caller, access)),
                "ready": tristate(_ready(operation)),
                "mutates": operation.mutates,
                "feature_flag": operation.feature or "",
                "required_permission": (operation.permission.value if operation.permission else "platform:admin" if operation.platform_admin else ""),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "gateway": gateway_release(),
        # Echoed so a client can prove which tenant the answer describes and
        # refuse to apply a cached document to a different one. Never accepted
        # as input — it comes only from the validated token.
        "tenant": {"org_id": caller.org_id},
        "operations": operations,
    }
