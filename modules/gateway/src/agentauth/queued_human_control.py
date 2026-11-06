"""One-use human steering authority, separate from short-lived admission proofs.

Only the authenticated human command path creates these rows. A worker gets an
opaque reference inside its normal 30-second admission envelope; that envelope
is never renewed or used as delivery authority after expiry. Delivery reads the
original session deadline, rechecks live authority, and consumes the row once.
No bearer token, instruction text, or signing key is retained here.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.envelope import SIGNING_KEY_ID_ENV, _signing_key, sign_envelope, verify_envelope
from src.agentauth.grants import AgentAction
from src.agentauth.human_control import require_canonical_protected_human_owner, require_live_human_membership
from src.agentauth.store import AuthorityStoreError
from src.shared.models.token import Token


def _key(tenant_id, run_id, generation, command_id):
    return {"pk": {"S": f"TENANT#{tenant_id}"}, "sk": {"S": f"HUMAN_STEER#{run_id}#{generation}#{command_id}"}}


def _deadline(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("session expiry must be timezone-aware")
    return result


def issue_queued_steer(store, *, session, run_id, generation, command_id, raw, env, now):
    """Persist only authority established by a currently valid admission proof.

    A deliberate repeat of the original request can recover its original grant;
    neither a later login nor another principal can replace or extend it.
    """
    key = _key(session.tenant_id, run_id, generation, command_id)
    record = store.authority.load_execution(invocation_id=run_id, tenant_id=session.tenant_id)
    if record is None:
        raise BootstrapRefusedError("target execution is unavailable")
    grant = store.live_grant(invocation_id=run_id, tenant_id=session.tenant_id, attempt=record.current_attempt, now=now)
    binding = {
        **key,
        "kind": {"S": "queued_human_steer_v1"},
        "principal": {"S": session.user_id},
        "body_digest": {"S": hashlib.sha256(raw).hexdigest()},
        "session_expires_at": {"S": session.expires_at.astimezone(UTC).isoformat()},
        "session_token_record_id": {"S": session.token_record_id or ""},
        "target_attempt": {"N": str(record.current_attempt)},
        "target_grant_id": {"S": grant.grant_id},
        "target_grant_epoch": {"N": str(grant.revocation_epoch)},
    }
    previous = store._read(key["pk"]["S"], key["sk"]["S"])
    if previous is not None:
        if any(previous.get(k) != v for k, v in binding.items()) or previous.get("consumed") != {"BOOL": False}:
            raise BootstrapRefusedError("queued command authority cannot be replaced")
        reference = previous["reference"]["S"]
    else:
        reference = secrets.token_hex(32)
    ttl = int((session.expires_at - now).total_seconds())
    envelope = sign_envelope(
        tenant_id=session.tenant_id,
        principal=session.user_id,
        authority_kind="human_session",
        target_run_id=run_id,
        target_generation=generation,
        action="steer",
        command_id=command_id,
        request_body=raw,
        queued_grant_ref=reference,
        ttl_seconds=ttl,
        now=now,
        env=env,
    )
    # This path has no expired-proof fallback. Establishing the queued grant
    # requires exactly the same current proof the receiving worker must verify.
    verify_envelope(
        envelope,
        public_keys={env[SIGNING_KEY_ID_ENV]: _signing_key(env).public_key()},
        expected_run_id=run_id,
        expected_generation=generation,
        expected_action="steer",
        expected_command_id=command_id,
        request_body=raw,
        now=now,
    )
    if previous is None:
        item = {**binding, "reference": {"S": reference}, "consumed": {"BOOL": False}, "created_at": {"S": now.astimezone(UTC).isoformat()}}
        try:
            store.client.put_item(TableName=store.table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise BootstrapRefusedError("queued command authority already exists") from None
            raise AuthorityStoreError("queued command authority unavailable") from None
        except BotoCoreError:
            raise AuthorityStoreError("queued command authority unavailable") from None
    return envelope


async def deliver_queued_steer(runtime, body, *, target, generation, raw, now=None):
    """Fresh online decision for one original command, never proof renewal."""
    from starlette.concurrency import run_in_threadpool

    from src.shared.database import get_session_factory

    clock = now or (lambda: datetime.now(UTC))
    key = _key(target.tenant_id, target.invocation_id, generation, body.command_id)
    item = await run_in_threadpool(runtime.store._read, key["pk"]["S"], key["sk"]["S"])
    try:
        if (
            body.action is not AgentAction.STEER
            or body.envelope is not None
            or not item
            or item.get("kind") != {"S": "queued_human_steer_v1"}
            or item.get("reference") != {"S": body.queued_grant_ref}
            or item.get("body_digest") != {"S": hashlib.sha256(raw).hexdigest()}
            or item.get("consumed") != {"BOOL": False}
        ):
            raise ValueError
        expires = _deadline(item["session_expires_at"]["S"])
        if expires - clock() <= timedelta(seconds=1):
            raise ValueError
        principal = item["principal"]["S"]
        grant = await run_in_threadpool(
            runtime.store.live_grant,
            invocation_id=target.invocation_id,
            tenant_id=target.tenant_id,
            attempt=target.attempt,
            now=clock(),
        )
        if (
            item.get("target_attempt") != {"N": str(target.attempt)}
            or item.get("target_grant_id") != {"S": grant.grant_id}
            or item.get("target_grant_epoch") != {"N": str(grant.revocation_epoch)}
        ):
            raise ValueError
        await require_canonical_protected_human_owner(
            runtime.store,
            user_id=principal,
            tenant_id=target.tenant_id,
            run_id=target.invocation_id,
            generation=generation,
            now=clock(),
        )
        async with get_session_factory()() as db:
            await require_live_human_membership(db, user_id=principal, tenant_id=target.tenant_id)
            token_id = item["session_token_record_id"]["S"]
            if token_id:
                # Legacy token revocation remains authoritative. Cognito's
                # existing JWT path has no local global-sign-out evidence;
                # its current membership/target-grant checks are preserved.
                active = await db.scalar(
                    select(Token.id).where(
                        Token.id == token_id,
                        Token.org_id == target.tenant_id,
                        Token.revoked_at.is_(None),
                        Token.expires_at > clock(),
                    )
                )
                if active != token_id:
                    raise ValueError
        runtime.dispatcher.policy.require_supported(body.action)
        # Reserve the worker's entire bounded round trip/handoff window. An
        # answer arriving under one second must not outlive the human session.
        if expires - clock() <= timedelta(seconds=1):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise BootstrapRefusedError("queued command is no longer authorized") from None

    # Consume before answering. An ambiguous/lost response never permits another
    # delivery decision; the worker cannot replay the command after uncertainty.
    try:
        await run_in_threadpool(
            runtime.store.client.update_item,
            TableName=runtime.store.table,
            Key=key,
            UpdateExpression="SET #used = :true",
            ConditionExpression="#ref = :ref AND #used = :false AND session_expires_at = :expiry",
            ExpressionAttributeNames={"#ref": "reference", "#used": "consumed"},
            ExpressionAttributeValues={
                ":true": {"BOOL": True},
                ":false": {"BOOL": False},
                ":ref": item["reference"],
                ":expiry": item["session_expires_at"],
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise BootstrapRefusedError("queued command was already consumed") from None
        raise AuthorityStoreError("queued command delivery outcome unavailable") from None
    except BotoCoreError:
        raise AuthorityStoreError("queued command delivery outcome unavailable") from None
    if expires - clock() <= timedelta(seconds=1):
        raise BootstrapRefusedError("queued command session expired during delivery")
    return {"allowed": True, "command_id": body.command_id, "generation": generation, "max_round_trip_ms": 1000, "delivery_consumed": True}
