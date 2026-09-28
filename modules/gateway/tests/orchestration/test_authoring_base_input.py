"""The real dispatch transports the accepted document to the real worker reader."""

import copy
import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import pytest

from src.orchestration import authoring_input
from src.orchestration.authoring_input import AuthoringInputError, resolve_authoring_input
from src.orchestration.engine_commands import EngineCommandReport
from src.orchestration.pending_amendments import _snapshot, in_force_plan
from src.orchestration.registration import promote_proposed_policy, transform_for_registration

from . import test_replan_authoring_dispatch as dispatch_tests
from .test_pending_amendments import ORG_A, base_proposal
from .test_replan_authoring_dispatch import flow_with_asker, replan, requests

session = dispatch_tests.session

WORKER = Path(__file__).resolve().parents[4] / "modules/agent-factory/agent-worker-image/lib/amendment_input.py"
spec = importlib.util.spec_from_file_location("worker_amendment_input", WORKER)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


async def commissioned(session, proposal=None):
    proposal, _ = transform_for_registration(proposal or base_proposal())
    # This fixture performs explicit human acceptance, rather than inert draft
    # registration. Carry the proposed bounds into that acceptance unchanged.
    proposal = promote_proposed_policy(proposal)
    flow = await flow_with_asker(session, proposal=proposal)
    report = EngineCommandReport()
    await replan(session, report, flow_id=flow)
    row = (await requests(session))[0]
    return _snapshot(row, created=False), report.pending_authoring[0].envelope


async def test_policy_bearing_snapshot_can_be_authored_registered_and_accepted(session, tmp_path):
    from src.orchestration.execution_policy import _STAMPED_FIELDS
    from src.orchestration.pending_amendments import accept_amendment, register_amendment_draft
    from src.orchestration.proposal import LoopProposal

    from .test_execution_policy_acceptance import a_policy
    from .test_pending_amendments import amender

    policy = a_policy(org_id=ORG_A, evaluation_acceptance={})
    request, envelope = await commissioned(session, base_proposal(execution_policy=policy))
    original = copy.deepcopy(envelope["payload"]["amendment_base"]["document"])
    assert worker.POLICY_SERVER_FIELDS == _STAMPED_FIELDS
    assert all(original["execution_policy"][name] for name in _STAMPED_FIELDS)
    path = worker.materialize_authoring_input(json.loads(json.dumps(envelope)), directory=str(tmp_path))
    authored = json.loads(Path(path).read_text())
    assert not _STAMPED_FIELDS.intersection(authored["execution_policy"])
    assert authored["execution_policy"] == {k: v for k, v in original["execution_policy"].items() if k not in _STAMPED_FIELDS}
    assert authored["nodes"] == original["nodes"]
    assert envelope["payload"]["amendment_base"]["document"] == original
    authored["description"] = "The requested clarification"
    draft = await register_amendment_draft(
        session, org_id=ORG_A, request=request, author_run_id=request.author_run_id, proposal=LoopProposal.model_validate(authored)
    )
    await session.commit()
    result = await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=request.flow_id)
    await session.commit()
    accepted = await in_force_plan(session, org_id=ORG_A, flow_id=request.flow_id)
    assert result.plan_version == 2
    assert accepted.plan_document["execution_policy"]["principal_id"] == amender().actor_id
    assert accepted.plan_document["execution_policy"]["policy_id"]


async def test_server_to_serialized_envelope_to_real_worker_preserves_synthesized_gates(session, tmp_path):
    request, envelope = await commissioned(session)
    accepted = await in_force_plan(session, org_id=ORG_A, flow_id=request.flow_id)
    path = worker.materialize_authoring_input(json.loads(json.dumps(envelope)), directory=str(tmp_path))
    materialized = json.loads(Path(path).read_text())
    assert materialized == accepted.plan_document
    original_addresses = {node.address for node in base_proposal().nodes}
    synthesized_gates = {node["address"] for node in materialized["nodes"] if node["kind"] == "gate"} - original_addresses
    assert synthesized_gates, "exercise the accepted gate that is absent from the original proposal"
    assert Path(path).stat().st_mode & 0o777 == 0o400
    assert authoring_input.MAX_BASE_DOCUMENT_BYTES == worker.MAX_BASE_DOCUMENT_BYTES


@pytest.mark.parametrize(
    "field,value",
    [
        ("flow_id", "other-flow"),
        ("id", "other-request"),
        ("author_run_id", "other-run"),
        ("base_plan_version", 99),
        ("base_plan_hash", "0" * 64),
        ("requested_by", "other-person"),
    ],
)
async def test_server_refuses_a_snapshot_that_disagrees_with_the_request_row(session, field, value):
    request, _ = await commissioned(session)
    with pytest.raises(AuthoringInputError, match="authoring_input_binding_mismatch"):
        await resolve_authoring_input(session, org_id=ORG_A, request=replace(request, **{field: value}), author_run_id=request.author_run_id)


async def test_server_does_not_resolve_another_tenants_input(session):
    request, _ = await commissioned(session)
    with pytest.raises(AuthoringInputError, match="authoring_input_binding_mismatch"):
        await resolve_authoring_input(session, org_id="other-tenant", request=request, author_run_id=request.author_run_id)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("version", "base_stale"),
        ("content", "base_hash_mismatch"),
        ("oversize", "base_too_large"),
    ],
)
async def test_server_refuses_stale_corrupt_or_untransportable_input(session, monkeypatch, change, reason):
    request, _ = await commissioned(session)
    accepted = await in_force_plan(session, org_id=ORG_A, flow_id=request.flow_id)
    if change == "version":
        accepted.version += 1
    elif change == "content":
        accepted.plan_document = {**accepted.plan_document, "title": "changed after acceptance"}
    else:
        monkeypatch.setattr(authoring_input, "MAX_BASE_DOCUMENT_BYTES", 16)
    await session.flush()
    with pytest.raises(AuthoringInputError, match="authoring_input_" + reason):
        await resolve_authoring_input(session, org_id=ORG_A, request=request, author_run_id=request.author_run_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "other-tenant"),
        ("flow_id", "other-flow"),
        ("request_id", "other-request"),
        ("author_run_id", "other-run"),
        ("base_plan_version", True),
        ("base_plan_version", 99),
        ("base_plan_hash", "0" * 64),
        ("version", True),
    ],
)
async def test_real_worker_refuses_rebound_input(session, tmp_path, field, value):
    _, envelope = await commissioned(session)
    envelope["payload"]["amendment_base"][field] = value
    with pytest.raises(worker.AuthoringInputError):
        worker.materialize_authoring_input(envelope, directory=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("change", ["missing", "corrupt", "oversize"])
async def test_real_worker_refuses_missing_corrupt_and_oversize_documents(session, tmp_path, change):
    _, envelope = await commissioned(session)
    envelope = copy.deepcopy(envelope)
    if change == "missing":
        del envelope["payload"]["amendment_base"]
    else:
        envelope["payload"]["amendment_base"]["document"]["description"] = "x" * (worker.MAX_BASE_DOCUMENT_BYTES if change == "oversize" else 1)
    with pytest.raises(worker.AuthoringInputError):
        worker.materialize_authoring_input(envelope, directory=str(tmp_path))
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["allocate", "write"])
async def test_real_worker_reports_filesystem_failures_without_partial_input(session, tmp_path, monkeypatch, failure):
    _, envelope = await commissioned(session)

    def refuse(*args, **kwargs):
        raise OSError("private filesystem detail")

    if failure == "allocate":
        monkeypatch.setattr(worker.tempfile, "mkdtemp", refuse)
    else:
        monkeypatch.setattr(worker.os, "chmod", refuse)
    with pytest.raises(worker.AuthoringInputError, match="^authoring_input_materialization_failed$"):
        worker.materialize_authoring_input(envelope, directory=str(tmp_path))
    assert list(tmp_path.iterdir()) == []
