"""Chat data capabilities; storage references are never ownership authority."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from typing import Annotated, Literal

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.run_credential import CredentialError, _key
from src.agentauth.store import AuthorityStoreError
from src.agentauth.workload import VerifiedPod

AUDIENCE = "adp-chat-data"
VERSION = "chat-data-v1"
MAX_TTL_SECONDS = 300
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Operation = Literal[
    "history.read",
    "history.expand",
    "history.append",
    "memory.search",
    "memory.write",
    "artifact.create",
    "artifact.read",
    "draft.read",
    "draft.write",
    "session.share",
]


class ChatAuthorizationRefusedError(Exception):
    """The current caller has no authority for this operation."""


class ChatCapabilityInvalidError(ChatAuthorizationRefusedError):
    """The presented capability is missing, malformed or not signed by this gateway (authentication, 401)."""


class ChatCapabilityExpiredError(ChatAuthorizationRefusedError):
    """A genuine capability whose validity window has passed; the sandbox must refresh (401)."""


class ChatAuthorizationUnavailableError(Exception):
    """Current authority could not be established; never use cached permission."""


class ChatLaunch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: Identifier
    tenant_id: Identifier
    user_id: Identifier
    # Canonical personal-chat identities use an empty team when the tenant
    # member has no team. Resource access still checks its actual team below.
    team_id: Identifier | Literal[""]
    session_id: Identifier
    sandbox_uid: Identifier
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    attempt: int = Field(strict=True, ge=1)
    credential_epoch: int = Field(strict=True, ge=1)
    lease_generation: int = Field(strict=True, ge=1)
    grant_id: Identifier
    grant_epoch: int = Field(strict=True, ge=1)
    operations: frozenset[Operation] = Field(min_length=1)
    expires_at: int = Field(strict=True, ge=1)


def _launch_json(launch: ChatLaunch) -> str:
    document = launch.model_dump(mode="json")
    document["operations"] = sorted(launch.operations)
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


class ChatLaunchStore:
    """Immutable records written only by trusted launch admission, never tools."""

    def __init__(self, store: BootstrapStore):
        self.store = store

    @staticmethod
    def item(launch: ChatLaunch) -> dict:
        return {
            "pk": {"S": f"CHAT-LAUNCH#{launch.run_id}"},
            "sk": {"S": "LAUNCH"},
            "document": {"S": _launch_json(launch)},
        }

    def register(self, launch: ChatLaunch) -> None:
        item = self.item(launch)
        try:
            self.store.client.put_item(TableName=self.store.table, Item=item, ConditionExpression="attribute_not_exists(pk)")
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise ChatAuthorizationUnavailableError("chat launch unavailable") from None
            if self.load(launch.run_id) != launch:
                raise ChatAuthorizationRefusedError("chat launch already bound") from None
        except BotoCoreError:
            raise ChatAuthorizationUnavailableError("chat launch unavailable") from None

    def load(self, run_id: str) -> ChatLaunch:
        try:
            row = self.store._read(f"CHAT-LAUNCH#{run_id}", "LAUNCH")
            if row is None:
                raise ChatAuthorizationRefusedError("chat launch unavailable")
            launch = ChatLaunch.model_validate_json(row["document"]["S"])
            if launch.run_id != run_id:
                raise ChatAuthorizationRefusedError("chat launch mismatch")
            return launch
        except AuthorityStoreError:
            raise ChatAuthorizationUnavailableError("chat launch unavailable") from None
        except (KeyError, TypeError, ValidationError):
            raise ChatAuthorizationRefusedError("chat launch invalid") from None


class _Claims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audience: Literal["adp-chat-data"]
    run_id: Identifier
    launch_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    issued_at: int = Field(strict=True, ge=1)
    expires_at: int = Field(strict=True, ge=1)


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


class ChatCapabilityService:
    """Requires fresh root/lease and directory checks for issuance and every use.

    ``current`` must verify the live root grant, execution, session and lease
    against this launch, including revocation epochs and the approved image.
    ``member`` must query current tenant/team membership, not token assertions;
    an empty team means tenant membership only, never membership in every team.
    There are deliberately no permissive defaults for either authority source.
    """

    def __init__(
        self,
        launches: ChatLaunchStore,
        *,
        current: Callable[[ChatLaunch, int], bool],
        member: Callable[[str, str, str], bool],
        env: dict[str, str] | None = None,
    ):
        self.launches = launches
        self.current = current
        self.member = member
        self.env = env

    def _sign(self, body: str) -> str:
        try:
            return _encode(hmac.new(_key(self.env), f"{VERSION}.{body}".encode("ascii"), hashlib.sha256).digest())
        except CredentialError:
            raise ChatAuthorizationUnavailableError("chat signing unavailable") from None

    def _member(self, tenant: str, user: str, team: str) -> None:
        try:
            allowed = self.member(tenant, user, team)
        except Exception:
            raise ChatAuthorizationUnavailableError("chat membership unavailable") from None
        if allowed is not True:
            raise ChatAuthorizationRefusedError("chat membership refused")

    def _live(self, launch: ChatLaunch, now: int) -> None:
        if now >= launch.expires_at:
            raise ChatAuthorizationRefusedError("chat launch expired")
        try:
            allowed = self.current(launch, now)
        except Exception:
            raise ChatAuthorizationUnavailableError("chat authority unavailable") from None
        if allowed is not True:
            raise ChatAuthorizationRefusedError("chat authority refused")
        self._member(launch.tenant_id, launch.user_id, launch.team_id)

    def issue(self, run_id: str, pod: VerifiedPod, *, now: int) -> str:
        launch = self.launches.load(run_id)
        if pod.uid != launch.sandbox_uid:
            raise ChatAuthorizationRefusedError("chat workload mismatch")
        self._live(launch, now)
        claims = _Claims(
            audience=AUDIENCE,
            run_id=launch.run_id,
            launch_digest=hashlib.sha256(_launch_json(launch).encode()).hexdigest(),
            issued_at=now,
            expires_at=min(now + MAX_TTL_SECONDS, launch.expires_at),
        )
        body = _encode(claims.model_dump_json().encode())
        return f"{VERSION}.{body}.{self._sign(body)}"

    def verify(self, token: str, *, run_id: str, session_id: str, operation: Operation, now: int) -> ChatLaunch:
        launch = self.verify_run(token, run_id=run_id, operation=operation, now=now)
        if launch.session_id != session_id:
            raise ChatAuthorizationRefusedError("chat capability scope refused")
        return launch

    def verify_run(self, token: str, *, run_id: str, operation: Operation, now: int) -> ChatLaunch:
        """Verify the caller's execution; resource access requires a separate check."""
        if not isinstance(token, str) or not 1 <= len(token) <= 4096:
            raise ChatCapabilityInvalidError("chat capability invalid")
        try:
            version, body, signature = token.split(".")
            if version != VERSION or not hmac.compare_digest(self._sign(body), signature):
                raise ChatCapabilityInvalidError("chat capability invalid")
            claims = _Claims.model_validate_json(_decode(body))
        except (ValueError, TypeError, UnicodeError):
            raise ChatCapabilityInvalidError("chat capability invalid") from None
        if not claims.issued_at <= now or claims.expires_at > claims.issued_at + MAX_TTL_SECONDS:
            raise ChatCapabilityInvalidError("chat capability validity window invalid")
        if now >= claims.expires_at:
            raise ChatCapabilityExpiredError("chat capability expired")
        if claims.run_id != run_id:
            raise ChatAuthorizationRefusedError("chat capability scope refused")
        launch = self.launches.load(claims.run_id)
        if (
            hashlib.sha256(_launch_json(launch).encode()).hexdigest() != claims.launch_digest
            or operation not in launch.operations
            or claims.expires_at > launch.expires_at
        ):
            raise ChatAuthorizationRefusedError("chat capability scope refused")
        self._live(launch, now)
        return launch

    def authorize_resource(
        self,
        launch: ChatLaunch,
        *,
        tenant_id: str,
        owner_user_id: str,
        team_id: str,
        acl_user_ids: frozenset[str],
        session_id: str | None,
        now: int,
    ) -> None:
        """Resource metadata/ACL must be freshly loaded from gateway-owned storage."""
        self._live(launch, now)
        if tenant_id != launch.tenant_id or not owner_user_id or (session_id is not None and session_id != launch.session_id):
            raise ChatAuthorizationRefusedError("chat resource scope refused")
        if owner_user_id != launch.user_id and launch.user_id not in acl_user_ids:
            raise ChatAuthorizationRefusedError("chat resource private")
        self._member(tenant_id, launch.user_id, team_id)
