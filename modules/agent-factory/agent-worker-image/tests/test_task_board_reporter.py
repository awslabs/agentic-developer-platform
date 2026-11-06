import json
import hashlib
import copy
import sys
from pathlib import Path
from unittest.mock import Mock
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from lib import run_report
from lib.task_board_reporter import TaskBoardReporter


def event():
    return {"version": 1, "invocation_id": "run", "generation": 1, "sequence": 2,
            "timestamp": "2026-10-30T10:00:00Z", "kind": "explanation",
            "payload": {"text": "- ☐ Check recovery", "progress": {"category": "plan"}}}


def test_flushes_same_live_board_on_exit_and_retries_only_unacknowledged_updates(tmp_path, monkeypatch):
    path = tmp_path / "board.json"
    send = Mock(side_effect=[run_report.RunReportError("offline"), {"recorded": True}])
    monkeypatch.setattr(run_report, "request", send)
    monkeypatch.setattr(run_report, "enabled", lambda: True)
    with TaskBoardReporter(path, "run", interval=60) as reporter:
        path.write_text(json.dumps(event()))
        reporter.publish()
    assert send.call_count == 2
    assert send.call_args.args[1]["text"] == event()["payload"]["text"]
    reporter.publish()
    assert send.call_count == 2


def test_rejects_foreign_malformed_and_non_plan_snapshots(tmp_path, monkeypatch):
    path = tmp_path / "board.json"
    send = Mock()
    monkeypatch.setattr(run_report, "request", send)
    reporter = TaskBoardReporter(path, "run")
    for value in [[], {**event(), "invocation_id": "other"}, {**event(), "payload": []}, {**event(), "kind": "terminal"}]:
        path.write_text(json.dumps(value))
        reporter.publish()
    send.assert_not_called()


def test_optional_reporting_failure_does_not_replace_the_agent_outcome(tmp_path, monkeypatch):
    path = tmp_path / "board.json"
    path.write_text(json.dumps(event()))
    monkeypatch.setattr(run_report, "enabled", lambda: True)
    monkeypatch.setattr(run_report, "request", Mock(side_effect=RuntimeError("credential provider unavailable")))
    with TaskBoardReporter(path, "run", interval=60):
        pass


def checkpoint(path, status="done", run_id="run"):
    value = {"version": 1, "invocation_id": run_id, "sequence": 1, "parent_revision": None,
        "story_checkpoint": {"scope": {"run_id": run_id}, "delta": {"updates": {"C1": {"status": status}}}}}
    Path(str(path) + ".checkpoint").write_text(json.dumps(value))
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def test_lost_ack_retries_identical_cas_before_coalescing_newer_private_work(tmp_path, monkeypatch):
    path = tmp_path / "board.json"
    path.write_text(json.dumps(event()))
    first = checkpoint(path)
    calls = []

    def request(_, body, **kwargs):
        calls.append(copy.deepcopy(body))
        if len(calls) == 1:
            raise run_report.RunReportError("lost acknowledgement")
        return {"recorded": True, "checkpoint_revision": digest(body["story_checkpoint"])}

    monkeypatch.setattr(run_report, "request", request)
    reporter = TaskBoardReporter(path, "run")
    assert reporter.publish() is False
    second = checkpoint(path, "open")
    assert reporter.publish() is True
    assert calls[0] == calls[1]
    assert calls[2]["expected_checkpoint_revision"] == digest(first["story_checkpoint"])
    assert calls[2]["story_checkpoint"] == second["story_checkpoint"]
    assert reporter.publish() is True and len(calls) == 3
    assert json.loads(path.read_text()) == event()  # Never inject private deltas into the public file.
    assert json.loads(Path(str(path) + ".checkpoint-receipt").read_text())["code"] == "story_checkpoint_acknowledged"


def test_private_checkpoint_persists_without_a_public_render(tmp_path, monkeypatch):
    path = tmp_path / "board.json"
    value = checkpoint(path)
    request = Mock(return_value={"recorded": True, "checkpoint_revision": digest(value["story_checkpoint"])})
    monkeypatch.setattr(run_report, "request", request)
    reporter = TaskBoardReporter(path, "run")
    assert reporter.publish() is True
    assert reporter.publish() is True
    assert request.call_count == 1
    assert "partial completion total" in request.call_args.args[1]["text"]


@pytest.mark.parametrize("display", ["not JSON", "x" * 32769, "[]", '{"payload": []}'])
def test_invalid_public_render_does_not_prevent_valid_private_checkpoint(tmp_path, monkeypatch, display):
    path = tmp_path / "board.json"
    path.write_text(display)
    value = checkpoint(path)
    request = Mock(return_value={"recorded": True, "checkpoint_revision": digest(value["story_checkpoint"])})
    monkeypatch.setattr(run_report, "request", request)
    assert TaskBoardReporter(path, "run").publish() is True
    assert request.call_args.args[1]["story_checkpoint"] == value["story_checkpoint"]


def test_foreign_private_snapshot_and_oversize_are_never_transported(tmp_path, monkeypatch):
    path = tmp_path / "board.json"
    request = Mock()
    monkeypatch.setattr(run_report, "request", request)
    reporter = TaskBoardReporter(path, "run")
    checkpoint(path, run_id="other")
    assert reporter.publish() is False
    Path(str(path) + ".checkpoint").write_text("x" * (128 * 1024 + 1))
    assert reporter.publish() is False
    request.assert_not_called()


def test_final_flush_and_wrong_digest_are_explicit_without_raw_failure_text(tmp_path, monkeypatch, caplog):
    path = tmp_path / "board.json"
    checkpoint(path)
    monkeypatch.setattr(run_report, "enabled", lambda: True)
    monkeypatch.setattr(run_report, "request", Mock(return_value={"recorded": True, "checkpoint_revision": "wrong"}))
    with TaskBoardReporter(path, "run", interval=60):
        pass
    assert "story_checkpoint_final_flush_failed" in caplog.text
    assert "wrong" not in caplog.text
    assert json.loads(Path(str(path) + ".checkpoint-receipt").read_text())["checkpoint_revision"] is None


@pytest.mark.parametrize("field,value", [("generation", 0), ("generation", True), ("sequence", "1"),
    ("timestamp", "invalid"), ("timestamp", "2026-10-31T10:00:00"), ("text", ""), ("text", "x" * 16385)])
def test_invalid_display_contract_falls_back_and_does_not_poison_pending(tmp_path, monkeypatch, field, value):
    path = tmp_path / "board.json"
    display = event()
    if field == "text":
        display["payload"][field] = value
    else:
        display[field] = value
    path.write_text(json.dumps(display))
    private = checkpoint(path)
    calls = []
    def request(_, body, **kwargs):
        calls.append(copy.deepcopy(body))
        return {"recorded": True, "checkpoint_revision": digest(body["story_checkpoint"])}
    monkeypatch.setattr(run_report, "request", request)
    reporter = TaskBoardReporter(path, "run")
    assert reporter.publish()
    assert "no partial completion total" in calls[0]["text"]
    assert calls[0]["story_checkpoint"] == private["story_checkpoint"]
    newer = checkpoint(path, "open")
    assert reporter.publish()
    assert calls[-1]["story_checkpoint"] == newer["story_checkpoint"]


@pytest.mark.parametrize("sequence", [0, True, "1"])
def test_invalid_private_sequence_never_becomes_pending(tmp_path, monkeypatch, sequence):
    path = tmp_path / "board.json"
    value = checkpoint(path)
    value["sequence"] = sequence
    Path(str(path) + ".checkpoint").write_text(json.dumps(value))
    request = Mock()
    monkeypatch.setattr(run_report, "request", request)
    reporter = TaskBoardReporter(path, "run")
    assert not reporter.publish()
    assert reporter._pending is None
    request.assert_not_called()
