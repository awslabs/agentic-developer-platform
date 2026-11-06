"""Offline composition of original provider and installed executor observations."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/L5-services/scripts"))
sys.path.insert(0, str(ROOT / "modules/L5-services/tests/transform"))

from qualification_transform_acceptance import EvidenceSet, catalog, evaluate, validate_catalog
from qualification_transform_collectors import NativeResults
from qualification_transform_journal import ReleaseJournal, digest
from qualification_transform_provider import InstalledProvider, current_isolation_receipt, qualification_pins
import qualification_transform_provider as provider
from l5_services.adapters.transform.ndjson import TransformConversation
from l5_services.adapters.transform.provider_job import NDJSON_BINARY_SHA256, NDJSON_INTERFACE
from test_provider_job import admitted
from test_transform_isolation_collection import CONTEXT, TARGET
from l5_services.adapters.transform import qualification_release as native
import test_qualification_release as original

observations = original.observations
published = original.published
assembled = original.assembled


NODE = {"node_uid": TARGET["node_uid"], "node_labels": {"adp.dev/transform-isolation": "v1"},
        "node_spec": {}, "policy_uid": "policy-a", "policy_spec": {}}


def original_case(evidence, cases, tmp_path, index, *, missing=None):
    run, _, attempt = admitted()
    run.admission = deepcopy(run.admission)
    run.run_id = "run-" + str(index)
    run.admission["scope"] = {"tenant_id": "tenant-a", "product_id": "product-a", "project_id": "project-" + str(index)}
    qualification = run.admission["transform_qualification"]
    qualification.update(interface=NDJSON_INTERFACE, cli_version="3.19.0", binary_sha256=NDJSON_BINARY_SHA256,
                         image_digest=TARGET["images"]["provider"].split("@", 1)[1])
    run.admission["transform_test_policy"].update(image_digest=TARGET["images"]["check"].split("@", 1)[1], test_policy_ref={"id": "pinned"})
    run.admission["qualified_executor_ref"] = deepcopy(run.admission["transform_settings"]["executor_binding_ref"])
    native_id = "native-" + str(index)
    result = {"result_id": "result-" + str(index), "revision": 1}
    case = {"capability": "refactor" if index == 0 else "dependency_upgrade", "run_id": run.run_id,
        "scope": run.admission["scope"], "result_sha256": digest(result), "attempt_id": attempt.attempt_id,
        "manifest_sha256": attempt.input_hash, "native_id": native_id}
    cases.cases.append(case)
    workspace = tmp_path / ("source-" + str(index))
    workspace.mkdir()
    conversation = TransformConversation({"attempt_token": attempt.idempotency_token, "input_hash": attempt.input_hash,
        "read_paths": ["src"], "write_paths": ["src/app.py"], "excluded_paths": [".git"]}, workspace=workspace, agent_minutes=10)
    def feed(**value): conversation.feed((json.dumps(value) + "\n").encode())
    feed(type="session_started", conversationId=native_id)
    feed(type="agent_minutes_update", used=0.1)
    feed(type="checkpoint", checkpointId=native_id + "#checkpoint-a")
    for i, (name, parameters) in enumerate([
        ("file_read", {"path": "src/app.py"}), ("file_write", {"path": "src/app.py"}),
        ("shell", {"command": "never-executed"}), ("file_read", {"path": "/proc/self/environ"}),
        ("file_write", {"path": "outside.py"}),
    ]):
        if missing == "tools" and i >= 2:
            continue
        feed(type="tool_permission_required", toolCallId=str(i), name=name, parameters=parameters)
    if missing != "resume":
        conversation.begin_continuation({"native_id": native_id, "checkpoint_id": native_id + "#checkpoint-a",
            "metadata_sha256": "c" * 64, "minutes_used": 0.1, "agent_minutes_limit": 10})
        feed(type="resume_status", conversationId=native_id, checkpointId=native_id + "#checkpoint-a")
    feed(type="agent_minutes_update", used=0.2)
    feed(type="complete", status="completed")
    observation = {} if missing == "trace" else {"capability_trace": conversation.capability_trace()}
    checkpoints = [{"kind": "provider", "checkpoint": {"observation": observation}}]
    calls = []
    async def verify_checks(**kwargs):
        calls.append("checkpoint-read")
        return checkpoints
    async def revalidate(): calls.append("authority-read")
    return {"run": run, "attempt": attempt, "result": result,
        "authority": SimpleNamespace(scope=run.admission["scope"], revalidate=revalidate, assert_current=lambda: None),
        "observer": SimpleNamespace(verify_checks=verify_checks), "intent": {}, "files": {}, "client": None, "sessions": None}, calls


def issued_isolation(evidence, target=TARGET, node=NODE):
    probes = []
    for kind in ("provider", "check"):
        uid = "isolation-" + kind
        output = evidence.journal.append("executor-isolation-output", {"kind": kind, "pod": {"metadata": {"uid": uid}}})
        probes.append({"kind": kind, "pod_uid": uid, "image": target["images"][kind], "output_receipt": output,
                       "observations": dict.fromkeys(provider.OBSERVATIONS, True), "owned_absence": True})
    reference = evidence.journal.append("executor-isolation-components", {
        "target_sha256": digest(target), "boundary_sha256": digest(node), "probes": probes,
        "remaining": ["other-EC02-controls"], "criterion_complete": False})
    evidence.progress.append({"kind": "executor-isolation-components", "outcome": "partial",
        "observed": sorted(provider.OBSERVATIONS), "collector_receipt": reference})


@pytest.fixture
def collection(tmp_path, monkeypatch):
    private = tmp_path / "evidence"
    private.mkdir(mode=0o700)
    journal = ReleaseJournal(private, CONTEXT, "d" * 64, catalog()[1])
    reads = []
    target = deepcopy(TARGET)
    node = deepcopy(NODE)
    def installed(*args):
        reads.append("installed-read")
        return SimpleNamespace(client=SimpleNamespace(close=lambda: reads.append("close"))), deepcopy(target)
    monkeypatch.setattr(provider, "installed_target", installed)
    monkeypatch.setattr(provider, "boundary", lambda *args: deepcopy(node))
    with journal.locked():
        evidence = EvidenceSet(journal)
        cases = NativeResults(evidence)
        installed = SimpleNamespace(series=SimpleNamespace(config={}, fixture=SimpleNamespace(), context=CONTEXT))
        collector = InstalledProvider(evidence, cases, installed, environment={})
        yield evidence, cases, collector, reads, target, node


@pytest.mark.asyncio
async def test_both_actual_case_collectors_can_issue_only_the_direct_check(collection, tmp_path):
    evidence, cases, collector, reads, _, _ = collection
    issued_isolation(evidence)
    for index in range(2):
        values, calls = original_case(evidence, cases, tmp_path, index)
        await collector.collect(**values)
        assert calls == ["authority-read", "checkpoint-read", "authority-read"]
    collector.finish()
    report = evaluate(evidence)
    validate_catalog(report)
    assert report["observed_criteria"] == ["provider-executor-qualified"]
    assert all(item["outcome"] == "incomplete" for item in report["epic_criteria"])
    assert reads.count("installed-read") == reads.count("close") == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["trace", "resume", "tools", "isolation", "node", "case", "installed"])
async def test_flags_or_incomplete_controls_cannot_establish_qualification(collection, tmp_path, missing):
    evidence, cases, collector, _, _, node = collection
    if missing != "isolation": issued_isolation(evidence)
    values, _ = original_case(evidence, cases, tmp_path, 0, missing=missing)
    if missing == "node": node["node_labels"] = {}
    elif missing == "case": cases.cases.clear()
    elif missing == "installed": collector.installed.series = None
    await collector.collect(**values)
    collector.finish()
    assert not evidence.verified()
    assert not collector.completed
    progress = evidence.progress[-1]
    assert progress["outcome"] in {"partial", "incomplete"} and progress["next_action"]
    if missing == "resume": assert progress["missing"] == ["original-session-continuation"]
    if missing == "tools": assert set(progress["missing"]) == {"denied_shell", "denied_source_read", "denied_source_write"}


def test_old_journal_record_without_current_verifier_execution_is_not_evidence(collection):
    evidence, _, _, _, _, _ = collection
    issued_isolation(evidence)
    assert current_isolation_receipt(evidence, TARGET, NODE)
    other = EvidenceSet(evidence.journal)
    with pytest.raises(ValueError): current_isolation_receipt(other, TARGET, NODE)


@pytest.mark.parametrize("fault", ["binary", "version", "provider-image", "check-image", "definition", "executor", "unqualified"])
def test_expected_qualification_flags_still_require_exact_installed_pins(collection, tmp_path, fault):
    evidence, cases, _, _, _, _ = collection
    values, _ = original_case(evidence, cases, tmp_path, 0)
    run = values["run"]
    qualification = run.admission["transform_qualification"]
    if fault == "binary": qualification["binary_sha256"] = "f" * 64
    elif fault == "version": qualification["cli_version"] = "different"
    elif fault == "provider-image": qualification["image_digest"] = "sha256:" + "f" * 64
    elif fault == "check-image": run.admission["transform_test_policy"]["image_digest"] = "sha256:" + "f" * 64
    elif fault == "definition": run.admission["transform_settings"]["definition_ref"]["record_revision"] = "different"
    elif fault == "executor": run.admission["qualified_executor_ref"] = {"id": "different"}
    else: qualification["executor_qualification"] = "unqualified"
    with pytest.raises(ValueError): qualification_pins(run, TARGET)


@pytest.mark.asyncio
async def test_authority_failure_closes_reader_and_does_not_disclose_provider_text(collection, tmp_path):
    evidence, cases, collector, reads, _, _ = collection
    issued_isolation(evidence)
    values, _ = original_case(evidence, cases, tmp_path, 0)
    async def rejected(**kwargs): raise ValueError("private-provider-diagnostics")
    values["observer"].verify_checks = rejected
    await collector.collect(**values)
    assert reads == ["installed-read", "close"]
    assert "private-provider-diagnostics" not in json.dumps(evidence.progress)
    assert not collector.completed


@pytest.mark.asyncio
async def test_real_original_result_and_checkpoint_readers_feed_collector_without_effects(collection, assembled, monkeypatch, tmp_path):
    evidence, cases, collector, _, _, _ = collection
    fixture, sessions, client, artifacts, result, _, s3, requests, _ = assembled
    # Synthetic fixture preparation; production collection only reads the
    # original run and checkpoint through the real independent evaluator.
    values, _ = original_case(evidence, cases, tmp_path, 0)
    prepared = await values["observer"].verify_checks()
    trace = prepared[0]["checkpoint"]["observation"]["capability_trace"]
    cases.cases.clear()
    async with sessions() as session:
        from sqlalchemy import select
        from l5_services.models.run import Run
        run = await session.scalar(select(Run))
        run.admission = {**run.admission, "limits": {"agent_minutes": 10}}
        selected_native = run.native_job_ref["native_id"]
        await session.commit()
    trace = json.loads(json.dumps(trace).replace("native-0", selected_native))
    for (key, _), saved in s3.objects.items():
        if "/checkpoints/" not in key:
            continue
        checkpoint = json.loads(saved["Body"])
        if "observation" in checkpoint:
            checkpoint["observation"]["capability_trace"] = trace
            saved["Body"] = json.dumps(checkpoint).encode()
            saved["Metadata"]["sha256"] = sha256(saved["Body"]).hexdigest()
    # Exact installed pin matching has separate negative tests above. This
    # fixture predates the pinned NDJSON binary and keeps its legacy selection.
    monkeypatch.setattr(provider, "qualification_pins", lambda *args: {"fixture": "legacy-pin-selection"})
    issued_isolation(evidence)
    cases.select(fixture["runs"][0])
    baseline = deepcopy(s3.objects)
    evaluated = await native.qualify(fixture, sessions, client, artifacts, clock=lambda: original.NOW,
                                    on_verified_case=cases.verified, on_retained_case=collector.collect)
    collector.finish()
    assert evaluated["status"] == "evaluated"
    assert len(collector.completed) == 1, evidence.progress
    assert not evidence.verified()  # One case is still incomplete.
    assert s3.objects == baseline
    assert requests and all(request.method == "GET" for request in requests)
