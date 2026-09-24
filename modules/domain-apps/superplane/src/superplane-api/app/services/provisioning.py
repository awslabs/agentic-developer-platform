"""Workspace provisioning through an authorized operation, not a foreign dispatch.

Issue #5058 (U17b), EPIC #4910. R14, upstream half, acceptance 1.

## What this replaces, and why the old shape was unsafe

Until this module, creating a workspace posted to GitHub's ``workflow_dispatch``
endpoint (the deleted ``app/services/github.py``) to start
``bootstrap-workspace.yml`` in ``aws-innovate/AISuperPlane`` — a repository this
project does not own — authenticated with a long-lived personal access token held
in configuration. Deleting a workspace did the same with
``teardown-workspace.yml``. Three consequences:

* **A credential for a foreign repository.** Long-lived, no expiry tied to the
  work it authorizes, and no revocation this side controls.
* **A product operation gated on GitHub Actions being up.**
* **No operation record on this side.** Progress was whatever a run in another
  repository reported, so there was nothing to cancel, bound or observe — and the
  old call sites *ignored* the dispatch result, logging a warning and still
  returning ``201 Created`` with ``status=Provisioning``. A user was told their
  workspace was being built when nothing was building it.

The replacement is U17a's shape (``contracts/superplane_contracts/provisioning.py``,
issue #5052): an operation binding that must exist before provisioning starts, a
principal resolved server-side from that binding, and progress that is a report
*about* the operation rather than a field this side sets on itself.

## Why there is no GitHub client, no PAT and no provider client here

The same reason U17a's adapter has none: the property "cannot bypass the facade"
holds by *absence* rather than by a check a later edit could remove. There is no
HTTP client in this module, so there is no dispatch call to re-enable; no token is
read, so there is no credential to leak; and no cloud-provider client, so the
"facade unavailable, provision directly instead" fallback is not something a call
site can reach for. When the facade is absent this module raises
``ProvisioningUnavailable`` and the caller surfaces a refusal.

That refusal remains the behavior whenever no facade is installed, and it is still
the honest outcome: fail-closed with a named unavailability, never a fabricated
``Provisioning`` status.

What has changed is *whether* one is installed. An earlier version of this section
said B's operation facade "does not exist in ADP (there is no
``modules/harness/jobs/``)". That is no longer true, and the staleness was
load-bearing in the same way the build-context claim below was: ``harness_jobs``
exists, ships ``OperationFacadeService``, and is now staged into the image by
``scripts/stage-domain-auth.sh``. ``app/adapters/harness_operation_facade.py``
adapts it to the ``OperationFacade`` Protocol above and ``app/composition.py``
installs it when the deployment configures a database for it.

The absence properties in the paragraph above are unaffected, because they are
properties of *this module*: it still holds no HTTP client, no token and no
provider client, so there is still no path here that could bypass whatever facade
is installed. What the adapter adds is a real implementation behind the port, not
a second way to reach the provider.

## Why the contract's values are mirrored here rather than imported

``superplane_contracts`` is standard-library-only and lives one directory above
this component, outside the Docker build context that ``releases/build-image.sh``
pins to ``src/superplane-api``.

An earlier version of this section said that made the package "genuinely not
importable at API runtime". That was **not accurate**, and the correction matters
because the inaccuracy was load-bearing: three modules in this same tree already
imported it at runtime (``app/routers/heartbeat.py``, ``app/services/leases.py``,
``app/services/observations.py``), and issue #5053 added ``app/main.py``. The
package was not unimportable — it was *unstaged*, which is a build defect rather
than a property of the architecture. Measured: with the auth package staged and
this one absent, ``import app.main`` raises ``ModuleNotFoundError`` at
``app/main.py:11``, so the image would have failed at startup rather than
degrading.

``scripts/stage-domain-auth.sh`` now stages this package into the build context
alongside ``superplane_auth``, and the Dockerfile fails the build by name if
either is missing. Widening the build context to the module root — the durable fix
that would make staging unnecessary — remains release-owned (#5327).

The mirroring below is therefore kept for the reason the contract itself gives for
``REQUIRED_PERMISSION``, and **not** for the import-availability reason: CI puts
the two packages on ``sys.path`` independently, so an import would couple this
module's test lane to the contract's, whereas a drifting duplicate is caught by a
test that fails. ``tests/test_workspaces.py`` asserts the action names, states and
forbidden-key set agree with ``superplane_contracts``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# The two provisioning verbs, mirroring `superplane_contracts.provisioning`.
# Teardown is here rather than in a separate module for the reason the contract
# gives: it is the same authority question with the opposite effect, and giving it
# its own module would invite giving it its own weaker authority check.
PROVISION = "provision"
TEARDOWN = "teardown"

PROVISIONING_ACTIONS: frozenset[str] = frozenset({PROVISION, TEARDOWN})

# The permission an operation must carry. String form of
# `superplane_auth.policy.Permission.PROVISION`.
REQUIRED_PERMISSION = "workspace:provision"

# Operation states as reported *by the facade*. `UNKNOWN` is deliberately not a
# synonym for failure: a consumer that collapses the two either leaks
# infrastructure it believes was never created, or retries a provision that
# actually succeeded.
STATE_PENDING = "pending"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_UNKNOWN = "unknown"

TERMINAL_STATES: frozenset[str] = frozenset(
    {STATE_SUCCEEDED, STATE_FAILED, STATE_CANCELLED, STATE_UNKNOWN}
)

# States from which nothing may be concluded about the provider. Named so a
# consumer cannot read "not failed" as "succeeded".
INCONCLUSIVE_STATES: frozenset[str] = frozenset(
    {STATE_PENDING, STATE_RUNNING, STATE_UNKNOWN}
)

# Parameter keys that may never appear in a provisioning request, because each one
# asserts an identity the caller does not get to assert. Mirrors
# `superplane_contracts.provisioning.FORBIDDEN_PARAMETER_KEYS`.
FORBIDDEN_PARAMETER_KEYS: frozenset[str] = frozenset(
    {
        "user_id",
        "user",
        "username",
        "org_id",
        "org",
        "organization",
        "organization_id",
        "tenant",
        "tenant_id",
        "workspace",
        "workspace_id",
        "principal",
        "subject",
        "sub",
        "on_behalf_of",
        "impersonate",
        "account_type",
        "role",
        "permission",
        "permissions",
    }
)

# Stated as a prefix family rather than a fixed list, because "strip the ones we
# thought of" is the failure mode this guards against.
FORBIDDEN_PARAMETER_PREFIXES: tuple[str, ...] = ("x-", "adp_", "auth_", "caller_")

_FORBIDDEN_COMPACT: frozenset[str] = frozenset(
    name.replace("_", "") for name in FORBIDDEN_PARAMETER_KEYS
)


class ProvisioningError(Exception):
    """Base for provisioning failures that a router turns into a response."""


class ProvisioningUnavailable(ProvisioningError):
    """No operation facade is configured, so provisioning cannot be authorized.

    Deliberately **not** a silent no-op and deliberately not a fallback. The old
    code's warning-and-continue is what let a workspace sit in ``Provisioning``
    with nothing provisioning it. A caller must surface this as a failure.
    """


class ProvisioningRefused(ProvisioningError, PermissionError):
    """A well-formed request the service is not willing to run.

    A ``PermissionError`` subclass, matching ``AuthorizationDeniedError`` in U9's
    policy and ``ProvisioningRefused`` in U17a's adapter, so a caller that treats
    authorization failures uniformly catches this too.
    """


def forbidden_parameters(parameters: dict[str, str]) -> tuple[str, ...]:
    """Identity-asserting keys present in a parameter map.

    Returns offending keys in iteration order so a refusal can name them.
    Case-insensitive and hyphen/underscore-insensitive, because ``Org-Id`` is the
    same smuggling attempt as ``org_id`` and a case-sensitive check is a bypass
    with an obvious recipe.
    """
    offending: list[str] = []
    for key in parameters:
        lowered = key.strip().lower().replace("-", "_")
        compact = lowered.replace("_", "")
        if compact in _FORBIDDEN_COMPACT or any(
            lowered.startswith(prefix.replace("-", "_"))
            for prefix in FORBIDDEN_PARAMETER_PREFIXES
        ):
            offending.append(key)
    return tuple(offending)


@dataclass(frozen=True)
class OperationProgress:
    """Progress for one operation, as reported through the facade.

    Frozen, and the service holds no status field of its own — a mutable status on
    the service is exactly what a caller or a test would read instead of asking
    the facade, and "reports success while the provider has provisioned nothing"
    is the failure this arrangement exists to prevent.
    """

    operation_id: str
    state: str
    detail: str | None = None

    @property
    def is_terminal(self) -> bool:
        """True when the facade will report no further change."""
        return self.state in TERMINAL_STATES

    @property
    def is_conclusive_success(self) -> bool:
        """True only for a reported success. ``unknown`` is not success."""
        return self.state == STATE_SUCCEEDED

    @property
    def is_conclusive_failure(self) -> bool:
        """True only for a reported failure. ``unknown`` is **not** a failure."""
        return self.state in (STATE_FAILED, STATE_CANCELLED)


@runtime_checkable
class OperationFacade(Protocol):
    """B's scoped trusted-operation contract, as this service consumes it.

    A ``Protocol`` rather than a base class: B owns the implementation and it does
    not exist yet, so there is nothing here for an implementation to inherit.
    Structural typing also means a test double satisfies exactly the surface used
    and nothing more, so it cannot drift into offering conveniences a real facade
    would not.
    """

    async def open_operation(
        self,
        *,
        action: str,
        workspace_id: str,
        org_id: str,
        permission: str,
        parameters: dict[str, str],
    ) -> OperationProgress:
        """Authorize and start one operation, returning the facade's first report.

        The facade resolves the principal itself and binds the operation to it.
        ``org_id`` comes from the caller's *verified* token at the API boundary,
        never from a request body — and the authoritative principal is the
        facade's, not this argument.
        """
        ...

    async def report_progress(self, operation_id: str) -> OperationProgress:
        """The facade's current report. The only way an outcome is learned."""
        ...


# Module-level facade, injected at application wiring time. `None` — the value in
# this repository today — means provisioning refuses rather than falls back.
_facade: OperationFacade | None = None


def set_operation_facade(facade: OperationFacade | None) -> None:
    """Install (or clear) the operation facade used for provisioning."""
    global _facade
    _facade = facade


def get_operation_facade() -> OperationFacade | None:
    """The installed facade, or ``None`` when provisioning is unavailable."""
    return _facade


def uninstall_operation_facade(facade: OperationFacade) -> bool:
    """Remove `facade` if it is the installed one. Returns whether it was.

    Identity-scoped so a composition's shutdown releases only its own facade. The
    failure this prevents is specific and was real: a composition that closed its
    transport and left the facade installed handed the *next* startup a facade
    over a closed connection pool, which no probe could distinguish from a healthy
    one until a request arrived. See `app/composition.py:Composition.aclose`.
    """
    global _facade
    if _facade is not facade:
        return False
    _facade = None
    return True


def _require_facade() -> OperationFacade:
    facade = _facade
    if facade is None:
        # No fallback branch exists here on purpose. "Facade unavailable, so
        # provision directly" is the substitution R14 forbids: it is a broader
        # authority that is *available* where the correct one is *not built*.
        raise ProvisioningUnavailable(
            "Workspace provisioning is unavailable: no authorized-operation "
            "facade is configured. Provisioning is not attempted by any other "
            "path."
        )
    return facade


def _check_parameters(parameters: dict[str, str]) -> None:
    """Refuse a parameter map that asserts an identity.

    The important case is the one that looks harmless: an ``org_id`` that
    *matches* the caller's real organization. It is still refused. A
    caller-supplied identity that agrees with the binding today is a code path
    that reads the caller's value, and once that path exists the only thing
    stopping a mismatched value from being honoured is that something else
    happens to compare them — and comparisons get reordered, cached, or made
    conditional. Refusing regardless means no path reads caller-supplied identity
    at all, which is a property rather than a coincidence.
    """
    offending = forbidden_parameters(parameters)
    if offending:
        raise ProvisioningRefused(
            "provisioning parameters may not assert an identity; remove: "
            f"{', '.join(sorted(offending))}. The principal is resolved from the "
            "operation binding."
        )


async def _start(
    *, action: str, workspace_id: str, org_id: str, parameters: dict[str, str]
) -> OperationProgress:
    """Open an authorized operation, or raise. Shared by both verbs."""
    if action not in PROVISIONING_ACTIONS:
        raise ProvisioningRefused(f"unknown provisioning action: {action!r}")
    _check_parameters(parameters)
    facade = _require_facade()

    progress = await facade.open_operation(
        action=action,
        workspace_id=workspace_id,
        org_id=org_id,
        permission=REQUIRED_PERMISSION,
        parameters=parameters,
    )
    if not isinstance(progress, OperationProgress):
        # A facade returning something else is a contract breach on B's side, and
        # it must surface here rather than being handed to a caller who would read
        # `.state` off an arbitrary object.
        raise ProvisioningError("facade progress report is not an OperationProgress")
    logger.info(
        "Opened %s operation %s for workspace %s (state=%s)",
        action,
        progress.operation_id,
        workspace_id,
        progress.state,
    )
    return progress


async def start_provision(
    *,
    workspace_id: str,
    org_id: str,
    workspace_name: str,
    isolation_mode: str,
    account: str = "",
) -> OperationProgress:
    """Begin provisioning a workspace under an authorized operation.

    ``workspace_name`` and ``isolation_mode`` are provisioning *shape*, not
    identity: they describe what to build. The workspace and organization the
    operation acts on are bound by the facade.
    """
    parameters: dict[str, str] = {
        "workspace_name": workspace_name,
        "isolation_mode": isolation_mode,
    }
    if account:
        parameters["aws_account_id"] = account
    return await _start(
        action=PROVISION,
        workspace_id=workspace_id,
        org_id=org_id,
        parameters=parameters,
    )


async def start_teardown(
    *, workspace_id: str, org_id: str, workspace_name: str
) -> OperationProgress:
    """Begin tearing down a workspace under an authorized operation."""
    return await _start(
        action=TEARDOWN,
        workspace_id=workspace_id,
        org_id=org_id,
        parameters={"workspace_name": workspace_name},
    )


async def observe(operation_id: str) -> OperationProgress:
    """Read current progress for an operation, through the facade.

    Exposed so a caller polling for completion has a supported way to do it that
    is not "read a field on the service".
    """
    if not operation_id or not operation_id.strip():
        raise ProvisioningError("progress must name the operation it describes")
    facade = _require_facade()
    progress = await facade.report_progress(operation_id)
    if not isinstance(progress, OperationProgress):
        raise ProvisioningError("facade progress report is not an OperationProgress")
    if progress.operation_id != operation_id:
        # Progress for a different operation. Accepting it would let one
        # operation's success be read as another's.
        raise ProvisioningError("facade reported progress for a different operation")
    return progress


def summarize(progress: OperationProgress) -> str:
    """A one-line, caller-safe description of an operation's reported state.

    Provided so a consumer logging progress does not hand-roll a string that
    renders ``UNKNOWN`` as a failure. Reveals no parameters and no principal: an
    operation summary is likely to be logged, and a log line is the least
    controlled place a tenant identifier can end up.
    """
    if progress.state == STATE_UNKNOWN:
        return (
            f"operation {progress.operation_id} outcome unresolved "
            f"(not a failure): {progress.detail or 'no detail supplied'}"
        )
    return f"operation {progress.operation_id} is {progress.state}"
