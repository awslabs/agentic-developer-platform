"""One-shot native delivery. SQL contains bindings and outcomes, never credentials."""

import asyncio
import hashlib
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.shared.models.native_credentials import NativeCredentialRequest as Record

from .aws_connection_authority import connection_material, verified_connection_evidence
from .native_credential_authority import canonical_human, credential_fingerprint, current_authority, refused, require
from .native_credential_wire import AUDIENCE, canonical, digest, encode, encrypt_payload, validate_offer, verify_proof

# Effects use their own bounded pool: their boundary rechecks await the event
# loop, whose authority-provider reads use the ordinary executor. No pool cycle.
_EFFECTS = ThreadPoolExecutor(max_workers=8, thread_name_prefix="native-private")
_UNSTARTED = {"created", "awaiting_approval", "approved", "challenged"}


def outcome(value, row, *, code="native_ack_invalid", status=400):
    """Revalidate both incoming and persisted acknowledgments before projection."""
    required = {"type", "execution_id", "request_id", "receipt_id", "delivery_state", "code", "cleanup"}
    require(
        isinstance(value, dict)
        and required <= value.keys() <= required | {"exit_code"}
        and all(type(value[key]) is str for key in required)
        and value["type"] == "result"
        and value["delivery_state"] in {"completed", "unknown", "failed", "cancelled", "expired"}
        and value["code"]
        in {
            "child_exited",
            "child_start_failed",
            "host_cancelled",
            "host_failed",
            "host_disconnected",
            "delivery_rejected",
            "local_lifetime_ended",
            "delivery_acknowledgement_lost",
        }
        and value["cleanup"] in {"complete", "unknown"}
        and ("exit_code" not in value or type(value["exit_code"]) is int and -255 <= value["exit_code"] <= 255)
        and all(
            value[key] == expected for key, expected in dict(execution_id=row.execution_id, request_id=row.request_id, receipt_id=row.id).items()
        ),
        code,
        status,
    )
    return {key: value[key] for key in required | {"exit_code"} if key in value}


def metadata(row):
    result = dict(
        execution_id=row.execution_id,
        request_id=row.request_id,
        receipt_id=row.id,
        status="expired" if row.status in _UNSTARTED and time.time() >= row.expires_at else row.status,
        selection=row.selection,
        snapshot=row.snapshot or "",
        expires_at=row.expires_at,
    )
    if row.acknowledgment is not None:
        result["acknowledgment"] = outcome(row.acknowledgment, row, code="native_acknowledgment_unavailable", status=503)
        result["acknowledgment_source"] = "client_reported"
    return result


class NativeCredentialService:
    def __init__(self, factory, config, providers):
        self.factory, self.config, self.providers = factory, config, providers

    async def owned(self, db, actor, receipt_id=None, request_id=None, *, lock=False, exact=False):
        user = await canonical_human(db, actor)
        query = select(Record).where(Record.org_id == user.org_id, Record.user_id == user.id)
        query = query.where(Record.id == receipt_id) if receipt_id is not None else query.where(Record.request_id == request_id)
        row = await db.scalar((query.with_for_update() if lock else query).execution_options(populate_existing=True))
        require(row is not None, "native_request_not_found", 404)
        if exact:
            require(actor.session_id == row.session_id, "native_original_session_required")
        return row

    async def authority(self, db, actor, row):
        selection = row.selection
        user, credential, version = await current_authority(
            db,
            actor=actor,
            providers=self.providers,
            **{key: selection[key] for key in ("credential_id", "operation", "service", "label")},
        )
        require(
            user.id == row.user_id
            and user.org_id == row.org_id
            and user.team_id == row.team_id
            and version == selection["policy_version"]
            and credential_fingerprint(credential) == row.credential_digest
        )
        require(time.time() < row.expires_at, "native_request_expired", 409)
        return credential

    async def create(self, actor, value):
        request_digest = digest(value.model_dump())
        async with self.factory() as db, db.begin():
            user, credential, version = await current_authority(
                db,
                actor=actor,
                providers=self.providers,
                **{key: getattr(value, key) for key in ("credential_id", "operation", "service", "label")},
            )
            row = await db.scalar(
                select(Record).where(Record.org_id == user.org_id, Record.user_id == user.id, Record.request_id == value.request_id)
            )
            if row is not None:
                require(row.request_digest == request_digest, "native_request_conflict", 409)
                return metadata(row)
            receipt_id, execution_id = str(uuid4()), str(uuid4())
            selection = dict(
                issuer=self.config.issuer,
                audience=AUDIENCE,
                key_id=self.config.key_id,
                tenant_id=user.org_id,
                user_id=user.id,
                session_id=actor.session_id,
                execution_id=execution_id,
                request_id=value.request_id,
                receipt_id=receipt_id,
                policy_version=version,
                argument_digest=digest(value.arguments()),
                **{key: getattr(value, key) for key in ("credential_id", "operation", "service", "label", "argv", "cwd", "use_seconds")},
            )
            row = Record(
                id=receipt_id,
                org_id=user.org_id,
                user_id=user.id,
                team_id=user.team_id,
                request_id=value.request_id,
                execution_id=execution_id,
                session_id=actor.session_id,
                request_digest=request_digest,
                credential_digest=credential_fingerprint(credential),
                selection=selection,
                status="created",
                expires_at=min(actor.expires_at, int(time.time()) + 600),
            )
            try:
                async with db.begin_nested():
                    db.add(row)
                    await db.flush()
            except IntegrityError:
                row = await db.scalar(
                    select(Record).where(Record.org_id == user.org_id, Record.user_id == user.id, Record.request_id == value.request_id)
                )
                require(row is not None and row.request_digest == request_digest, "native_request_conflict", 409)
            return metadata(row)

    async def status(self, actor, **identifiers):
        async with self.factory() as db:
            return metadata(await self.owned(db, actor, **identifiers))

    async def offer(self, actor, receipt_id, offer):
        async with self.factory() as db, db.begin():
            row = await self.owned(db, actor, receipt_id, lock=True, exact=True)
            await self.authority(db, actor, row)
            snapshot = validate_offer(offer, row.selection)
            if row.offer is not None:
                require(row.offer == offer and row.snapshot == snapshot, "native_offer_conflict", 409)
                return metadata(row)
            require(row.status == "created", "native_request_consumed", 409)
            row.offer, row.snapshot, row.status = offer, snapshot, "awaiting_approval"
            row.expires_at = min(row.expires_at, int(time.time()) + 90)
            return metadata(row)

    async def preview(self, actor, receipt_id):
        async with self.factory() as db, db.begin():
            row = await self.owned(db, actor, receipt_id, lock=True)
            result = metadata(row)
            require(row.offer is not None, "native_offer_required", 409)
            nonce = ""
            if result["status"] == "awaiting_approval":
                await self.authority(db, actor, row)
                nonce = encode(secrets.token_bytes(32))
                row.browser_session_id, row.approval_nonce_hash = actor.session_id, hashlib.sha256(nonce.encode()).hexdigest()
            return {**result, "offer": row.offer, "approval_nonce": nonce}

    async def approve(self, actor, receipt_id, value, origin):
        require(origin == self.config.approval_origin, "native_browser_origin_required")
        require(
            isinstance(value, dict)
            and set(value) == {"action", "snapshot", "approval_nonce"}
            and value["action"] in {"approve", "deny"}
            and type(value["approval_nonce"]) is str,
            "native_approval_invalid",
            400,
        )
        async with self.factory() as db, db.begin():
            row = await self.owned(db, actor, receipt_id, lock=True)
            await self.authority(db, actor, row)
            require(
                row.status == "awaiting_approval"
                and row.browser_session_id == actor.session_id
                and row.snapshot == value["snapshot"]
                and row.approval_nonce_hash is not None
                and secrets.compare_digest(row.approval_nonce_hash, hashlib.sha256(value["approval_nonce"].encode()).hexdigest()),
                "native_approval_changed",
                409,
            )
            row.approval_nonce_hash = None
            row.status = "approved" if value["action"] == "approve" else "denied"
            row.approved_snapshot = row.snapshot if row.status == "approved" else None
            return dict(receipt_id=row.id, status=row.status)

    async def challenge(self, actor, receipt_id):
        async with self.factory() as db, db.begin():
            row = await self.owned(db, actor, receipt_id, lock=True, exact=True)
            await self.authority(db, actor, row)
            require(row.status in {"approved", "challenged"} and row.approved_snapshot == row.snapshot, "native_approval_required", 409)
            if row.status == "approved":
                row.challenge = encode(secrets.token_bytes(32))
                row.challenge_expires_at = min(row.expires_at, int(time.time()) + 30)
                row.status = "challenged"
            require(time.time() < row.challenge_expires_at, "native_challenge_expired", 409)
            return dict(receipt_id=row.id, challenge=row.challenge, expires_at=row.challenge_expires_at)

    async def recheck(self, actor, receipt_id):
        # Fresh sessions for every check prevent ORM/transaction snapshot reuse.
        await asyncio.to_thread(self.providers.current_session, actor)
        async with self.factory() as db:
            row = await self.owned(db, actor, receipt_id, exact=True)
            require(
                row.status == "unknown" and row.approved_snapshot == row.snapshot and row.offer is not None and digest(row.offer) == row.snapshot,
                "native_request_consumed",
                409,
            )
            credential = await self.authority(db, actor, row)
            return row, credential

    async def effect(self, actor, receipt_id, operation):
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()

        def invoke():
            # A queued worker must recheck after scheduling, immediately before
            # the SDK boundary. A cancellation/timeout never schedules a retry.
            future = asyncio.run_coroutine_threadsafe(self.recheck(actor, receipt_id), loop)
            try:
                row, credential = future.result(timeout=45)
            except BaseException:
                future.cancel()
                raise
            actor.fresh()
            require(not cancelled.is_set(), "native_request_cancelled", 409)
            return operation(row, credential)

        try:
            return await loop.run_in_executor(_EFFECTS, invoke)
        except BaseException:
            cancelled.set()
            raise

    async def issue(self, actor, receipt_id, proof):
        async with self.factory() as db, db.begin():
            row = await self.owned(db, actor, receipt_id, lock=True, exact=True)
            await self.authority(db, actor, row)
            require(
                row.status == "challenged" and row.approved_snapshot == row.snapshot and time.time() < row.challenge_expires_at,
                "native_request_consumed",
                409,
            )
            verify_proof(proof, row.offer, row.challenge)
            # Commit the permanent no-reissue fence before any effect. There is
            # intentionally no secret/envelope recovery endpoint or retry path.
            row.status = "unknown"
            offer, challenge = row.offer, row.challenge
        try:
            key_pem = await self.effect(actor, receipt_id, lambda r, c: self.providers.public_key(self.config))
            version = await self.effect(actor, receipt_id, lambda r, c: self.providers.secret_version(c.secret_arn))

            def read_secret(r, c):
                if r.selection["operation"] == "assume_role":
                    require(verified_connection_evidence(c)[1] == version, "native_credential_version_changed")
                return self.providers.secret(c.secret_arn, version)

            raw = await self.effect(actor, receipt_id, read_secret)
            require(type(raw) is bytes and 0 < len(raw) <= 65536, "native_credential_payload_unavailable")

            def unchanged_version(r, c):
                require(self.providers.secret_version(c.secret_arn) == version, "native_credential_version_changed")

            await self.effect(actor, receipt_id, unchanged_version)
            if offer["selection"]["operation"] == "assume_role":
                material = json.loads(raw)
                del raw

                def assume(r, c):
                    require(verified_connection_evidence(c)[1] == version, "native_credential_version_changed")
                    role, external_id, _ = connection_material(material, c.scopes or {}, c)
                    started = time.time()
                    credentials = self.providers.assume(role_arn=role, external_id=external_id, user_id=r.user_id, receipt_id=r.id)
                    finished = time.time()
                    expiry = credentials.get("Expiration")
                    require(isinstance(expiry, datetime) and expiry.tzinfo is not None, "native_provider_lifetime_invalid")
                    epoch = expiry.astimezone(UTC).timestamp()
                    require(epoch.is_integer(), "native_provider_lifetime_invalid")
                    issued = int(epoch) - 900
                    require(int(started) <= issued <= int(finished), "native_provider_lifetime_invalid")
                    payload = {
                        target: credentials.get(source)
                        for target, source in (
                            ("access_key_id", "AccessKeyId"),
                            ("secret_access_key", "SecretAccessKey"),
                            ("session_token", "SessionToken"),
                        )
                    }
                    require(all(type(v) is str and 0 < len(v) <= 16384 for v in payload.values()), "native_credential_payload_unavailable")
                    return payload, issued, int(epoch)

                payload, issued_at, credential_expires_at = await self.effect(actor, receipt_id, assume)
                del material
            else:
                current, credential = await self.recheck(actor, receipt_id)
                issued_at = int(time.time())
                credential_expires_at = issued_at + current.selection["use_seconds"]
                if credential.expires_at is not None:
                    credential_expires_at = min(
                        credential_expires_at, int(credential.expires_at.replace(tzinfo=credential.expires_at.tzinfo or UTC).timestamp())
                    )
                payload = {"file_base64": encode(raw)}
                del raw
            body = encrypt_payload(
                offer=offer,
                challenge=challenge,
                issued_at=issued_at,
                credential_expires_at=credential_expires_at,
                payload=payload,
                nonce=secrets.token_bytes(12),
            )
            del payload
            signature = await self.effect(actor, receipt_id, lambda r, c: self.providers.sign(self.config, canonical(body)))
            serialization.load_pem_public_key(key_pem.encode()).verify(
                signature, canonical(body), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256()
            )
            await self.effect(actor, receipt_id, unchanged_version)
            await self.recheck(actor, receipt_id)
            envelope = dict(body=body, signature=encode(signature))
            require(issued_at <= time.time() < min(body["delivery_expires_at"], credential_expires_at), "native_delivery_expired")
            async with self.factory() as db, db.begin():
                row = await self.owned(db, actor, receipt_id, lock=True, exact=True)
                await self.authority(db, actor, row)
                require(row.status == "unknown", "native_request_consumed", 409)
                row.status, row.issued_at, row.credential_expires_at = "delivered", issued_at, credential_expires_at
                row.envelope_digest = digest(envelope)
            actor.fresh()
            return envelope
        except Exception:
            # Provider/crypto exceptions can contain credentials. Never log or
            # return their messages. The committed original receipt stays fenced.
            raise refused("native_private_delivery_unknown", 503) from None

    async def acknowledge(self, actor, receipt_id, value):
        async with self.factory() as db, db.begin():
            row = await self.owned(db, actor, receipt_id, lock=True, exact=True)
            value = outcome(value, row)
            require(row.status in {"unknown", "delivered", "acknowledged"}, "native_request_consumed", 409)
            require(row.acknowledgment is None or row.acknowledgment == value, "native_ack_conflict", 409)
            row.acknowledgment = value
            if row.status == "delivered":
                row.status = "acknowledged"
            return dict(receipt_id=row.id, status=row.status)
