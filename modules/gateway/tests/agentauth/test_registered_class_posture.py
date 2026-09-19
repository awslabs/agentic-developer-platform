"""Missing snapshot material is never permission to skip live enforcement.

The regression behind operator finding P1. An earlier revision established the
live posture *from the proposal snapshot*, so a run whose snapshot was absent,
unparseable or unbound produced ``posture=None`` — and that had to be admitted by
a reason-code exception (``snapshot_missing`` / ``snapshot_binding_missing``) to
keep ordinary bootstrap working. Keying admission on the reason meant a run under
a committed **enforcing** posture was admitted with a full run credential purely
because its snapshot was missing: a complete enforcement bypass, reachable by any
condition that prevents a snapshot from being written.

The repair is ordering, not an exception. The posture is a property of the
compatibility class a persona is registered to, which the gateway knows from its
own persona registry without any snapshot at all. Establishing it first means:

* a failed or absent proposal keeps the genuinely established posture, so
  ``report_only``/``disabled`` preserve exact legacy admission — the thing the
  bypass was trying to protect, now done truthfully;
* an ``enforcing`` posture still refuses that same run, because an unavailable
  proposal under enforcement must stop the run rather than fall through to a
  legacy model;
* a genuinely absent or unreadable settings row still refuses, because unknown is
  not permissive.

Nothing here is mocked except the AWS and Kubernetes transports: the route, the
policy evaluator, the committed-posture reader and the credential issuer are all
the shipping code. ``enforcing`` is exercised in source only — every configured
environment stays ``report_only`` and no model is invoked.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.routes import MODEL_POLICY_CONTRACT_VERSION
from src.agentauth.runtime_posture import reset_posture_cache
from src.agentauth.workload import WORKLOAD_HEADER
from src.shared.models.base import Base
from src.shared.models.persona_models import PersonaModelPolicySetting
from tests.agentauth.test_bootstrap_routes import (  # noqa: F401 - re-exported fixtures
    http_client,
    kubernetes,
    provision,
    store,
)

HEADERS = {"X-Caller-Identity": "registered-worker-transport", WORKLOAD_HEADER: "pod-token"}


def _bootstrap(client, envelope, *, contract=MODEL_POLICY_CONTRACT_VERSION):
    body = {"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)}
    if contract is not None:
        body["model_policy_contract"] = contract
    return client.post("/internal/v1/agent/bootstrap", json=body, headers=HEADERS)


def _strip_snapshot(store, *, shape):  # noqa: F811 - re-exported fixture name, see the import above
    """Reproduce the two snapshot failures on a real protected execution.

    ``missing_binding`` deliberately leaves snapshot **and** digest present while
    removing the policy revision/correlation/root binding. That case is why the
    removed bypass was unsound on its own terms: its documentation claimed
    ``snapshot_binding_missing`` could not occur on a run that had a snapshot,
    and it can.
    """
    key = {"pk": {"S": "TENANT#tenant"}, "sk": {"S": "EXEC#run-a"}}
    if shape == "absent":
        store.client.update_item(
            TableName=store.table,
            Key=key,
            UpdateExpression="REMOVE model_policy_snapshot, model_policy_snapshot_digest",
        )
        return
    store.client.update_item(
        TableName=store.table,
        Key=key,
        UpdateExpression=(
            "SET model_policy_snapshot = :snapshot, model_policy_snapshot_digest = :digest "
            "REMOVE model_policy_revision, model_policy_correlation_id, model_policy_root_invocation_id"
        ),
        ExpressionAttributeValues={":snapshot": {"S": "{}"}, ":digest": {"S": "a" * 64}},
    )


@pytest.mark.parametrize("shape", ["absent", "missing_binding"])
@pytest.mark.parametrize("posture", ["report_only", "disabled"])
def test_a_permissive_committed_posture_admits_a_failed_proposal(store, kubernetes, monkeypatch, posture, shape):  # noqa: F811 - re-exported fixtures, see the import above
    """The live configuration: legacy admission, with the posture reported truthfully.

    The proposal genuinely fails, and the run is still admitted — but on the
    strength of a *verified committed* posture read from the registered class, not
    because a reason code was allowlisted. ``posture_verified`` is what a worker
    keys its behaviour on, so reporting it truthfully here is what lets the worker
    preserve legacy assignment without consulting its own editable config.
    """
    client, _ = http_client(store, kubernetes, monkeypatch, posture=posture, posture_revision=11)
    envelope, _ = provision(store)
    _strip_snapshot(store, shape=shape)

    response = _bootstrap(client, envelope)

    assert response.status_code == 200, response.text
    assert response.json()["credential"].startswith("adpr1.")
    policy = response.json()["model_policy"]
    assert policy["status"] == "unavailable"
    assert policy["posture"] == posture
    assert policy["posture_verified"] is True
    assert policy["posture_revision"] == 11
    # The proposal failure is reported as itself, not laundered into a success.
    assert policy["reason"] in {"snapshot_missing", "snapshot_binding_missing"}


@pytest.mark.parametrize("shape", ["absent", "missing_binding"])
def test_an_enforcing_committed_posture_refuses_a_failed_proposal(store, kubernetes, monkeypatch, shape):  # noqa: F811 - re-exported fixtures, see the import above
    """The bypass, pinned shut.

    Identical to the permissive test except for the committed posture. Before the
    repair these returned 200 with a usable run credential; under enforcement a
    proposal that could not be built must stop the run.
    """
    client, _ = http_client(store, kubernetes, monkeypatch, posture="enforcing", posture_revision=11)
    envelope, _ = provision(store)
    _strip_snapshot(store, shape=shape)

    response = _bootstrap(client, envelope)

    assert response.status_code == 409, response.text
    assert "credential" not in response.json()


@pytest.mark.parametrize("shape", ["absent", "missing_binding"])
def test_no_committed_posture_row_refuses_rather_than_defaulting(store, kubernetes, monkeypatch, shape):  # noqa: F811 - re-exported fixtures, see the import above
    """Unknown is not permissive, and a missing row is unknown.

    Provisioning nothing is the deliberately fail-closed case. It must not be
    rescued by reclassifying an absent row as ``report_only``, nor by consulting
    the ``agent_models`` UI flag — that flag gates catalogue editing and is not a
    statement about runtime posture.
    """
    client, _ = http_client(store, kubernetes, monkeypatch, posture=None)
    envelope, _ = provision(store)
    _strip_snapshot(store, shape=shape)

    response = _bootstrap(client, envelope)

    assert response.status_code == 409, response.text
    assert "credential" not in response.json()


def test_the_posture_is_read_for_the_personas_registered_class(store, kubernetes, monkeypatch):  # noqa: F811 - re-exported fixtures, see the import above
    """A posture committed for a *different* class does not govern this run.

    Establishing the class from the registry must mean the registry's answer, not
    "whatever row happens to exist". The developer persona is registered to
    ``claude-agent-sdk``; an enforcing row provisioned for another class leaves
    this run with no posture of its own, which is a refusal rather than an
    inherited one.
    """
    client, _ = http_client(store, kubernetes, monkeypatch, posture=None)
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)

    async def _seed_other_class():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_seed_other_class())
    reset_posture_cache()
    envelope, _ = provision(store)
    _strip_snapshot(store, shape="absent")

    response = _bootstrap(client, envelope)

    assert response.status_code == 409, response.text
    assert "credential" not in response.json()


@pytest.mark.parametrize("posture", ["report_only", "disabled"])
def test_an_audited_rollback_is_observed_without_a_new_snapshot(store, kubernetes, monkeypatch, posture):  # noqa: F811 - re-exported fixtures, see the import above
    """§9: the committed setting decides, so a rollback takes effect immediately.

    The execution is untouched between the two bootstraps — same snapshot state,
    same envelope. Only the committed posture changes. That is the property a
    frozen root snapshot must not be able to override: a chain captured under
    enforcing stops enforcing once an operator has rolled the setting back.
    """
    client, _ = http_client(store, kubernetes, monkeypatch, posture="enforcing", posture_revision=11)
    envelope, _ = provision(store)
    _strip_snapshot(store, shape="absent")

    assert _bootstrap(client, envelope).status_code == 409

    async def _rollback():
        async with client.posture_sessions() as operator:
            await operator.execute(
                update(PersonaModelPolicySetting).values(enforcement_posture=posture, posture_revision=12)
            )
            await operator.commit()

    asyncio.run(_rollback())
    # The measured cache is what bounds how long a stale posture can be served;
    # clearing it here stands in for that bound elapsing, so the assertion is
    # about the committed value being authoritative rather than about timing.
    reset_posture_cache()

    response = _bootstrap(client, envelope)

    assert response.status_code == 200, response.text
    policy = response.json()["model_policy"]
    assert (policy["posture"], policy["posture_revision"]) == (posture, 12)
    assert policy["posture_verified"] is True
