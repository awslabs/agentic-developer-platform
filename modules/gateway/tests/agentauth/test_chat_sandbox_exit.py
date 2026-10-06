"""Supervisor exit signal requires the same protected run and pod binding as admission."""

import json
import os

import pytest

from src.agentauth.bootstrap import envelope_digest
from tests.agentauth.test_chat_data_routes import SUPERVISOR_ROLE, admit, document
from tests.agentauth.test_work_producer import proof

pytest_plugins = ("tests.agentauth.test_chat_data_routes",)
EXIT = "/internal/v1/agent/chat/data/exit"


def supervisor(sts, monkeypatch):
    bindings = json.loads(os.environ["ADP_MODEL_ROOT_BINDINGS"])
    bindings[0]["chat_supervisor_role"] = SUPERVISOR_ROLE
    monkeypatch.setenv("ADP_MODEL_ROOT_BINDINGS", json.dumps(bindings))
    sts["role"] = "chat-supervisor"


async def check(client, body, digest=None):
    return await client.post(EXIT, json=body, headers={"X-Adp-Producer-Proof": proof(digest or envelope_digest(body))})


async def test_supervisor_observes_only_bound_terminal_pod(client, runtime, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    assert (await admit(client, runtime)).status_code == 200
    observed = []

    def exited(**kwargs):
        observed.append(kwargs)
        return len(observed) > 1

    monkeypatch.setattr(runtime[1].workloads, "has_exited", exited)
    body = document(runtime)
    pending = await check(client, body)
    assert pending.status_code == 200, pending.text
    assert pending.json() == {"run_id": "run-a", "pod_uid": body["pod_uid"], "terminated": False}
    complete = await check(client, body)
    assert complete.json() == {"run_id": "run-a", "pod_uid": body["pod_uid"], "terminated": True}
    assert observed == [{"name": body["pod_name"], "uid": body["pod_uid"], "image_digest": runtime[5].image_digest}] * 2
    assert complete.headers["cache-control"] == "no-store"


async def test_forged_proof_or_foreign_pod_cannot_observe_exit(client, runtime, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    assert (await admit(client, runtime)).status_code == 200
    observed = []
    monkeypatch.setattr(runtime[1].workloads, "has_exited", lambda **kwargs: observed.append(kwargs) or True)
    body = document(runtime)
    assert (await check(client, {**body, "pod_uid": "other-uid"}, envelope_digest(body))).status_code == 403
    assert (await check(client, {**body, "pod_uid": "other-uid"})).status_code == 404
    assert (await check(client, {**body, "pod_name": "other-pod"})).status_code == 404
    assert (await check(client, {**body, "envelope_digest": "f" * 64})).status_code == 404
    sts["role"] = "worker"
    assert (await check(client, body)).status_code == 403
    assert not observed


async def test_status_requires_supervisor_binding_and_enabled_authority(client, runtime, sts, monkeypatch):
    assert (await admit(client, runtime)).status_code == 200
    body = document(runtime)
    assert (await check(client, body)).status_code == 403
    supervisor(sts, monkeypatch)
    monkeypatch.delenv("ADP_CHAT_DATA_ENABLED")
    assert (await check(client, body)).status_code == 503


@pytest.mark.parametrize("proof_value", [None, "not-a-signed-proof"])
async def test_exit_requires_supervisor_proof_before_observing_pod(client, runtime, sts, monkeypatch, proof_value):
    supervisor(sts, monkeypatch)
    assert (await admit(client, runtime)).status_code == 200
    observed = []
    monkeypatch.setattr(runtime[1].workloads, "has_exited", lambda **kwargs: observed.append(kwargs) or True)
    headers = {} if proof_value is None else {"X-Adp-Producer-Proof": proof_value}
    prior_requests = len(sts["requests"])
    result = await client.post(EXIT, json=document(runtime), headers=headers)
    assert result.status_code == 403
    assert len(sts["requests"]) == prior_requests
    assert not observed


async def test_exit_rejects_invalid_supervisor_signature_before_observing_pod(client, runtime, sts, monkeypatch):
    supervisor(sts, monkeypatch)
    assert (await admit(client, runtime)).status_code == 200
    observed = []
    monkeypatch.setattr(runtime[1].workloads, "has_exited", lambda **kwargs: observed.append(kwargs) or True)
    sts["status"] = 403
    prior_requests = len(sts["requests"])
    result = await check(client, document(runtime))
    assert result.status_code == 403
    assert len(sts["requests"]) == prior_requests + 1
    assert not observed
