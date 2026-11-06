"""Single-use E45 requests bound to existing admitted chat authority.

Admission receipts are not execution evidence. Collected observations require
the full fixed runtime probe matrix and harness oracle before qualifying isolation.
"""

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.agentauth.chat_admission import _encoded
from src.agentauth.chat_capability import ChatAuthorizationRefusedError, ChatAuthorizationUnavailableError, ChatLaunchStore, Identifier
from src.agentauth.chat_execution_mode import execution_mode_update
from src.agentauth.chat_history_store import ChatHistoryStore
from src.agentauth.chat_history_write import ChatHistoryConflictError, ChatHistoryWriter
from src.agentauth.chat_teardown import ChatTeardown

OWNER_PROBES = (
    "session_read",
    "session_write",
    "message_read",
    "message_write",
    "summary_read",
    "summary_write",
    "artifact_read",
    "artifact_write",
    "memory_read",
    "memory_write",
    "cursor_read",
    "object_read",
    "direct_url_read",
)
CROSS_PROBES = OWNER_PROBES + ("forged_principal", "forged_run", "forged_session", "forged_generation", "forged_user_header", "forged_tenant_header")
CREDENTIAL_PROBES = (
    "environment",
    "proc",
    "projected_files",
    "aws_provider",
    "aws_cli",
    "sts_identity",
    "s3_read",
    "dynamodb_read",
    "secrets_read",
    "metadata",
    "kubernetes_api",
    "network_routes",
)
MANIFEST = {
    "version": "e45-v1",
    "probes": {
        actor: list(
            (OWNER_PROBES if actor == "a1" else CROSS_PROBES + ("residue_file", "residue_process")) + ("sandbox_identity",) + CREDENTIAL_PROBES
        )
        for actor in ("a1", "a2", "b1")
    },
    "limits": {"duration_seconds": 120, "probe_timeout_seconds": 5, "result_bytes": 65536, "model_turns": 0},
}


def digest(value):
    return hashlib.sha256(json.dumps(jsonable_encoder(value), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


MANIFEST_DIGEST = digest(MANIFEST)


class QualificationUnavailableError(ChatAuthorizationUnavailableError):
    """Qualification cannot be attempted; this is never isolation evidence."""


class FixtureOwner(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    tenant_id: Identifier
    user_id: Identifier
    team_id: Identifier | Literal[""]


class QualificationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    target: Identifier
    evaluation_id: Identifier
    expires_at: int = Field(gt=0)
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    fixtures: dict[Literal["a1", "a2", "b1"], FixtureOwner]
    secret_canary: str | None = Field(default=None, pattern=r"^[A-Za-z0-9/_+=.@:-]{1,256}$")

    @model_validator(mode="after")
    def bounded_fixtures(self):
        if set(self.fixtures) != {"a1", "a2", "b1"}:
            raise ValueError("three qualification fixtures required")
        first, second, other = (self.fixtures[actor] for actor in ("a1", "a2", "b1"))
        if first.tenant_id != second.tenant_id or first.tenant_id == other.tenant_id or len({entry.user_id for entry in self.fixtures.values()}) != 3:
            raise ValueError("qualification requires distinct users in two tenants")
        return self


def configuration():
    if os.environ.get("ADP_CHAT_QUALIFICATION_ENABLED") != "true":
        raise QualificationUnavailableError("chat_qualification_disabled")
    raw = os.environ.get("ADP_CHAT_QUALIFICATION_CONFIG", "")
    try:
        if not raw or len(raw.encode()) > 8192:
            raise ValueError
        configured = QualificationPolicy.model_validate_json(raw)
    except (ValueError, ValidationError):
        raise QualificationUnavailableError("chat_qualification_configuration_unavailable") from None
    return configured


def admit_window(configured, now):
    if configured.target != os.environ.get("ADP_CHAT_QUALIFICATION_TARGET") or configured.expires_at <= now:
        raise QualificationUnavailableError("chat_qualification_target_unavailable")


def policy(now):
    configured = configuration()
    admit_window(configured, now)
    return configured


class QualificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    evaluation_id: Identifier
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualificationClaim(QualificationRequest):
    session_id: Identifier
    nonce: str = Field(pattern=r"^[a-f0-9]{64}$")


class QualificationLookup(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run_id: Identifier
    session_id: Identifier


class QualificationObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    probe: Identifier
    executed: bool
    outcome: Literal["allowed", "denied", "absent", "present", "distinct", "error", "timeout"]
    exposure_digests: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def redacted_exposure(self):
        if any(len(value) != 64 or any(character not in "0123456789abcdef" for character in value) for value in self.exposure_digests):
            raise ValueError("only SHA-256 exposure digests may be recorded")
        return self


class QualificationResult(QualificationClaim):
    source: Literal["sandbox_runtime"]
    status: Literal["completed", "failed"]
    observations: list[QualificationObservation] = Field(max_length=34)
    failure: Literal["probe_unavailable", "deadline", "execution_error"] | None = None

    @model_validator(mode="after")
    def consistent_failure(self):
        if (self.status == "failed") != (self.failure is not None):
            raise ValueError("only failed executions carry a failure reason")
        return self


class ChatQualification:
    def __init__(self, authority, capabilities):
        self.authority = authority
        self.capabilities = capabilities
        self.table = authority.context_table
        self.history = ChatHistoryStore(self.table, capabilities)
        self.writer = ChatHistoryWriter(authority, self.history)

    def readiness(self, user, evaluation_id, *, now):
        configured = policy(now)
        owner = FixtureOwner(tenant_id=user.org_id, user_id=user.user_id, team_id=user.team_id or "")
        actors = [actor for actor, fixture in configured.fixtures.items() if fixture == owner]
        if user.account_type != "human" or user.is_admin or len(actors) != 1 or evaluation_id != configured.evaluation_id:
            raise QualificationUnavailableError("chat_qualification_fixture_unavailable")
        if not configured.secret_canary:
            raise QualificationUnavailableError("chat_qualification_secret_canary_unavailable")
        if not all(os.environ.get(name) for name in ("MEMORY_TABLE", "ARTIFACTS_TABLE", "ARTIFACTS_BUCKET")):
            raise QualificationUnavailableError("chat_qualification_data_tools_unavailable")
        return {
            "protocol": MANIFEST["version"],
            "manifest_digest": MANIFEST_DIGEST,
            "evaluation_id": evaluation_id,
            "target": configured.target,
            "actor": actors[0],
            "image_digest": configured.image_digest,
            "expires_at": configured.expires_at,
        }

    def for_runtime(self, launch, *, now):
        execution = self.authority.store._read(f"TENANT#{launch.tenant_id}", f"EXEC#{launch.run_id}") or {}
        if execution.get("chat_execution_kind") == {"S": "qualification"}:
            return True
        if os.environ.get("ADP_CHAT_QUALIFICATION_ENABLED") != "true":
            return False
        configured = configuration()
        owner = FixtureOwner(tenant_id=launch.tenant_id, user_id=launch.user_id, team_id=launch.team_id)
        if owner not in configured.fixtures.values():
            return False
        admit_window(configured, now)
        self._scope(launch, configured)
        if launch.session_run_id:
            raise QualificationUnavailableError("chat_qualification_requires_ephemeral_sandbox")
        header = self.history._get(launch.session_id, "header")
        if not header:
            raise QualificationUnavailableError("chat_qualification_session_unavailable")
        checks = self._fences(launch, header, now)
        checks[-1] = execution_mode_update(self.authority, launch, "qualification")
        self._transact(checks)
        return True

    def _scope(self, launch, configured):
        if launch.image_digest != configured.image_digest:
            raise QualificationUnavailableError("chat_qualification_image_unavailable")
        owner = FixtureOwner(tenant_id=launch.tenant_id, user_id=launch.user_id, team_id=launch.team_id)
        actors = [actor for actor, fixture in configured.fixtures.items() if fixture == owner]
        if len(actors) != 1:
            raise QualificationUnavailableError("chat_qualification_fixture_unavailable")
        return actors[0]

    def _request(self, request, now):
        configured = policy(now)
        if request.evaluation_id != configured.evaluation_id or request.manifest_digest != MANIFEST_DIGEST:
            raise ChatAuthorizationRefusedError("chat qualification request refused")
        return configured

    def _key(self, session_id, evaluation_id):
        return {"PK": f"session#{session_id}", "SK": f"qualification#{evaluation_id}"}

    def _read(self, key):
        try:
            return self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        except (BotoCoreError, ClientError):
            raise QualificationUnavailableError("chat_qualification_storage_unavailable") from None

    def _transact(self, changes):
        try:
            self.authority.store.client.transact_write_items(TransactItems=changes)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException":
                raise ChatHistoryConflictError("chat qualification request changed or was consumed") from None
            raise QualificationUnavailableError("chat_qualification_storage_unavailable") from None
        except BotoCoreError:
            raise QualificationUnavailableError("chat_qualification_storage_unavailable") from None

    def _fences(self, launch, header, now):
        store = self.authority.store
        instant = datetime.fromtimestamp(now, UTC)
        grant = store.live_grant(invocation_id=launch.run_id, tenant_id=launch.tenant_id, attempt=launch.attempt, now=instant)
        if grant.grant_id != launch.grant_id or grant.revocation_epoch != launch.grant_epoch:
            raise ChatAuthorizationRefusedError("chat qualification grant changed")
        return [
            self.writer._unchanged(header),
            store._authority_check(grant),
            store._grant_check(grant, instant),
            {"ConditionCheck": ChatTeardown(self.authority, None)._unchanged(ChatLaunchStore.item(launch))},
            {
                "ConditionCheck": {
                    "TableName": store.table,
                    "Key": _encoded({"pk": f"TENANT#{launch.tenant_id}", "sk": f"EXEC#{launch.run_id}"}),
                    "ConditionExpression": (
                        "#status = :active AND workload_binding = :pod AND current_attempt = :attempt "
                        "AND current_credential_epoch = :epoch AND attribute_not_exists(abort_command_id) "
                        "AND attribute_not_exists(chat_turn_sealed) AND chat_execution_kind = :qualification"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _encoded(
                        {
                            ":active": "active",
                            ":pod": launch.sandbox_uid,
                            ":attempt": launch.attempt,
                            ":epoch": launch.credential_epoch,
                            ":qualification": "qualification",
                        }
                    ),
                }
            },
        ]

    def issue(self, user, session_id, request, *, now):
        configured = self._request(request, now)
        if user.account_type != "human" or user.is_admin:
            raise ChatAuthorizationRefusedError("chat qualification requires an ordinary fixture owner")
        launch = self.capabilities.launches.load(request.run_id)
        if (user.org_id, user.team_id or "", user.user_id, session_id) != (launch.tenant_id, launch.team_id, launch.user_id, launch.session_id):
            raise ChatAuthorizationRefusedError("chat qualification owner refused")
        self.capabilities._live(launch, now)
        actor = self._scope(launch, configured)
        header = self.history._get(session_id, "header")
        if not header or (header.get("tenantId"), header.get("teamId"), header.get("ownerUserId")) != (
            launch.tenant_id,
            launch.team_id,
            launch.user_id,
        ):
            raise ChatAuthorizationRefusedError("chat qualification session refused")
        if header.get("historyVersion") != 1 or header.get("draftVersion", 0) != 0:
            raise QualificationUnavailableError("chat_qualification_requires_fresh_session")
        fixtures = None
        if actor != "a1":
            from src.agentauth.chat_qualification_fixtures import removed

            reservation = self._read({"PK": f"chat-qualification#{configured.evaluation_id}", "SK": "a1"}) or {}
            source = self._read(self._key(reservation.get("session_id", ""), configured.evaluation_id)) or {}
            if (
                source.get("state") != "finished"
                or source.get("receipt", {}).get("status") != "completed"
                or not source.get("fixtures")
                or source.get("policy_digest") != digest(configured.model_dump())
                or source.get("fixtures", {}).get("cursor_expires_at", 0) <= now
                or not removed(self.authority, source["binding"])
            ):
                raise QualificationUnavailableError("chat_qualification_owner_fixture_unavailable")
            fixtures = source["fixtures"]
        deadline = min(now + MANIFEST["limits"]["duration_seconds"], launch.expires_at, configured.expires_at)
        request_binding = {
            "nonce": secrets.token_hex(32),
            "manifest_digest": MANIFEST_DIGEST,
            "manifest_version": MANIFEST["version"],
            "evaluation_id": configured.evaluation_id,
            "target": configured.target,
            "actor": actor,
            **{
                field: getattr(launch, field)
                for field in (
                    "tenant_id",
                    "user_id",
                    "team_id",
                    "session_id",
                    "run_id",
                    "sandbox_uid",
                    "lease_generation",
                    "image_digest",
                    "attempt",
                    "credential_epoch",
                )
            },
            "issued_at": now,
            "expires_at": deadline,
            "limits": MANIFEST["limits"],
        }
        record = {
            **self._key(session_id, configured.evaluation_id),
            "state": "issued",
            "binding": request_binding,
            "policy_digest": digest(configured.model_dump()),
            "ttl": configured.expires_at + 86400,
        }
        if fixtures:
            record["fixtures"] = fixtures
        reservation = {
            "PK": f"chat-qualification#{configured.evaluation_id}",
            "SK": actor,
            "session_id": session_id,
            "run_id": launch.run_id,
            "ttl": configured.expires_at + 86400,
        }
        self._transact(
            self._fences(launch, header, now)
            + [
                {"Put": {"TableName": self.table.name, "Item": _encoded(item), "ConditionExpression": "attribute_not_exists(PK)"}}
                for item in (record, reservation)
            ]
        )
        return request_binding

    def _bound_record(self, token, request, configured, now, state="issued"):
        launch, header = self.history._authorize(token, request.run_id, request.session_id, "turn.next", now)
        actor = self._scope(launch, configured)
        record = self._read(self._key(request.session_id, configured.evaluation_id))
        if not record or record.get("policy_digest") != digest(configured.model_dump()):
            raise QualificationUnavailableError("chat_qualification_request_unavailable")
        binding = record["binding"]
        if (
            record.get("state") != state
            or binding["actor"] != actor
            or binding["expires_at"] <= now
            or binding["issued_at"] > now
            or any(
                binding[field] != getattr(launch, field)
                for field in (
                    "tenant_id",
                    "user_id",
                    "team_id",
                    "session_id",
                    "run_id",
                    "sandbox_uid",
                    "lease_generation",
                    "image_digest",
                    "attempt",
                    "credential_epoch",
                )
            )
        ):
            raise ChatAuthorizationRefusedError("chat qualification expired, replayed or mismatched")
        return launch, header, record

    def pending(self, token, request, *, now):
        _, _, record = self._bound_record(token, request, policy(now), now)
        return self._plan(record)

    def _plan(self, record):
        return {"binding": record["binding"], "manifest": MANIFEST, **({"fixtures": record["fixtures"]} if record.get("fixtures") else {})}

    def claim(self, token, request, *, now):
        launch, header, record = self._bound_record(token, request, self._request(request, now), now)
        if record["binding"]["nonce"] != request.nonce:
            raise ChatAuthorizationRefusedError("chat qualification nonce refused")
        update = self.writer._unchanged(record)["ConditionCheck"]
        update["UpdateExpression"] = "SET #state = :claimed, claimed_at = :now"
        update["ExpressionAttributeNames"]["#state"] = "state"
        update["ExpressionAttributeValues"].update(_encoded({":claimed": "claimed", ":now": now}))
        self._transact(self._fences(launch, header, now) + [{"Update": update}])
        return self._plan(record)

    def active(self, token, request, *, now):
        _, _, record = self._bound_record(token, request, self._request(request, now), now, "claimed")
        if record["binding"]["nonce"] != request.nonce:
            raise ChatAuthorizationRefusedError("chat qualification nonce refused")
        return {"nonce": request.nonce, "active": True, "expires_at": record["binding"]["expires_at"]}

    def collect(self, token, request, *, now):
        launch, header, record = self._bound_record(token, request, self._request(request, now), now, "claimed")
        binding = record["binding"]
        if binding["nonce"] != request.nonce:
            raise ChatAuthorizationRefusedError("chat qualification nonce refused")
        probes = [observation.probe for observation in request.observations]
        required = set(MANIFEST["probes"][binding["actor"]])
        if (
            len(set(probes)) != len(probes)
            or not set(probes) <= required
            or (request.status == "completed" and (set(probes) != required or not all(row.executed for row in request.observations)))
        ):
            raise ChatAuthorizationRefusedError("chat qualification execution incomplete or invalid")
        observations = [row.model_dump() for row in request.observations]
        if len(json.dumps(observations).encode()) > MANIFEST["limits"]["result_bytes"]:
            raise ChatAuthorizationRefusedError("chat qualification evidence exceeds bound")
        receipt = {
            "binding": binding,
            "source": "trusted_runtime",
            "status": request.status,
            "failure": request.failure,
            "observations": observations,
            "recorded_at": now,
        }
        if record.get("fixtures"):
            receipt["fixture_digest"] = digest(record["fixtures"])
        receipt["result_digest"] = digest(receipt)
        update = self.writer._unchanged(record)["ConditionCheck"]
        update["UpdateExpression"] = "SET #state = :finished, receipt = :receipt"
        update["ExpressionAttributeNames"]["#state"] = "state"
        update["ExpressionAttributeValues"].update(_encoded({":finished": "finished", ":receipt": receipt}))
        self._transact(self._fences(launch, header, now) + [{"Update": update}])
        return {"nonce": request.nonce, "result_digest": receipt["result_digest"]}

    def result(self, user, session_id, request, *, now):
        if user.account_type != "human" or user.is_admin:
            raise ChatAuthorizationRefusedError("chat qualification requires an ordinary fixture owner")
        record = self._read(self._key(session_id, request.evaluation_id))
        if not record:
            raise QualificationUnavailableError("chat_qualification_request_unavailable")
        binding = record["binding"]
        if (user.org_id, user.team_id or "", user.user_id, request.run_id, request.manifest_digest) != (
            binding["tenant_id"],
            binding["team_id"],
            binding["user_id"],
            binding["run_id"],
            binding["manifest_digest"],
        ):
            raise ChatAuthorizationRefusedError("chat qualification owner refused")
        state = record["state"]
        if state in {"issued", "claimed"} and binding["expires_at"] <= now:
            state = "expired"
        from src.agentauth.chat_qualification_fixtures import removed

        return {
            "state": state,
            "binding": binding,
            "receipt": record.get("receipt"),
            "sandbox_removed": removed(self.authority, binding),
            "cleanup_complete": record.get("cleanup_complete", False),
        }

    def cancel(self, user, session_id, request, *, now):
        if request.manifest_digest != MANIFEST_DIGEST:
            raise ChatAuthorizationRefusedError("chat qualification request refused")
        if user.account_type != "human" or user.is_admin:
            raise ChatAuthorizationRefusedError("chat qualification requires an ordinary fixture owner")
        record = self._read(self._key(session_id, request.evaluation_id))
        if not record:
            raise QualificationUnavailableError("chat_qualification_request_unavailable")
        binding = record["binding"]
        if (user.org_id, user.team_id or "", user.user_id, request.run_id) != (
            binding["tenant_id"],
            binding["team_id"],
            binding["user_id"],
            binding["run_id"],
        ):
            raise ChatAuthorizationRefusedError("chat qualification owner refused")
        if record["state"] in {"cancelled", "finished"}:
            return {"state": record["state"]}
        update = self.writer._unchanged(record)["ConditionCheck"]
        update["UpdateExpression"] = "SET #state = :cancelled, cancelled_at = :now"
        update["ExpressionAttributeNames"]["#state"] = "state"
        update["ExpressionAttributeValues"].update(_encoded({":cancelled": "cancelled", ":now": now}))
        self._transact([{"Update": update}])
        return {"state": "cancelled"}
