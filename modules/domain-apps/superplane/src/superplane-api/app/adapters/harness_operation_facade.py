"""The production ``operation_facade`` adapter over ``harness_jobs``.

Issue #5535 (Superplane W6), EPIC #4910.

`app/services/provisioning.py` declares the port as a `Protocol` with two methods
and `superplane_contracts.integration` declares its refusals as
`ProvisioningUnavailable` and `ProvisioningRefused` — the *domain's* exception
names. `harness_jobs` raises its own types and publishes the mapping as data in
`PORT_REFUSAL_NAMES`, with the explicit note that "the composer's thin adapter
re-raises as the declared ones". This module is that thin adapter.

## Why the translation is not optional

`superplane_contracts.conformance.classify_response` matches a raised exception
against the port's `refusal_exceptions` **as an allowlist over the type's MRO**.
An `OperationRefused` reaching the boundary untranslated is not in that list, so
the capability probe would classify a *correct refusal* as `FAILED` with "raised
OperationRefused, which is not among this port's declared refusals" — and the
image would fail its own gate while behaving correctly. The translation is what
makes the port's contract true, not a cosmetic nicety.

## Why it is `wraps` and not inheritance

The harness's `OperationFacadeService` is a `@dataclass` this module must not
subclass: the port's surface is exactly two methods, and a subclass would expose
`cancel_operation`, `report_execution` and `list_operations` behind a name the
consumer's Protocol says has two. Structural typing means a narrow wrapper
satisfies the port precisely, and cannot drift into offering authority the port
never declared.

## What is deliberately not here

No fallback. There is no "facade unavailable, so provision directly" branch,
because that substitution is the one R14 forbids — a broader authority that is
*available* where the correct one is *not reachable*. When the store cannot be
reached the adapter raises `ProvisioningUnavailable` and the route answers 503.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.operation_activation import require_admission_enabled
from app.services.provisioning import (
    OperationProgress,
    ProvisioningError,
    ProvisioningRefused,
    ProvisioningUnavailable,
)

logger = logging.getLogger(__name__)


class HarnessOperationFacade:
    """Adapts `harness_jobs.OperationFacadeService` to the domain's port.

    Holds the service rather than constructing it, so composition decides what the
    service is composed with — the approval source and ledger in particular, which
    are the domain's and which the service refuses to be built without.
    """

    def __init__(
        self,
        service: Any,
        *,
        enabled: bool = True,
        lifecycle_verify=None,
        activation_verify=None,
    ) -> None:
        self._service = service
        self._lifecycle_verify = lifecycle_verify
        self._activation_verify = activation_verify
        if type(enabled) is not bool:
            raise ValueError("operation admission enabled must be a boolean")
        self._enabled = enabled

    async def open_operation(
        self,
        *,
        action: str,
        workspace_id: str,
        org_id: str,
        permission: str,
        parameters: dict[str, str],
    ) -> OperationProgress:
        """Authorize and start one operation, returning the facade's first report."""
        lifecycle = (
            "runtime_config_sha256" in parameters or "lifecycle_phase" in parameters
        )
        require_admission_enabled(enabled=self._enabled, lifecycle=lifecycle)
        if lifecycle:
            await self._require_lifecycle_binding(org_id)
            require_admission_enabled(enabled=self._enabled, lifecycle=True)
        progress = await self._call(
            self._service.open_operation(
                action=action,
                workspace_id=workspace_id,
                org_id=org_id,
                permission=permission,
                parameters=parameters,
            )
        )
        return _progress(progress)

    async def _require_lifecycle_binding(self, org_id: str) -> None:
        from app.operation_activation import expected_lifecycle_binding

        require_admission_enabled(enabled=self._enabled, lifecycle=True)
        try:
            dependencies_ready = (
                self._activation_verify is not None and await self._activation_verify()
            )
        except Exception:
            dependencies_ready = False
        if not dependencies_ready:
            raise ProvisioningUnavailable("paid worker dependencies are unavailable")
        if self._lifecycle_verify is None or not await self._lifecycle_verify(
            org_id, expected_lifecycle_binding()
        ):
            raise ProvisioningUnavailable(
                "paid worker binding verification is unavailable"
            )

    async def report_progress(self, operation_id: str) -> OperationProgress:
        """The facade's current report. The only way an outcome is learned."""
        progress = await self._call(self._service.report_progress(operation_id))
        return _progress(progress)

    async def _call(self, awaitable: Any) -> Any:
        """Await a harness call, re-raising its refusals under the declared names.

        The mapping is taken from `PORT_REFUSAL_NAMES` at runtime rather than
        hardcoded, so a refusal type added to the harness's published mapping is
        translated without an edit here — and one that is *not* in the mapping
        falls through to the unavailable branch rather than escaping as an
        undeclared exception.
        """
        try:
            return await awaitable
        except asyncio.CancelledError:
            # Never translated: this task is being torn down, which is not an
            # answer about an operation.
            raise
        except BaseException as error:
            raise _translate(error) from error


def _translate(error: BaseException) -> BaseException:
    """Map a harness exception onto the port's two declared refusals."""
    from harness_jobs.facade import PORT_REFUSAL_NAMES

    names = [cls.__name__ for cls in type(error).__mro__]
    for name in names:
        declared = PORT_REFUSAL_NAMES.get(name)
        if declared == "ProvisioningRefused":
            return ProvisioningRefused(str(error))
        if declared == "ProvisioningUnavailable":
            return ProvisioningUnavailable(str(error))

    if isinstance(error, ProvisioningError):
        # Already the port's own vocabulary — a `ContractViolation` translated
        # below, or a progress-shape error raised by this module.
        return error

    if isinstance(error, Exception):
        # Anything else, including `ContractViolation`, is reported as unavailable
        # rather than as a refusal. A refusal asserts the caller was not entitled;
        # an unexpected exception asserts nothing about the caller, and claiming it
        # did would blame a tenant for a defect. The message is dropped because an
        # unanticipated exception's text is not known to be caller-safe.
        logger.warning(
            "the operation facade failed with %s", type(error).__name__, exc_info=False
        )
        return ProvisioningUnavailable(
            "the operation facade could not establish an outcome"
        )

    return error


def _progress(progress: Any) -> OperationProgress:
    """Convert the harness's progress report into the consumer's own type.

    A conversion and not a pass-through: `app/services/provisioning.py` checks
    `isinstance(progress, OperationProgress)` against **its own** class and raises
    `ProvisioningError` otherwise, so returning the harness's identically-shaped
    dataclass would be rejected as a contract breach. The two types are field-
    compatible, which is what makes this a copy rather than a translation.
    """
    operation_id = getattr(progress, "operation_id", None)
    state = getattr(progress, "state", None)
    if not isinstance(operation_id, str) or not isinstance(state, str):
        raise ProvisioningError("the operation facade returned an unusable report")
    detail = getattr(progress, "detail", None)
    return OperationProgress(
        operation_id=operation_id,
        state=state,
        detail=detail if isinstance(detail, str) else None,
    )
