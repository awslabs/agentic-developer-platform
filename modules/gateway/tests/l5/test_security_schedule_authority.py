"""Gateway Security schedule authority: consent selection and zero provider effects.

Offline Gateway regressions against a recording fake Security Agent client and a
fake scoped session. Nothing here contacts AWS, qualifies a native space, or
changes an owner, credential or consent record. ``require_execution_scope``,
``assess_readiness``, the real binding adapter and the real threat-model
reconciliation all run; only the AWS session, the product-scope broker and
source-visibility reads are replaced.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Namespace

import pytest
from fastapi import HTTPException
from l5_services.models.binding import Binding
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.event import semantic_key

from src import l5_security_schedule
from src.l5_service_identity import worker_id
from src.products.models import ProductProjectBinding
from src.shared.models.vault import CredentialServiceDelegation, UserCredential

TENANT, PRODUCT, PROJECT, CONFIG = "tenant-a", "product-a", "project-a", "config-a"
OWNER, CREDENTIAL, ACCOUNT = "owner-a", "credential-a", "111122223333"
OPERATION, INTENT_HASH = "operation-a", "c" * 64
IDENTITY = {"account_id": ACCOUNT, "role_arn": f"arn:aws:iam::{ACCOUNT}:role/adp-security", "region": "us-east-1"}
REGISTRATION = {"service_role": f"arn:aws:iam::{ACCOUNT}:role/adp-security-agent", "bucket": "adp-security-evidence"}
SPACE_CREATED = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
MODEL_CREATED = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
CONNECTION = {
    "tenant_id": TENANT,
    "project_id": PROJECT,
    "source_connection_id": CREDENTIAL,
    "record_type": "connection",
    "record_id": CREDENTIAL,
    "record_revision": "secret-version-1",
}
DOCUMENT = {
    "tenant_id": TENANT,
    "project_id": PROJECT,
    "source_connection_id": CREDENTIAL,
    "record_type": "document",
    "record_id": "doc-a",
    "record_revision": "pinned-document",
}
COMMIT = {**DOCUMENT, "record_type": "repository_commit", "record_id": "repo-a", "record_revision": "pinned-commit"}
SECOND_DOCUMENT = {**DOCUMENT, "record_id": "doc-b", "record_revision": "pinned-document-b"}
CREDENTIAL_SOURCE = {**CONNECTION, "record_type": "credential"}
SPACE = {**DOCUMENT, "record_type": "security_space", "record_id": "space-a", "record_revision": SPACE_CREATED.isoformat()}
MODEL = {**DOCUMENT, "record_type": "threat_model", "record_id": "model-one", "record_revision": MODEL_CREATED.isoformat()}
CREATION_RECEIPT = {
    "operation_id": OPERATION,
    "intent_hash": INTENT_HASH,
    "native_resource_ref": deepcopy(SPACE),
    "provider_identity": deepcopy(IDENTITY),
}
STORED_MODEL = {"kind": "threat_model", "native_model_ref": deepcopy(MODEL), "observed_at": "2026-10-01T09:05:00+00:00"}
STORED_REGISTRATION = {"kind": "code_review_source_registration", "registration": deepcopy(REGISTRATION), "observed_at": "2026-10-01T09:05:00+00:00"}
THREAT = {
    "trigger_id": "nightly-threat-model",
    "kind": "schedule",
    "enabled": True,
    "schedule": "30 2 * * *",
    "capability": "threat_model",
    "binding_id": "binding-threat",
    "source_refs": [deepcopy(DOCUMENT)],
}
REVIEW = {
    "trigger_id": "nightly-code-review",
    "kind": "schedule",
    "enabled": True,
    "schedule": "15 3 * * *",
    "capability": "code_review",
    "binding_id": "binding-code",
    "source_refs": [deepcopy(COMMIT)],
}
OPERATIONS = ["credential:session", "product:read", "source:pin"]
CAPABILITIES = ["code_review", "threat_model"]


def _settings(**overrides):
    return {
        "enabled": True,
        "purpose": "Assess the selected product sources",
        "classification": "internal",
        "visibility": "only_me",
        "region": "us-east-1",
        "connection_ref": deepcopy(CONNECTION),
        "profile_ref": deepcopy(DOCUMENT),
        "limits": {
            "deadline_seconds": 3600,
            "max_provider_attempts": 1,
            "max_polls": 30,
            "concurrency": 1,
            "max_cost": "1.00",
            "currency": "USD",
            "policy_ref": {**CONNECTION, "record_type": "policy", "record_id": "example-budget"},
        },
        "capabilities": ["threat_model", "code_review"],
        "source_refs": [deepcopy(DOCUMENT), deepcopy(COMMIT)],
        "triggers": [deepcopy(THREAT), deepcopy(REVIEW)],
        **overrides,
    }


def _binding(binding_id, *, generation=4, readiness=None, state="ready"):
    return Namespace(
        tenant_id=TENANT,
        binding_id=binding_id,
        configuration_id=CONFIG,
        product_id=PRODUCT,
        project_id=PROJECT,
        service_id="security",
        scope=None,
        revision=1,
        generation=generation,
        state=state,
        ownership="adp_created",
        connection_ref=deepcopy(CONNECTION),
        connection_key=binding_id,
        native_resource_ref=deepcopy(SPACE),
        native_key=binding_id,
        provider_identity=deepcopy(IDENTITY),
        creation_receipt=deepcopy(CREATION_RECEIPT),
        next_check_at=None,
        gaps=[],
        readiness=deepcopy([STORED_REGISTRATION, STORED_MODEL] if readiness is None else readiness),
    )


def _credential():
    return Namespace(
        id=CREDENTIAL,
        org_id=TENANT,
        user_id=OWNER,
        service="aws",
        credential_type="aws_role",
        aws_verified_version_id=CONNECTION["record_revision"],
        label="selected connection",
        secret_arn=f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:connection",
        scopes={"account_id": ACCOUNT, "role_arn": IDENTITY["role_arn"]},
    )


def _consent(identifier, *, binding_id, generation=4, sources=None, revision=1):
    sources = deepcopy(sources if sources is not None else [CREDENTIAL_SOURCE, DOCUMENT, COMMIT])
    scope = {
        "tenant_id": TENANT,
        "product_id": PRODUCT,
        "project_id": PROJECT,
        "service_id": worker_id("security"),
        "credential_id": CREDENTIAL,
        "created_by": OWNER,
        "configuration_id": CONFIG,
        "configuration_revision": revision,
        "configuration_hash": "a" * 64,
        "binding_id": binding_id,
        "binding_generation": generation,
        "visibility": "only_me",
        "region": "us-east-1",
        "provider_account_id": ACCOUNT,
        "provider_role_arn": IDENTITY["role_arn"],
        "operations": list(OPERATIONS),
        "capabilities": list(CAPABILITIES),
        "sources": deepcopy(sources),
    }
    return Namespace(
        id=identifier,
        tenant_id=TENANT,
        product_id=PRODUCT,
        project_id=PROJECT,
        credential_id=CREDENTIAL,
        created_by=OWNER,
        service_id=worker_id("security"),
        parent_delegation_id=None,
        revoked_at=None,
        revision=1,
        visibility="only_me",
        expires_at=datetime.now(UTC) + timedelta(hours=12),
        binding_generation=generation,
        operations=list(OPERATIONS),
        capabilities=list(CAPABILITIES),
        sources=sources,
        execution_scope=scope,
    )


def _receipt(trigger_id, *, capability, binding_id, sources, revision=1, instant="2026-10-31T02:30:00Z"):
    receipt = Namespace(
        tenant_id=TENANT,
        product_id=PRODUCT,
        project_id=PROJECT,
        service_id="security",
        trigger_id=trigger_id,
        event_id=f"schedule:{instant}",
        event_kind="schedule",
        state="pending",
        configuration_id=CONFIG,
        configuration_revision=revision,
        target=None,
        run_input={"configuration_revision": revision, "binding_id": binding_id, "capability": capability, "sources": deepcopy(sources)},
    )
    receipt.idempotency_key = semantic_key(receipt)
    return receipt


def _threat_tick(**overrides):
    return _receipt("nightly-threat-model", capability="threat_model", binding_id="binding-threat", sources=[DOCUMENT], **overrides)


def _review_tick(**overrides):
    return _receipt("nightly-code-review", capability="code_review", binding_id="binding-code", sources=[COMMIT], **overrides)


class SelectionDatabase:
    """Entity-keyed reads only; no SQL predicate is re-implemented here."""

    def __init__(self, configuration, version, bindings, credential, consents, relationship):
        self.configuration, self.version, self.bindings = configuration, version, bindings
        self.credential, self.consents, self.relationship = credential, consents, relationship
        self.flushes = 0

    async def get(self, model, key, **_):
        if model is Configuration:
            return self.configuration if key == (TENANT, CONFIG) else None
        if model is ConfigurationVersion:
            return self.version if key == (TENANT, CONFIG, self.version.revision) else None
        if model is Binding:
            return next((item for item in self.bindings if (item.tenant_id, item.binding_id) == key), None)
        if model is UserCredential:
            return self.credential if key == self.credential.id else None
        raise AssertionError(f"unexpected read of {model}")

    async def scalars(self, statement):
        rows = {Binding: self.bindings, CredentialServiceDelegation: self.consents}[statement.column_descriptions[0]["entity"]]
        return Namespace(all=lambda: list(rows))

    async def scalar(self, statement):
        assert statement.column_descriptions[0]["entity"] is ProductProjectBinding
        return self.relationship

    async def flush(self):
        self.flushes += 1


class FlushOnly:
    def __init__(self):
        self.flushes = 0

    async def flush(self):
        self.flushes += 1


class RecordingSecurityAgent:
    """Fake Security Agent API recording every provider call it is asked to make."""

    def __init__(self, models=(("model-one", MODEL_CREATED),)):
        self.calls = []
        self.models = list(models)

    @property
    def names(self):
        return [name for name, _ in self.calls]

    def batch_get_agent_spaces(self, agentSpaceIds):  # noqa: N803 - provider API shape
        self.calls.append(("batch_get_agent_spaces", tuple(agentSpaceIds)))
        return {
            "agentSpaces": [
                {
                    "agentSpaceId": SPACE["record_id"],
                    "name": f"adp-{OPERATION}",
                    "description": f"ADP setup {INTENT_HASH}",
                    "createdAt": SPACE_CREATED,
                    "awsResources": {"iamRoles": [REGISTRATION["service_role"]], "s3Buckets": [f"arn:aws:s3:::{REGISTRATION['bucket']}"]},
                }
            ]
        }

    def list_threat_models(self, agentSpaceId, maxResults, nextToken=None):  # noqa: N803 - provider API shape
        self.calls.append(("list_threat_models", agentSpaceId))
        return {"threatModelSummaries": [{"threatModelId": identifier, "title": f"adp-{OPERATION}-threat-model"} for identifier, _ in self.models]}

    def batch_get_threat_models(self, agentSpaceId, threatModelIds):  # noqa: N803 - provider API shape
        self.calls.append(("batch_get_threat_models", tuple(threatModelIds)))
        created = dict(self.models)
        return {
            "threatModels": [
                {
                    "threatModelId": identifier,
                    "agentSpaceId": agentSpaceId,
                    "title": f"adp-{OPERATION}-threat-model",
                    "description": f"ADP setup {INTENT_HASH}",
                    "serviceRole": REGISTRATION["service_role"],
                    "createdAt": created[identifier],
                }
                for identifier in threatModelIds
            ]
        }

    def create_threat_model(self, **parameters):
        self.calls.append(("create_threat_model", parameters))
        raise AssertionError("scheduled admission must never create a provider threat model")


def _environment(monkeypatch, *, consents, bindings=None, settings=None, revision=1, generation=4):
    configuration = Namespace(
        tenant_id=TENANT,
        configuration_id=CONFIG,
        product_id=PRODUCT,
        project_id=PROJECT,
        service_id="security",
        relationship_revision="7",
        owner_id=OWNER,
        current_revision=revision,
        handoff_state="linked",
    )
    version = Namespace(
        tenant_id=TENANT,
        configuration_id=CONFIG,
        revision=revision,
        settings=settings if settings is not None else _settings(),
        inherited_fields=[],
        gaps=["execution_delegation"],
        state="needs_input",
        hash="a" * 64,
    )
    rows = bindings if bindings is not None else [_binding("binding-threat", generation=generation), _binding("binding-code", generation=generation)]
    # The stored binding scope is the configuration's own scope, so it is read
    # from the real view rather than restated here.
    for row in rows:
        row.scope = deepcopy(l5_security_schedule.view(configuration, version)["scope"])
    credential = _credential()
    database = SelectionDatabase(configuration, version, rows, credential, consents, Namespace(relationship_revision=7))
    actor = Namespace(account_type="service", auth_source="iam", scope="internal", agent_registry_id=worker_id("security"), credential_scopes=[])
    requested = []

    async def product_scope(db, caller, delegation_id, product_id, project_id, operation):
        requested.append((delegation_id, operation))
        assert caller is actor and (product_id, project_id) == (PRODUCT, PROJECT)
        return next((item for item in consents if item.id == delegation_id), None)

    async def visible(db, grant):
        return [Namespace(binding_id=grant.execution_scope.get("binding_id"))]

    monkeypatch.setattr(l5_security_schedule, "schedule_product_scope", product_scope)
    monkeypatch.setattr(l5_security_schedule, "verified_connection_evidence", lambda credential: None)
    monkeypatch.setattr(l5_security_schedule, "service_visible_bindings", visible)
    monkeypatch.setattr(l5_security_schedule, "source_ref_authorized", lambda grant, item, source: True)
    return Namespace(
        db=database, actor=actor, configuration=configuration, version=version, bindings=rows, credential=credential, requested=requested
    )


def _install_provider(monkeypatch, client):
    async def provider_session(backend, credential, identity, parent, audit_id):
        assert (identity["account_id"], identity["role_arn"]) == (ACCOUNT, IDENTITY["role_arn"])
        assert parent.created_by == OWNER and audit_id
        return Namespace(client=lambda service, region_name: client)

    monkeypatch.setattr(l5_security_schedule, "_provider_session", provider_session)


@pytest.mark.asyncio
async def test_each_capability_uses_its_own_binding_and_its_own_owner_consent(monkeypatch):
    """Two bindings with one consent each: neither grant may mask the other."""
    consents = [_consent("consent-threat", binding_id="binding-threat"), _consent("consent-code", binding_id="binding-code")]
    environment = _environment(monkeypatch, consents=consents)
    backend = Namespace(db=environment.db, actor=environment.actor)

    _, _, binding, _, parent, verified = await l5_security_schedule.current_selection(backend, _threat_tick())
    assert (binding.binding_id, parent.id) == ("binding-threat", "consent-threat")
    assert (verified["delegation_id"], verified["binding_generation"]) == ("consent-threat", 4)
    assert verified["source_refs"] == [DOCUMENT] and verified["trigger"]["capability"] == "threat_model"
    assert verified["causal_origin"] == "schedule" and verified["binding_id"] == "binding-threat"

    _, _, review, _, review_parent, review_verified = await l5_security_schedule.current_selection(backend, _review_tick())
    assert (review.binding_id, review_parent.id) == ("binding-code", "consent-code")
    assert review_verified["source_refs"] == [COMMIT] and review_verified["binding_id"] == "binding-code"
    assert environment.requested == [("consent-threat", "product:read"), ("consent-code", "product:read")]


@pytest.mark.asyncio
async def test_a_sibling_binding_consent_alone_never_authorizes_this_binding(monkeypatch):
    """Control: the only retained grant is scoped to the other binding."""
    environment = _environment(monkeypatch, consents=[_consent("consent-code", binding_id="binding-code")])
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(Namespace(db=environment.db, actor=environment.actor), _threat_tick())
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "delegation_scope_changed"


@pytest.mark.asyncio
async def test_two_indistinguishable_exact_scope_grants_are_refused_not_guessed(monkeypatch):
    consents = [_consent("consent-one", binding_id="binding-threat"), _consent("consent-two", binding_id=None)]
    environment = _environment(monkeypatch, consents=consents)
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(Namespace(db=environment.db, actor=environment.actor), _threat_tick())
    assert (denied.value.status_code, denied.value.detail["code"]) == (403, "delegation_denied")
    assert environment.requested == []


@pytest.mark.asyncio
async def test_a_new_binding_generation_requires_renewed_owner_consent(monkeypatch):
    consents = [_consent("consent-threat", binding_id="binding-threat"), _consent("consent-code", binding_id="binding-code")]
    environment = _environment(monkeypatch, consents=consents, generation=5)
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(Namespace(db=environment.db, actor=environment.actor), _threat_tick())
    assert (denied.value.status_code, denied.value.detail["code"]) == (403, "schedule_consent_required")


@pytest.mark.asyncio
async def test_an_added_pinned_source_requires_renewed_owner_consent(monkeypatch):
    settings = _settings(
        source_refs=[deepcopy(DOCUMENT), deepcopy(COMMIT), deepcopy(SECOND_DOCUMENT)],
        triggers=[{**deepcopy(THREAT), "source_refs": [deepcopy(DOCUMENT), deepcopy(SECOND_DOCUMENT)]}, deepcopy(REVIEW)],
    )
    consents = [_consent("consent-threat", binding_id="binding-threat"), _consent("consent-code", binding_id="binding-code")]
    environment = _environment(monkeypatch, consents=consents, settings=settings)
    receipt = _receipt("nightly-threat-model", capability="threat_model", binding_id="binding-threat", sources=[DOCUMENT, SECOND_DOCUMENT])
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(Namespace(db=environment.db, actor=environment.actor), receipt)
    assert (denied.value.status_code, denied.value.detail["code"]) == (403, "schedule_consent_required")


@pytest.mark.asyncio
async def test_a_tick_frozen_at_a_superseded_configuration_revision_is_refused(monkeypatch):
    environment = _environment(monkeypatch, consents=[_consent("consent-threat", binding_id="binding-threat")], revision=2)
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(Namespace(db=environment.db, actor=environment.actor), _threat_tick())
    assert (denied.value.status_code, denied.value.detail["code"]) == (404, "not_found")


@pytest.mark.asyncio
async def test_a_replayed_receipt_identity_must_match_its_retained_semantic_key(monkeypatch):
    environment = _environment(monkeypatch, consents=[_consent("consent-threat", binding_id="binding-threat")])
    backend = Namespace(db=environment.db, actor=environment.actor)
    replayed = _threat_tick()
    replayed.event_id = "schedule:2026-10-31T03:30:00Z"
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(backend, replayed)
    assert (denied.value.status_code, denied.value.detail["code"]) == (404, "not_found")
    rejected = _threat_tick()
    rejected.state = "rejected"
    with pytest.raises(HTTPException) as refused:
        await l5_security_schedule.current_selection(backend, rejected)
    assert refused.value.status_code == 404


@pytest.mark.asyncio
async def test_a_disabled_trigger_is_not_a_consent_problem(monkeypatch):
    settings = _settings(triggers=[{**deepcopy(THREAT), "enabled": False}, deepcopy(REVIEW)])
    environment = _environment(monkeypatch, consents=[_consent("consent-threat", binding_id="binding-threat")], settings=settings)
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule.current_selection(Namespace(db=environment.db, actor=environment.actor), _threat_tick())
    assert (denied.value.status_code, denied.value.detail["code"]) == (409, "configuration_disabled")


@pytest.mark.asyncio
async def test_code_scan_refresh_observes_the_space_without_any_threat_model_call(monkeypatch):
    """A code-review tick must have no provider threat-model effect at all."""
    client = RecordingSecurityAgent()
    _install_provider(monkeypatch, client)
    binding = _binding("binding-code")
    database = FlushOnly()
    registration, model = await l5_security_schedule._refresh_native_rights(
        Namespace(db=database),
        binding,
        _credential(),
        _consent("consent-code", binding_id="binding-code"),
        {"connection_ref": deepcopy(CONNECTION)},
        "code_review",
        "audit-code",
    )
    assert (registration, model) == (REGISTRATION, None)
    assert client.names == ["batch_get_agent_spaces"]
    assert binding.readiness[0]["kind"] == "code_review_source_registration"
    assert binding.readiness[0]["registration"] == REGISTRATION
    # The owner's selected model is retained untouched by a code-scan refresh.
    assert binding.readiness[1] == STORED_MODEL and database.flushes == 1


@pytest.mark.asyncio
async def test_threat_model_refresh_matches_the_owner_selection_and_creates_nothing(monkeypatch):
    client = RecordingSecurityAgent()
    _install_provider(monkeypatch, client)
    binding = _binding("binding-threat")
    registration, model = await l5_security_schedule._refresh_native_rights(
        Namespace(db=FlushOnly()),
        binding,
        _credential(),
        _consent("consent-threat", binding_id="binding-threat"),
        {"connection_ref": deepcopy(CONNECTION)},
        "threat_model",
        "audit-threat",
    )
    assert (registration, model) == (REGISTRATION, MODEL)
    assert client.names == ["batch_get_agent_spaces", "list_threat_models", "batch_get_threat_models"]
    assert binding.readiness[1]["native_model_ref"] == MODEL


@pytest.mark.asyncio
async def test_a_threat_model_tick_without_a_selected_model_denies_before_any_provider_call(monkeypatch):
    client = RecordingSecurityAgent()
    _install_provider(monkeypatch, client)
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule._refresh_native_rights(
            Namespace(db=FlushOnly()),
            _binding("binding-threat", readiness=[]),
            _credential(),
            _consent("consent-threat", binding_id="binding-threat"),
            {"connection_ref": deepcopy(CONNECTION)},
            "threat_model",
            "audit-unselected",
        )
    assert denied.value.detail["code"] == "security_model_selection_required"
    assert client.calls == []


@pytest.mark.asyncio
async def test_a_missing_selected_model_is_denied_without_creating_a_replacement(monkeypatch):
    client = RecordingSecurityAgent(models=(("model-two", MODEL_CREATED),))
    _install_provider(monkeypatch, client)
    binding = _binding("binding-threat")
    with pytest.raises(HTTPException) as denied:
        await l5_security_schedule._refresh_native_rights(
            Namespace(db=FlushOnly()),
            binding,
            _credential(),
            _consent("consent-threat", binding_id="binding-threat"),
            {"connection_ref": deepcopy(CONNECTION)},
            "threat_model",
            "audit-missing",
        )
    assert (denied.value.status_code, denied.value.detail["code"]) == (409, "native_unqualified")
    assert "create_threat_model" not in client.names
