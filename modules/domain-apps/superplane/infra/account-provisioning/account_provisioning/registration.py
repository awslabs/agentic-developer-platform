"""Trusted composition of durable account creation, placement and registration.

The executor and credential source are service-owned. Neither history nor an
account ID is accepted from the caller. The generated identity is consumed by
the maintained Account Factory renderer; #5534/#5535 own the runtime dispatch.
"""

from account_factory.creation import CreateAccountStatus, account_identity_key
from account_factory.registration import CreatedAccountRegistration
from account_factory.render import render

from .creation_runner import (
    _OPERATION_KIND,
    _PROVIDER,
    CreationRefused,
    _plan_generation,
    _require_complete_authorization,
    _require_new_account_mode,
    _require_unchanged_payload,
    _settled_outcome,
    create_account,
    creation_key,
)
from .placement import read_placement


async def creation_history(executor, request):
    history = tuple(await executor.provider_calls(provider=_PROVIDER, operation_kind=_OPERATION_KIND))
    for index, row in enumerate(history):
        if (row.operation_id, row.org_id, row.workspace_id, row.idempotency_key) != (
            executor.operation_id,
            executor.org_id,
            executor.workspace_id,
            creation_key(executor, index),
        ):
            raise CreationRefused("durable creation history has a gap or mismatched operation identity")
        _require_unchanged_payload(request, row)
    if history:
        # Every predecessor must be a settled retryable failure, even when the
        # last row is successful and is being consumed for registration.
        plan = _plan_generation(request, history[:-1])
        if plan.generation != len(history) - 1:
            raise CreationRefused("durable creation generation is inconsistent")
    return history


async def create_from_durable_history(executor, credentials, request, *, authorization):
    _require_complete_authorization(executor, request, authorization)
    history = await creation_history(executor, request)
    return await create_account(executor, credentials, request, authorization=authorization, history=history)


async def load_created_account(executor, credentials, request, *, authorization):
    _require_new_account_mode(request)
    _require_complete_authorization(executor, request, authorization)
    history = await creation_history(executor, request)
    successes = []
    for row in history:
        _require_unchanged_payload(request, row)
        outcome = _settled_outcome(row)
        if outcome.status is CreateAccountStatus.SUCCEEDED:
            successes.append(outcome)
        elif outcome.status is not CreateAccountStatus.FAILED:
            raise CreationRefused("creation registration has unresolved durable history")
    if len(successes) != 1 or not successes[0].account_id:
        raise CreationRefused("creation registration requires exactly one durable successful account")
    account_id = successes[0].account_id
    management = await credentials.management(operation_id=executor.operation_id)
    placement = read_placement(management.organizations, account_id=account_id, organizational_unit_id=request.organizational_unit_id)
    if not placement.verified:
        raise CreationRefused("creation registration requires verified approved OU placement")
    # Renew the authority check after provider I/O; cancellation or lease expiry
    # during the placement read must not authorize publication.
    if await creation_history(executor, request) != history:
        raise CreationRefused("durable creation history changed during registration")
    return CreatedAccountRegistration(
        operation_id=executor.operation_id,
        organization_id=executor.org_id,
        workspace_id=executor.workspace_id,
        account_id=account_id,
        approved_identity=account_identity_key(request),
    )


async def render_created_account(executor, credentials, request, *, authorization):
    record = await load_created_account(executor, credentials, request, authorization=authorization)
    return render(request, authorization, creation_record=record)
