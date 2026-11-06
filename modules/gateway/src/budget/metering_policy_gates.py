"""Order budget and person-limit mutations against durable admissions."""

import rfc8785
from sqlalchemy import select, text

from src.orchestration.provider_quotes import request_digest
from src.shared.models.metering import MeteringScopeGate


async def lock_admission_policy(
    session,
    *,
    tenant_id: str,
    person_anchors: set[str] | None = None,
    org_ids: list[str] | None = None,
    team_keys: list[tuple[str, str]] | None = None,
) -> None:
    if session.bind.dialect.name != "postgresql":
        return
    await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    keys = {f"tenant:{tenant_id}"}
    if person_anchors:
        keys.update(f"person:{anchor}" for anchor in person_anchors)
        keys.add("default:platform")
        keys.update(f"default:org:{org_id}" for org_id in org_ids or ())
        keys.update(f"default:team:{org_id}:{team_id}" for org_id, team_id in team_keys or ())
    for key in sorted(keys):
        await session.execute(text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"), {"key": f"metering:policy:{key}"})


async def lock_policy_mutation(
    session,
    *,
    tenant_id: str | None = None,
    scope_kind: str | None = None,
    identity: str | None = None,
    person_anchor: str | None = None,
    default_scope: str | None = None,
) -> None:
    if session.bind.dialect.name != "postgresql":
        return
    domains = [tenant_id is not None, person_anchor is not None, default_scope is not None]
    if sum(domains) != 1:
        raise ValueError("metering policy mutation requires exactly one scoped authority")
    if tenant_id is not None:
        key = f"tenant:{tenant_id}"
    elif person_anchor is not None:
        key = f"person:{person_anchor}"
    else:
        key = f"default:{default_scope}"
    await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"metering:policy:{key}"})
    if tenant_id is None or scope_kind is None or identity is None:
        return
    if await session.scalar(text("SELECT to_regclass('metering_scope_gates')")) is None:
        return
    scope_key = f"v1:tenant:{request_digest(rfc8785.dumps([tenant_id, scope_kind, identity]))}"
    await session.scalar(select(MeteringScopeGate).where(MeteringScopeGate.scope_key == scope_key).with_for_update())


async def lock_person_binding(session, root_user_id: str) -> tuple[str | None, list[str]]:
    from src.budget.person_ledger import resolve_person_identity
    from src.shared.models.organization import User
    from src.shared.models.vault import UserIdentity

    async def binding():
        user = await session.scalar(select(User).where(User.id == root_user_id).execution_options(populate_existing=True))
        identities = (
            await session.execute(select(UserIdentity.provider, UserIdentity.provider_user_id).where(UserIdentity.user_id == root_user_id))
        ).all()
        anchor, user_ids = await resolve_person_identity(session, root_user_id) if user else (None, [root_user_id])
        keys = {f"anchor:{provider}:{provider_user_id}" for provider, provider_user_id in identities}
        if user and user.cognito_sub:
            keys.add(f"anchor:cognito:{user.cognito_sub}")
        return anchor, user_ids, keys

    if session.bind.dialect.name != "postgresql":
        anchor, user_ids, _ = await binding()
        return anchor, user_ids
    await session.execute(text("SET LOCAL lock_timeout = '500ms'"))
    anchor, user_ids, keys = await binding()
    for key in sorted(keys) + [f"user:{user_id}" for user_id in sorted(set(user_ids) | {root_user_id})]:
        await session.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
            {"key": f"metering:binding:{key}"},
        )
    if (anchor, user_ids, keys) != await binding():
        raise ValueError("person binding changed while acquiring mutation fence")
    return anchor, user_ids
