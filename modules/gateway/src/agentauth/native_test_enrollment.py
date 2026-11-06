"""Dormant, owner-approved CI test capability; never a Cognito login issuer.

No router, environment factory, registration writer or live fixture is installed.
The protected subject designation is permanent (revocation leaves a tombstone).
Deployment adoption must deny deletion of these rows and install the mandatory
model middleware before exposing approval/exchange. See the #8078 design note.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from botocore.exceptions import BotoCoreError, ClientError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from src.agentauth.human_control import authorize_human_session, require_live_human_membership
from src.agentauth.test_run_admission import VerifiedNativeTestIdentity
from src.agentauth.test_run_admission_store import AdmissionRefusedError, TestAdmissionStore, canonical, digest
from src.internal.tool_entitlements import effective_entitlement
from src.shared.models.organization import Team, TeamMembership, User
from src.shared.schemas.auth import TokenContext

ISSUER = "adp.tools-test-capability/v1"
AUDIENCE = "adp-native-test-model-v1"
ALL_IN_AUDIENCE = "adp-native-test-all-in-v1"
OPERATOR_AUDIENCE = "adp-native-operator-local-v1"
OIDC_AUDIENCE = "adp-native-test-enrollment-v1"
CAPABILITY_HEADER = "x-adp-native-test-capability"
OPERATION_HEADER = "x-adp-native-test-operation"
Name = Annotated[str, Field(min_length=1, max_length=255)]
Uuid = Annotated[str, Field(pattern=r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$")]
Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CapabilityRecord(Contract):
    issuer: Literal["adp.tools-test-capability/v1"]
    audience: Literal["adp-native-test-model-v1", "adp-native-test-all-in-v1", "adp-native-operator-local-v1"]
    deployment_id: Name
    tenant_id: Name
    credential_id: Uuid
    run_id: Uuid
    approval_revision: Uuid
    designation_digest: Digest | None = None
    principal_digest: Digest | None = None
    authority_digest: Digest
    token_digest: Digest
    expires_at: int = Field(gt=0)
    status: Literal["issuing", "active", "revoked"]
    revoked_at: int | None = None
    revoked_by: Name | None = None

    @model_validator(mode="after")
    def producer_binding(self):
        if self.audience == OPERATOR_AUDIENCE:
            if self.principal_digest is None or self.designation_digest is not None:
                raise ValueError("operator principal binding required")
        elif self.designation_digest is None or self.principal_digest is not None:
            raise ValueError("fixture binding required")
        return self


class FixtureDesignation(Contract):
    subject_id: Name
    tenant_id: Name
    user_id: Name
    team_id: Name
    department_id: Name
    owner_id: Name
    deployment_id: Name
    revision: Uuid
    revocation_epoch: int = Field(ge=0)
    state: Literal["active", "revoked"]


class ApprovedJob(Contract):
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    repository_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    workflow_ref: str = Field(min_length=1, max_length=512)
    # Reusable workflows need their own pinned job_workflow_sha contract.
    # Initial support is deliberately direct-workflow only.
    job_workflow_ref: None = None
    ref: str = Field(min_length=1, max_length=255)
    sha: Sha
    workflow_sha: Sha
    event_name: Literal["workflow_dispatch"]
    run_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    run_attempt: Literal["1"]
    actor_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    # Exact environment-bound GitHub sub, not an asserted local process name.
    sub: str = Field(pattern=r"^repo:[^:]+/[^:]+:environment:.{1,255}$")
    runner_environment: Literal["github-hosted", "self-hosted"]
    runner_role: str = Field(pattern=r"^arn:aws(?:-us-gov|-cn)?:iam::[0-9]{12}:role/[A-Za-z0-9/+=,.@_-]+$")

    @model_validator(mode="after")
    def same_repository(self):
        if not self.sub.startswith(f"repo:{self.repository}:environment:") or not self.workflow_ref.startswith(
            self.repository + "/.github/workflows/"
        ):
            raise ValueError("job repository mismatch")
        return self


class ApprovalRequest(Contract):
    run_id: Uuid
    scenario: Name
    job: ApprovedJob
    allowed_models: list[Name] = Field(min_length=1, max_length=16)
    allowed_routes: list[Literal["/v1/messages", "/openai/v1/responses"]] = Field(min_length=1, max_length=2)
    tools: list[Name] = Field(min_length=1, max_length=32)
    dispatch_expires_at: int
    cleanup_expires_at: int
    meter_expires_at: int


class EffectRegistration(Contract):
    """Protected source/price registration, copied exactly into owner approval."""

    effect_key: str = Field(pattern=r"^native-effect:[a-z][a-z0-9_.-]{1,127}$")
    action: Literal["call", "list", "recover", "close", "setup", "cleanup", "settle"]
    tool: Name
    contract_id: Name
    contract_sha256: Digest
    source_sha256: Digest


class AllInApprovalRequest(ApprovalRequest):
    scope: Literal["all_in"]
    allowed_models: list[Name] = Field(max_length=16)
    allowed_routes: list[Literal["/v1/messages", "/openai/v1/responses"]] = Field(max_length=2)
    effects: list[EffectRegistration] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def distinct_effects(self):
        if len({effect.effect_key for effect in self.effects}) != len(self.effects) or any(effect.tool not in self.tools for effect in self.effects):
            raise ValueError("invalid all-in effect registration")
        return self


class Approval(ApprovalRequest):
    designation: FixtureDesignation
    approved_by: Name
    approved_at: int
    revision: Uuid
    meter_epoch: Uuid
    credential_id: Uuid

    @property
    def principal(self):
        return self.designation

    @property
    def deployment_id(self):
        return self.designation.deployment_id

    @property
    def audience(self):
        return AUDIENCE

    @property
    def admission_scope(self):
        return "model_only"

    @property
    def source_sha256(self):
        # Digest identifies the approved source revision, not a binary attestation.
        return digest({"repository_id": self.job.repository_id, "sha": self.job.sha, "workflow_sha": self.job.workflow_sha})

    @property
    def runtime_id(self):
        return "github-actions:" + digest(self.job.model_dump())

    @property
    def identity(self):
        fixture = self.designation
        return VerifiedNativeTestIdentity(
            fixture.tenant_id,
            fixture.subject_id,
            fixture.user_id,
            fixture.team_id,
            fixture.department_id,
            self.run_id,
            self.credential_id,
            self.source_sha256,
            self.runtime_id,
        )

    def authority(self):
        return {
            **asdict(self.identity),
            "schema_version": "1",
            "cap_usd": "20",
            "meter_epoch": self.meter_epoch,
            "meter_expires_at": self.meter_expires_at,
            "dispatch_expires_at": self.dispatch_expires_at,
            "cleanup_expires_at": self.cleanup_expires_at,
            "state": "active",
            "allowed_routes": self.allowed_routes,
            "allowed_models": self.allowed_models,
            "revision": self.revision,
        }


class AllInApproval(Approval):
    scope: Literal["all_in"]
    allowed_models: list[Name] = Field(max_length=16)
    allowed_routes: list[Literal["/v1/messages", "/openai/v1/responses"]] = Field(max_length=2)
    effects: list[EffectRegistration] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def distinct_effects(self):
        if len({effect.effect_key for effect in self.effects}) != len(self.effects) or any(effect.tool not in self.tools for effect in self.effects):
            raise ValueError("invalid all-in effect registration")
        return self

    @property
    def audience(self):
        return ALL_IN_AUDIENCE

    @property
    def admission_scope(self):
        return "all_in"

    def authority(self):
        return {
            **super().authority(),
            "scope": "all_in",
            "deployment_id": self.designation.deployment_id,
            "started_at": self.approved_at,
            "effects": [effect.model_dump() for effect in self.effects],
        }


class OperatorPrincipal(Contract):
    """Canonical current human and membership, never a fixture designation."""

    subject_id: Name
    tenant_id: Name
    user_id: Name
    team_id: Name
    department_id: str = Field(max_length=255)
    cognito_username: str = Field(max_length=255)


class OperatorLocalApprovalRequest(Contract):
    producer: Literal["operator_local"]
    run_id: Uuid
    team_id: Name
    deployment_revision: Uuid
    contract_sha256: Digest
    dispatch_expires_at: int
    cleanup_expires_at: int
    meter_expires_at: int


class OperatorLocalApproval(Contract):
    """A local human approves one exact protected Interpreter journey."""

    scope: Literal["operator_local"]
    principal: OperatorPrincipal
    deployment_id: Name
    deployment_revision: Uuid
    contract_sha256: Digest
    source_revision: Sha
    release_sha256: Digest
    gateway_origin: str = Field(pattern=r"^https://[a-z0-9][a-z0-9.-]*[a-z0-9](?::[0-9]{1,5})?$")
    run_id: Uuid
    approved_by: Name
    approved_at: int
    revision: Uuid
    meter_epoch: Uuid
    credential_id: Uuid
    dispatch_expires_at: int
    cleanup_expires_at: int
    meter_expires_at: int
    allowed_models: list[Name] = Field(max_length=0)
    allowed_routes: list[Name] = Field(max_length=0)
    tools: list[Name] = Field(min_length=1, max_length=8)
    effects: list[EffectRegistration] = Field(min_length=1, max_length=8)

    @property
    def audience(self):
        return OPERATOR_AUDIENCE

    @property
    def admission_scope(self):
        return "operator_local"

    @property
    def source_sha256(self):
        return digest({"source_revision": self.source_revision, "release_sha256": self.release_sha256, "contract_sha256": self.contract_sha256})

    @property
    def runtime_id(self):
        return "operator-local:" + self.contract_sha256

    @property
    def identity(self):
        principal = self.principal
        return VerifiedNativeTestIdentity(
            principal.tenant_id,
            principal.subject_id,
            principal.user_id,
            principal.team_id,
            principal.department_id,
            self.run_id,
            self.credential_id,
            self.source_sha256,
            self.runtime_id,
        )

    def authority(self):
        # This is the existing retained all-in ledger, not another accumulator.
        return {
            **asdict(self.identity),
            "schema_version": "1",
            "cap_usd": "20",
            "meter_epoch": self.meter_epoch,
            "meter_expires_at": self.meter_expires_at,
            "dispatch_expires_at": self.dispatch_expires_at,
            "cleanup_expires_at": self.cleanup_expires_at,
            "state": "active",
            "allowed_routes": [],
            "allowed_models": [],
            "revision": self.revision,
            "scope": "all_in",
            "deployment_id": self.deployment_id,
            "started_at": self.approved_at,
            "effects": [effect.model_dump() for effect in self.effects],
        }


def is_effect_approval(approval):
    return type(approval) in {AllInApproval, OperatorLocalApproval}


def capability_principal_binding(approval):
    key = "principal_digest" if type(approval) is OperatorLocalApproval else "designation_digest"
    return {key: digest(approval.principal.model_dump())}


class NativeTestStore(TestAdmissionStore):
    """Dynamo CAS guards include designation and capability revocation.

    Reads are strongly consistent. No row has a TTL: expired authority, consent
    evidence, issuance ambiguity and fixture tombstones must survive cleanup.
    """

    def __init__(self, *, table_name, client):
        super().__init__(table_name=table_name, client=client)
        # The production composition installs the protected deployment verifier.
        # Existing source-only compositions remain dormant without an HTTP mount.
        self._deployment_verifier = None

    def deployment_guards(self, approval):
        return self._deployment_verifier.guards(approval) if self._deployment_verifier is not None else []

    @staticmethod
    def fixture_key(subject_id):
        return {"pk": {"S": "TOOLS_TEST_SUBJECT#" + subject_id}, "sk": {"S": "DESIGNATION"}}

    @staticmethod
    def key(tenant, kind, identifier):
        return {"pk": {"S": "TENANT#" + tenant}, "sk": {"S": f"TOOLS_TEST_{kind}#{identifier}"}}

    def document(self, key, kind):
        row = self._read(key)
        if row is None:
            return None
        try:
            value = json.loads(row["document"]["S"])
            if row["kind"]["S"] != kind or canonical(value) != row["document"]["S"]:
                raise ValueError
            return value
        except (KeyError, TypeError, ValueError):
            raise AdmissionRefusedError("native_test_registry_unavailable") from None

    @staticmethod
    def item(key, kind, document):
        return {**key, "kind": {"S": kind}, "document": {"S": canonical(document)}}

    def put(self, key, kind, document, previous=None):
        result = {"TableName": self.table, "Item": self.item(key, kind, document), "ConditionExpression": "attribute_not_exists(pk)"}
        if previous is not None:
            result.update(
                ConditionExpression="document = :previous AND kind = :kind",
                ExpressionAttributeValues={
                    ":previous": {"S": canonical(previous)},
                    ":kind": {"S": kind},
                },
            )
        return {"Put": result}

    def guard(self, key, kind, document):
        return {
            "ConditionCheck": {
                "TableName": self.table,
                "Key": key,
                "ConditionExpression": "document = :expected AND kind = :kind",
                "ExpressionAttributeValues": {":expected": {"S": canonical(document)}, ":kind": {"S": kind}},
            }
        }

    def transaction(self, items):
        try:
            self.client.transact_write_items(TransactItems=items)
        except (BotoCoreError, ClientError):
            # No retry, read-as-success or capability rotation after a lost ack.
            raise AdmissionRefusedError("native_test_write_unconfirmed") from None

    def designation(self, subject_id):
        raw = self.document(self.fixture_key(subject_id), "native_test_fixture_v1")
        if raw is None:
            return None
        try:
            designation = FixtureDesignation.model_validate(raw)
            if designation.subject_id != subject_id:
                raise ValueError
            return designation
        except (ValidationError, ValueError):
            raise AdmissionRefusedError("native_test_registry_unavailable") from None

    def approval(self, tenant, run):
        raw = self.document(self.key(tenant, "APPROVAL", run), "native_test_approval_v1")
        try:
            model = {"all_in": AllInApproval, "operator_local": OperatorLocalApproval}.get(
                raw.get("scope") if isinstance(raw, dict) else None, Approval
            )
            approval = model.model_validate(raw)
            if approval.principal.tenant_id != tenant or approval.run_id != run:
                raise ValueError
            return approval
        except (ValidationError, ValueError):
            raise AdmissionRefusedError("native_test_approval_unavailable") from None

    def designation_guard(self, designation):
        return self.guard(self.fixture_key(designation.subject_id), "native_test_fixture_v1", designation.model_dump())

    def principal_guards(self, approval):
        if type(approval) is OperatorLocalApproval:
            # A local approval must never remove or ignore a CI designation.
            return [
                {"ConditionCheck": {"TableName": self.table, "Key": self.fixture_key(subject), "ConditionExpression": "attribute_not_exists(pk)"}}
                for subject in sorted({approval.principal.subject_id, approval.principal.user_id})
            ]
        return [self.designation_guard(approval.designation)]

    def approve(self, approval):
        # Same approved job/scenario cannot gain another accumulator by changing
        # run UUID or source. Another test requires a different explicit approval.
        slot = (
            digest(
                {
                    "producer": "operator_local",
                    "subject": approval.principal.subject_id,
                    "deployment_revision": approval.deployment_revision,
                    "contract_sha256": approval.contract_sha256,
                }
            )
            if type(approval) is OperatorLocalApproval
            else digest(
                {
                    "subject": approval.designation.subject_id,
                    "repository_id": approval.job.repository_id,
                    "job_run": approval.job.run_id,
                    "scenario": approval.scenario,
                }
            )
        )
        document = approval.model_dump()
        self.transaction(
            [
                *self.deployment_guards(approval),
                *self.principal_guards(approval),
                self.put(self.key(approval.principal.tenant_id, "APPROVAL", approval.run_id), "native_test_approval_v1", document),
                self.put(self.key(approval.principal.tenant_id, "SLOT", slot), "native_test_slot_v1", {"run_id": approval.run_id}),
            ]
        )

    def capability(self, tenant, credential_id):
        value = self.document(self.key(tenant, "CAPABILITY", credential_id), "native_test_capability_v1")
        try:
            record = CapabilityRecord.model_validate(value)
            if record.tenant_id != tenant or record.credential_id != credential_id or record.model_dump(exclude_none=True) != value:
                raise ValueError
        except (ValidationError, ValueError):
            raise AdmissionRefusedError("native_test_capability_unavailable") from None
        return value

    def authority_guards(self, authority, now):
        approval = self.approval(authority["tenant_id"], authority["run_id"])
        capability = self.capability(authority["tenant_id"], authority["credential_id"])
        active_authority = approval.authority()
        setup = is_effect_approval(approval) and authority.get("state") == "issuing_setup"
        expected_authority = {**active_authority, "state": "issuing_setup"} if setup else active_authority
        if (
            expected_authority != authority
            or (is_effect_approval(approval) and now >= approval.approved_at + 300)
            or capability.get("authority_digest") != digest(active_authority)
            or capability.get("status") != ("issuing" if setup else "active")
            or capability["audience"] != approval.audience
            or capability["run_id"] != approval.run_id
            or capability["deployment_id"] != approval.deployment_id
            or any(capability.get(key) != value for key, value in capability_principal_binding(approval).items())
            or capability["approval_revision"] != approval.revision
            or capability["expires_at"] != authority["dispatch_expires_at"]
            or type(capability.get("expires_at")) is not int
            or capability["expires_at"] <= now
        ):
            raise AdmissionRefusedError("native_test_capability_refused")
        return [
            *super().authority_guards(authority, now),
            *self.deployment_guards(approval),
            *self.principal_guards(approval),
            self.guard(self.key(authority["tenant_id"], "CAPABILITY", authority["credential_id"]), "native_test_capability_v1", capability),
        ]


async def require_fixture(db, approval, policy_store, *, native_catalogue=None):
    """Read canonical membership, existing consent and current entitlements only."""
    fixture = approval.principal
    if type(approval) is OperatorLocalApproval:
        from src.shared.identity.workspaces import workspace_user

        user = await workspace_user(db, fixture.subject_id, fixture.tenant_id, username=fixture.cognito_username)
    else:
        user = await db.scalar(select(User).where(User.id == fixture.user_id, User.org_id == fixture.tenant_id))
    team = await db.scalar(
        select(Team)
        .join(TeamMembership, TeamMembership.team_id == Team.id)
        .where(
            Team.id == fixture.team_id,
            Team.org_id == fixture.tenant_id,
            Team.department_id == fixture.department_id,
            TeamMembership.user_id == fixture.user_id,
            TeamMembership.org_id == fixture.tenant_id,
        )
    )
    if (
        user is None
        or user.id != fixture.user_id
        or user.org_id != fixture.tenant_id
        or user.user_kind != "human"
        or user.is_shadow
        or team is None
        or (type(approval) is not OperatorLocalApproval and user.cognito_sub != fixture.subject_id)
    ):
        raise AdmissionRefusedError("native_test_membership_refused")
    await require_live_human_membership(db, user_id=fixture.user_id, tenant_id=fixture.tenant_id)
    policy = await run_in_threadpool(policy_store.get, tenant_id=fixture.tenant_id, owner_id=fixture.user_id)
    if not policy or policy.get("status") != "active":
        raise AdmissionRefusedError("native_test_consent_unavailable")
    from src.internal.tools_catalogue import gateway_catalogue

    catalogue = await run_in_threadpool(gateway_catalogue) if native_catalogue is None else await run_in_threadpool(native_catalogue.read, approval)
    deployed = {entry["name"] for entry in catalogue["tools"]}
    for tool in approval.tools:
        if tool not in deployed or tool not in policy.get("allowed_tools", []) or tool not in policy.get("consented_tools", []):
            raise AdmissionRefusedError("native_test_consent_refused")
        granted, ceiling = await effective_entitlement(db, tenant_id=fixture.tenant_id, team_id=fixture.team_id, tool_name=tool)
        maximum = Decimal(str(policy.get("tool_max_cost_usd", {}).get(tool, "NaN")))
        if not granted or ceiling is None or not ceiling.is_finite() or ceiling <= 0 or not maximum.is_finite() or maximum <= 0:
            raise AdmissionRefusedError("native_test_entitlement_refused")


class NativeTestEnrollment:
    """Private composition service. No external endpoint calls it in this slice."""

    def __init__(self, *, store, session_factory, policy_store, reservations, deployment_id, clock=None):
        if type(store) is not NativeTestStore or not deployment_id:
            raise AdmissionRefusedError("native_test_enrollment_unavailable")
        self.store, self.session_factory, self.policy_store = store, session_factory, policy_store
        self.reservations, self.deployment_id = reservations, deployment_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self._enforcer = None
        self._effect_runtime = None

    def require_enforcer(self):
        from src.agentauth.native_test_model import NativeTestModelMiddleware

        if not isinstance(self._enforcer, NativeTestModelMiddleware) or self._enforcer.enrollment is not self:
            raise AdmissionRefusedError("mandatory_native_test_enforcement_unavailable")

    def active(self, designation):
        if designation is None or designation.state != "active" or designation.deployment_id != self.deployment_id:
            raise AdmissionRefusedError("native_test_designation_refused")

    def _fixture_catalogue(self, approval):
        from src.agentauth.native_test_atlassian_runtime import NativeAtlassianRuntime
        from src.agentauth.native_test_interpreter_runtime import NativeInterpreterRuntime
        from src.agentauth.native_test_knowledge_runtime import NativeKnowledgeRuntime
        from src.agentauth.native_test_stateless_runtime import NativeStatelessRuntime

        if is_effect_approval(approval) and type(self._effect_runtime) in {
            NativeStatelessRuntime,
            NativeKnowledgeRuntime,
            NativeInterpreterRuntime,
            NativeAtlassianRuntime,
        }:
            self._effect_runtime.coverage()
            return self._effect_runtime.catalogue
        return None

    async def current_fixture(self, approval):
        await run_in_threadpool(self.store.deployment_guards, approval)
        if type(approval) is OperatorLocalApproval:
            for subject in {approval.principal.subject_id, approval.principal.user_id}:
                if await run_in_threadpool(self.store.designation, subject) is not None:
                    raise AdmissionRefusedError("native_test_designation_refused")
        else:
            current = await run_in_threadpool(self.store.designation, approval.designation.subject_id)
            self.active(current)
            if current != approval.designation:
                raise AdmissionRefusedError("native_test_designation_changed")
        async with self.session_factory() as db:
            await require_fixture(db, approval, self.policy_store, native_catalogue=self._fixture_catalogue(approval))
        if is_effect_approval(approval):
            await self.require_all_in(approval)

    async def require_all_in(self, approval):
        # Runtime composition must install the concrete HTTP dispatch, original
        # cleanup and setup/settlement boundary before this scope can be issued.
        if self._effect_runtime is None or self._effect_runtime.enrollment is not self:
            raise AdmissionRefusedError("native_test_all_in_runtime_unavailable")
        if await self._effect_runtime.require_ready(approval) is not None:
            raise AdmissionRefusedError("native_test_all_in_runtime_unavailable")

    async def approve_operator(self, context, *, request):
        from src.agentauth.native_test_configuration import NativeOperatorDeployment
        from src.shared.identity.workspaces import workspace_user

        self.require_enforcer()
        now = int(self.clock().timestamp())
        verifier = self.store._deployment_verifier
        configuration = await run_in_threadpool(verifier.current) if verifier is not None else None
        if type(configuration) is not NativeOperatorDeployment or (
            request.deployment_revision != configuration.revision or request.contract_sha256 != configuration.contract_sha256
        ):
            raise AdmissionRefusedError("native_operator_deployment_refused")
        if (
            not now < request.dispatch_expires_at <= now + 300
            or not request.dispatch_expires_at <= request.cleanup_expires_at <= now + 600
            or not request.cleanup_expires_at < request.meter_expires_at <= now + 7 * 86400
            or context.expires_at.timestamp() < request.dispatch_expires_at
        ):
            raise AdmissionRefusedError("native_test_windows_refused")
        async with self.session_factory() as db:
            if (
                context.account_type != "human"
                or context.auth_source != "jwt"
                or context.expires_at <= self.clock()
                or context.org_id != configuration.tenant_id
                or context.attributed_org_id != context.org_id
                or (context._authenticated_subject_id and context._authenticated_subject_id != context.user_id)
            ):
                raise AdmissionRefusedError("native_operator_owner_refused")
            # The session is new for each authority check. The workspace helper
            # accepts only proven login links, not raw-ID fallback from resolver.
            user = await workspace_user(db, context.user_id, context.org_id, username=context.cognito_username or "")
            if user is None or user.user_kind != "human" or user.is_shadow or user.org_id != context.org_id:
                raise AdmissionRefusedError("native_operator_owner_refused")
            await require_live_human_membership(db, user_id=user.id, tenant_id=user.org_id)
            team = await db.scalar(
                select(Team)
                .join(TeamMembership, TeamMembership.team_id == Team.id)
                .where(
                    Team.id == request.team_id, Team.org_id == user.org_id, TeamMembership.org_id == user.org_id, TeamMembership.user_id == user.id
                )
            )
            if team is None:
                raise AdmissionRefusedError("native_operator_owner_refused")
            principal = OperatorPrincipal(
                subject_id=context.user_id,
                tenant_id=user.org_id,
                user_id=user.id,
                team_id=team.id,
                department_id=team.department_id or "",
                cognito_username=context.cognito_username or "",
            )
        approval = OperatorLocalApproval(
            scope="operator_local",
            principal=principal,
            deployment_id=configuration.deployment_id,
            deployment_revision=configuration.revision,
            contract_sha256=configuration.contract_sha256,
            source_revision=configuration.source_revision,
            release_sha256=configuration.release_sha256,
            gateway_origin=configuration.gateway_origin,
            run_id=request.run_id,
            approved_by=principal.user_id,
            approved_at=now,
            revision=str(uuid4()),
            meter_epoch=str(uuid4()),
            credential_id=str(uuid4()),
            dispatch_expires_at=request.dispatch_expires_at,
            cleanup_expires_at=request.cleanup_expires_at,
            meter_expires_at=request.meter_expires_at,
            allowed_models=[],
            allowed_routes=[],
            tools=configuration.allowed_tools,
            effects=configuration.effects,
        )
        await self.current_fixture(approval)
        self.require_login(context, approval, self.clock())
        if self.clock().timestamp() >= approval.dispatch_expires_at:
            raise AdmissionRefusedError("native_test_owner_expired")
        await run_in_threadpool(self.store.approve, approval)
        return approval

    async def exchange_operator(self, context, *, run_id):
        self.require_enforcer()
        approval = await run_in_threadpool(self.store.approval, context.org_id, run_id)
        if type(approval) is not OperatorLocalApproval:
            raise AdmissionRefusedError("native_test_producer_refused")
        self.require_login(context, approval, self.clock())
        await self.current_fixture(approval)
        return await self._issue(context, approval, proof_expires_at=context.expires_at.timestamp())

    async def current_operator_owner(self, context, approval):
        """Current canonical owner for retained cleanup; no fresh consent grant."""
        from src.shared.identity.workspaces import workspace_user

        self.require_login(context, approval, self.clock())
        principal = approval.principal
        async with self.session_factory() as db:
            user = await workspace_user(db, principal.subject_id, principal.tenant_id, username=principal.cognito_username)
            team = await db.scalar(
                select(Team)
                .join(TeamMembership, TeamMembership.team_id == Team.id)
                .where(
                    Team.id == principal.team_id,
                    Team.org_id == principal.tenant_id,
                    TeamMembership.org_id == principal.tenant_id,
                    TeamMembership.user_id == principal.user_id,
                )
            )
            if (
                user is None
                or user.id != principal.user_id
                or user.org_id != principal.tenant_id
                or user.user_kind != "human"
                or user.is_shadow
                or team is None
                or (team.department_id or "") != principal.department_id
            ):
                raise AdmissionRefusedError("native_operator_owner_refused")
            await require_live_human_membership(db, user_id=user.id, tenant_id=user.org_id)

    async def approve(self, owner_context: TokenContext, *, subject_id: str, request: ApprovalRequest):
        self.require_enforcer()
        designation = await run_in_threadpool(self.store.designation, subject_id)
        self.active(designation)
        now = int(self.clock().timestamp())
        if (
            not now < request.dispatch_expires_at <= now + 900
            or not request.dispatch_expires_at <= request.cleanup_expires_at < request.meter_expires_at
        ):
            raise AdmissionRefusedError("native_test_windows_refused")
        if request.cleanup_expires_at > now + 3600 or request.meter_expires_at > now + 7 * 86400:
            raise AdmissionRefusedError("native_test_windows_refused")
        async with self.session_factory() as db:
            owner = await authorize_human_session(owner_context, db, now=self.clock())
            if (
                owner.user_id != designation.owner_id
                or owner.tenant_id != designation.tenant_id
                or owner_context.attributed_org_id != owner_context.org_id
            ):
                raise AdmissionRefusedError("native_test_owner_refused")
            approval = (AllInApproval if isinstance(request, AllInApprovalRequest) else Approval)(
                **request.model_dump(),
                designation=designation,
                approved_by=owner.user_id,
                approved_at=now,
                revision=str(uuid4()),
                meter_epoch=str(uuid4()),
                credential_id=str(uuid4()),
            )
            await require_fixture(db, approval, self.policy_store, native_catalogue=self._fixture_catalogue(approval))
        if is_effect_approval(approval):
            if approval.cleanup_expires_at < approval.dispatch_expires_at + 600:
                raise AdmissionRefusedError("native_test_windows_refused")
            await self.require_all_in(approval)
        if owner_context.expires_at <= self.clock() or self.clock().timestamp() >= approval.dispatch_expires_at:
            raise AdmissionRefusedError("native_test_owner_expired")
        await run_in_threadpool(self.store.approve, approval)
        return approval

    async def verify_job(self, approval, token, proof):
        from src.agentauth.arc_model import github_claims
        from src.agentauth.bootstrap import envelope_digest
        from src.agentauth.work_routes import verify_producer

        claims = await github_claims(token, audience=OIDC_AUDIENCE)
        job = approval.job.model_dump()
        if any(claims.get(name) != value for name, value in job.items() if name != "runner_role"):
            raise AdmissionRefusedError("native_test_job_refused")
        envelope = {
            "tenant_id": approval.designation.tenant_id,
            "run_id": approval.run_id,
            "approval_revision": approval.revision,
            "github_oidc_token": token,
            "deployment_id": self.deployment_id,
        }
        role = await verify_producer(proof, envelope_digest(envelope), allowed_roles={approval.job.runner_role})
        if role != approval.job.runner_role or claims["exp"] <= self.clock().timestamp():
            raise AdmissionRefusedError("native_test_job_expired")
        return claims

    @staticmethod
    def require_login(context, approval, now):
        fixture = approval.principal
        if (
            context.account_type != "human"
            or context.auth_source != "jwt"
            or context.expires_at <= now
            or context.org_id != fixture.tenant_id
            or context.attributed_org_id != fixture.tenant_id
            or context.user_id != fixture.subject_id
            or (context._authenticated_subject_id and context._authenticated_subject_id != fixture.subject_id)
            or (type(approval) is OperatorLocalApproval and (context.cognito_username or "") != fixture.cognito_username)
        ):
            raise AdmissionRefusedError("native_test_fixture_login_required")

    async def exchange(self, context, *, run_id, github_oidc_token, producer_proof):
        self.require_enforcer()
        approval = await run_in_threadpool(self.store.approval, context.org_id, run_id)
        self.require_login(context, approval, self.clock())
        await self.current_fixture(approval)
        if type(approval) is OperatorLocalApproval:
            raise AdmissionRefusedError("native_test_producer_refused")
        claims = await self.verify_job(approval, github_oidc_token, producer_proof)
        return await self._issue(context, approval, proof_expires_at=claims["exp"])

    async def _issue(self, context, approval, *, proof_expires_at):
        """One issuance CAS, original meter and setup hold for either producer."""
        run_id = approval.run_id
        now = int(self.clock().timestamp())
        expiry = approval.dispatch_expires_at
        if now >= expiry or context.expires_at.timestamp() < expiry:
            raise AdmissionRefusedError("native_test_capability_expired")
        token = "adpt1." + approval.credential_id + "." + secrets.token_urlsafe(32)
        authority = approval.authority()
        capability = {
            "issuer": ISSUER,
            "audience": approval.audience,
            "deployment_id": self.deployment_id,
            "tenant_id": context.org_id,
            "credential_id": approval.credential_id,
            "run_id": approval.run_id,
            "approval_revision": approval.revision,
            **capability_principal_binding(approval),
            "authority_digest": digest(authority),
            "token_digest": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": expiry,
            "status": "issuing",
        }
        cap_key = self.store.key(context.org_id, "CAPABILITY", approval.credential_id)
        approval_guard = self.store.guard(self.store.key(context.org_id, "APPROVAL", run_id), "native_test_approval_v1", approval.model_dump())
        staging_authority = None
        staging_writes = []
        if is_effect_approval(approval):
            staging_authority = {**authority, "state": "issuing_setup"}
            staged = self.store.put(self.store.authority_key(context.org_id, run_id), "tools_test_run_v1", staging_authority)
            staged["Put"]["Item"]["dispatch_expires_at"] = {"N": str(approval.dispatch_expires_at)}
            staging_writes.append(staged)
        await run_in_threadpool(
            self.store.transaction,
            [
                *await run_in_threadpool(self.store.deployment_guards, approval),
                *self.store.principal_guards(approval),
                approval_guard,
                self.store.put(cap_key, "native_test_capability_v1", capability),
                *staging_writes,
            ],
        )
        # One durable issuance owner BEFORE Redis. No retry/reinitialization if
        # either store acknowledgement is lost, even if Redis looks empty later.
        from src.agentauth.test_run_admission import TestRunAdmissionService

        target = TestRunAdmissionService(store=self.store, clock=self.clock)._target(authority)
        script = """
        if redis.call('EXISTS', KEYS[1]) ~= 0 then return 0 end
        redis.call('HSET', KEYS[1], '__initialized__', ARGV[1])
        redis.call('EXPIREAT', KEYS[1], ARGV[2])
        return 1
        """
        try:
            client = await self.reservations._get_client()
            created = await client.eval(script, 1, target.key(), "0:" + str(approval.meter_expires_at), approval.meter_expires_at)
            if created != 1 or await self.reservations.snapshot(target) is None:
                raise ValueError
        except Exception:
            raise AdmissionRefusedError("native_test_meter_initialization_unconfirmed") from None
        setup = None
        if is_effect_approval(approval):
            # Setup, issuer, runner and evidence work belongs to this original
            # allowance too. No capability escapes before its strict reservation.
            setup = await self._effect_runtime.reserve_setup(context, approval, capability)
        await self.current_fixture(approval)
        self.require_login(context, approval, self.clock())
        if self.clock().timestamp() >= min(expiry, proof_expires_at):
            raise AdmissionRefusedError("native_test_capability_expired")
        if is_effect_approval(approval):
            from src.agentauth.native_test_setup_admission import NativeSetupHold

            if type(setup) is not NativeSetupHold:
                raise AdmissionRefusedError("native_test_all_in_setup_unconfirmed")
            await setup.confirm(context=context, enrollment=self, approval=approval, capability=capability)
        active = {**capability, "status": "active"}
        authority_put = self.store.put(self.store.authority_key(context.org_id, run_id), "tools_test_run_v1", authority, staging_authority)
        authority_put["Put"]["Item"]["dispatch_expires_at"] = {"N": str(approval.dispatch_expires_at)}
        await run_in_threadpool(
            self.store.transaction,
            [
                *await run_in_threadpool(self.store.deployment_guards, approval),
                *self.store.principal_guards(approval),
                approval_guard,
                self.store.put(cap_key, "native_test_capability_v1", active, capability),
                authority_put,
            ],
        )
        if self.clock().timestamp() >= min(expiry, proof_expires_at):
            raise AdmissionRefusedError("native_test_issuance_reconciliation_required")
        return token

    async def verify(self, context, credential):
        self.require_enforcer()
        try:
            prefix, credential_id, secret = credential.split(".")
            if prefix != "adpt1" or str(UUID(credential_id)) != credential_id or len(secret) != 43:
                raise ValueError
            capability = await run_in_threadpool(self.store.capability, context.org_id, credential_id)
            approval = await run_in_threadpool(self.store.approval, context.org_id, capability["run_id"])
            self.require_login(context, approval, self.clock())
            if (
                capability["issuer"] != ISSUER
                or capability["audience"] != approval.audience
                or capability["deployment_id"] != self.deployment_id
                or capability["status"] != "active"
                or capability["expires_at"] <= self.clock().timestamp()
                or capability["credential_id"] != approval.credential_id
                or capability["approval_revision"] != approval.revision
                or any(capability.get(key) != value for key, value in capability_principal_binding(approval).items())
                or capability["authority_digest"] != digest(approval.authority())
                or not hmac.compare_digest(capability["token_digest"], hashlib.sha256(credential.encode()).hexdigest())
            ):
                raise ValueError
            await self.current_fixture(approval)
            if is_effect_approval(approval) and self.clock().timestamp() >= approval.approved_at + 300:
                raise ValueError
            if capability["expires_at"] <= self.clock().timestamp():
                raise ValueError
            return approval.identity
        except AdmissionRefusedError:
            raise
        except Exception:
            raise AdmissionRefusedError("native_test_capability_refused") from None

    async def verify_effect(self, context, credential, effect_key):
        identity = await self.verify(context, credential)
        approval = await run_in_threadpool(self.store.approval, identity.tenant_id, identity.run_id)
        if not is_effect_approval(approval) or effect_key not in {
            effect.effect_key for effect in approval.effects if effect.action in {"call", "list", "recover", "close"}
        }:
            raise AdmissionRefusedError("native_test_all_in_credential_unavailable")
        await self.require_all_in(approval)
        return identity

    async def revoke(self, owner_context, *, run_id):
        """Revocation leaves the meter, immutable approval and cleanup owner intact."""
        approval = await run_in_threadpool(self.store.approval, owner_context.org_id, run_id)
        if type(approval) is OperatorLocalApproval:
            await self.current_operator_owner(owner_context, approval)
            owner_id, tenant_id = approval.principal.user_id, approval.principal.tenant_id
        else:
            async with self.session_factory() as db:
                owner = await authorize_human_session(owner_context, db, now=self.clock())
                if owner.user_id != approval.approved_by or owner.tenant_id != approval.principal.tenant_id:
                    raise AdmissionRefusedError("native_test_owner_refused")
                owner_id, tenant_id = owner.user_id, owner.tenant_id
        capability = await run_in_threadpool(self.store.capability, tenant_id, approval.credential_id)
        if owner_context.expires_at <= self.clock():
            raise AdmissionRefusedError("native_test_owner_expired")
        await run_in_threadpool(
            self.store.transaction,
            [
                self.store.put(
                    self.store.key(tenant_id, "CAPABILITY", approval.credential_id),
                    "native_test_capability_v1",
                    {**capability, "status": "revoked", "revoked_at": int(self.clock().timestamp()), "revoked_by": owner_id},
                    capability,
                )
            ],
        )
