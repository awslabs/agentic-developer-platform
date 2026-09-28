import sys
import time
from functools import partial
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from isolated_browser import ProcessActor
from investigation_browser import InvestigationError, InvestigationManager
from browser_guard import DestinationRefused
import domain_investigation as cli
from research_case import verify_case


def actor(script, *, stop=lambda sid: True, **kwargs):
    return ProcessActor(
        {},
        None,
        lambda a: None,
        stop=stop,
        command=[sys.executable, "-u", "-c", script],
        **kwargs,
    )


def test_hung_start_is_killed_and_remote_cleanup_releases_capacity():
    stopped = []
    a = actor(
        """import json,time,sys
json.loads(sys.stdin.readline())
print(json.dumps({"event":"session","session_id":"test-session"}),flush=True)
time.sleep(90)
""",
        stop=lambda sid: stopped.append(sid) or True,
        startup_seconds=0.4,
    )
    before = time.monotonic()
    with pytest.raises(InvestigationError) as raised:
        a.initial()
    assert time.monotonic() - before < 4
    assert raised.value.code == "startup_timeout"
    assert raised.value.cleanup["cleanup_status"] == "stopped"
    assert a.ended.is_set() and a.process.poll() is not None
    assert stopped == ["test-session"]
    a.abort()
    assert stopped == ["test-session"]
    manager = InvestigationManager()
    manager.actors["old"] = a
    assert manager.capacity()["active_sessions"] == 0


def test_hung_action_is_not_replayed_and_cleanup_failure_remains_visible():
    a = actor(
        """import json,time,sys
json.loads(sys.stdin.readline())
print(json.dumps({"event":"session","session_id":"test-session"}),flush=True)
print(json.dumps({"event":"result","id":"start","result":{"ready":True}}),flush=True)
json.loads(sys.stdin.readline())
time.sleep(90)
""",
        stop=lambda sid: False,
        action_seconds=0.2,
    )
    try:
        assert a.initial()["ready"]
        with pytest.raises(InvestigationError) as raised:
            a.call({"action": "root"})
        assert raised.value.code == "action_timeout"
        assert raised.value.cleanup["cleanup_status"] == "unknown"
        with pytest.raises(InvestigationError, match="session ended"):
            a.call({"action": "root"})
        assert a.call({"action": "close"})["cleanup_status"] == "unknown"
    finally:
        a.abort()


def test_checkpoint_survives_start_timeout():
    checkpoint = {
        "schema_version": "domain-investigation/1",
        "manifest": {"cleanup_status": "open"},
        "session_open": True,
        "choices": [],
        "observations": [{"status": "partial", "errors": []}],
    }
    script = """import json,time,sys
json.loads(sys.stdin.readline())
print(json.dumps({"event":"session","session_id":"test-session"}),flush=True)
print(json.dumps({"event":"checkpoint","id":"start","packet":PACKET}),flush=True)
time.sleep(90)
""".replace("PACKET", repr(checkpoint))
    a = actor(script, startup_seconds=0.3)
    try:
        packet = a.initial()
        assert packet["session_open"] is False
        assert packet["manifest"]["cleanup_status"] == "stopped"
        assert "worker_deadline_exceeded" in packet["observations"][0]["errors"]
    finally:
        a.abort()


def test_process_crash_does_not_wait_for_full_lease():
    a = actor("import sys;sys.stdin.readline();sys.exit(7)")
    try:
        with pytest.raises(InvestigationError, match="ended"):
            a.initial()
        assert a.ended.is_set()
        assert a.close_result["cleanup_status"] == "unknown"
    finally:
        a.abort()


def test_destination_refusal_is_not_replaced_with_checkpoint():
    a = actor("""import json,sys
sys.stdin.readline()
print(json.dumps({"event":"checkpoint","id":"start","packet":{"observations":[]}}),flush=True)
print(json.dumps({"event":"error","reason_code":"private_address","message":"Destination refused"}),flush=True)
""")
    try:
        with pytest.raises(DestinationRefused):
            a.initial()
    finally:
        a.abort()


@pytest.mark.parametrize(
    "mode,stopped,reason",
    [
        ("stall", True, "worker_deadline_exceeded"),
        ("crash", True, "worker_failed"),
        ("crash", False, "worker_failed"),
    ],
)
def test_real_dom_checkpoint_passes_case_validation_after_screenshot_interruption(
    tmp_path, monkeypatch, mode, stopped, reason
):
    a = ProcessActor(
        {"url": "https://public.test/seed"},
        None,
        lambda actor: None,
        command=[
            sys.executable,
            str(Path(__file__).with_name("isolated_fixture_worker.py")),
            f"--{mode}-screenshot",
        ],
        stop=lambda sid: stopped,
        startup_seconds=20,
        action_seconds=0.5,
    )
    try:
        initial = a.initial()
        assert initial["session_open"] and initial["observations"][0]["dom_snapshot"]
        packet = a.call({"action": "screenshot", "view_id": initial["view_id"]})
        assert packet["session_open"] is False
        assert reason in packet["observations"][0]["errors"]
        assert not packet["observations"][0].get("screenshot_base64")
        monkeypatch.setattr(cli, "_lease_path", lambda output: tmp_path / "lease.json")
        output = tmp_path / "case"
        case = cli.start(
            output,
            "https://public.test/seed",
            "Inspect the support portal",
            request=lambda *args: {**packet, "session_token": "fixture-capability"},
        )
        verify_case(output)
        observation = case["observations"][0]
        assert "Example support" in observation["visible_text"]
        assert (output / observation["dom_snapshot"]).is_file()
        assert observation["evidence_items"]
        assert observation["status"] == "partial"
        assert case["assessment"]["verdict"] is None
        assert case["sessions"][0]["cleanup_status"] == (
            "stopped" if stopped else "unknown"
        )
    finally:
        a.abort()


def test_close_and_watchdog_race_is_idempotent():
    stopped = []
    a = actor(
        """import sys,json,time
sys.stdin.readline()
print(json.dumps({"event":"session","session_id":"test-session"}),flush=True)
print(json.dumps({"event":"result","id":"start","result":{}}),flush=True)
time.sleep(90)
""",
        stop=lambda sid: stopped.append(sid) or True,
    )
    a.initial()
    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(lambda _: a.abort(), range(3)))
    assert stopped == ["test-session"] and a.ended.is_set()


def test_real_chromium_state_and_evidence_survive_process_protocol():
    command = [
        sys.executable,
        str(Path(__file__).with_name("isolated_fixture_worker.py")),
    ]
    manager = InvestigationManager(
        actor_factory=partial(ProcessActor, command=command, stop=lambda sid: True)
    )
    try:
        packet = manager.start({"url": "https://public.test/seed"})
        token = packet["session_token"]
        first = packet["observations"][0]
        assert not first["screenshot_base64"] and first["dom_snapshot"]
        assert first["evidence_items"]
        choice = next(c for c in packet["choices"] if "verification" in c["text"])
        second = manager.request(
            {
                "session_token": token,
                "action": "follow",
                "candidate_id": choice["id"],
                "view_id": packet["view_id"],
            }
        )
        assert second["observations"][0]["forms"][0]["fields"][0]["type"] == "password"
        assert (
            "Missing session context" not in second["observations"][0]["visible_text"]
        )
        visual = manager.request({"session_token": token, "action": "screenshot", "view_id": second["view_id"]})
        assert visual["observations"][0]["screenshot_base64"]
        close = manager.request({"session_token": token, "action": "close"})
        assert close["cleanup_status"] == "stopped"
    finally:
        manager.close_all()
    assert manager.capacity()["active_sessions"] == 0
