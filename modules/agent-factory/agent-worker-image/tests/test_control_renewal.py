"""The running listener sees staged tokens before the gateway can publish them."""

import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.control_renewal import ControlRenewal


def test_lost_response_retries_same_rotation_and_retains_bounded_overlap(tmp_path):
    clock = [1800000000.0]
    calls = []

    def post(path, request):
        lease = json.loads((tmp_path / "lease.json").read_text())
        assert lease["current"]["token"] == request["control_token"]
        assert lease["generation"] == 7
        calls.append(request)
        if len(calls) == 1:
            raise OSError("lost response")
        return {
            "control_generation": 7,
            "control_credential_epoch": 2,
            "rotation_id": request["rotation_id"],
        }

    session = ControlRenewal(
        run_id="run",
        generation=7,
        token="a" * 40,
        expires_at="2027-01-15T09:00:00Z",
        post=post,
        directory=tmp_path,
        now=lambda: clock[0],
    )
    with pytest.raises(OSError):
        session.refresh()
    original = (tmp_path / "lease.json").read_bytes()
    clock[0] += 60
    session.refresh()
    assert calls[0] == calls[1]
    assert (tmp_path / "lease.json").read_bytes() == original
    assert (tmp_path / "lease.json").stat().st_mode & 0o077 == 0
    session.close()
    assert not session.path.exists()
    session.refresh()
    assert len(calls) == 2


def test_failed_local_stage_never_publishes_token(tmp_path, monkeypatch):
    calls = []
    session = ControlRenewal(
        run_id="run",
        generation=1,
        token="a" * 40,
        expires_at="2027-01-15T09:00:00Z",
        post=lambda *args: calls.append(args),
        directory=tmp_path,
    )

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(session, "_write", fail)
    with pytest.raises(OSError):
        session.refresh()
    assert calls == []


@pytest.mark.parametrize("committed", [False, True])
def test_outage_beyond_token_expiry_resolves_epoch_before_new_rotation(tmp_path, committed):
    clock = [1800000000.0]
    requests = []

    def post(path, request):
        requests.append((path, request))
        if len(requests) == 1:
            raise OSError("response lost")
        if path.endswith("/state"):
            return {
                "control_generation": 7,
                "control_credential_epoch": 2 if committed else 1,
                "rotation_id": requests[0][1]["rotation_id"] if committed else None,
            }
        return {
            "control_generation": 7,
            "control_credential_epoch": request["expected_epoch"] + 1,
            "rotation_id": request["rotation_id"],
        }

    session = ControlRenewal(
        run_id="run",
        generation=7,
        token="a" * 40,
        expires_at="2027-01-15T09:00:00Z",
        post=post,
        directory=tmp_path,
        now=lambda: clock[0],
    )
    with pytest.raises(OSError):
        session.refresh()
    clock[0] += 4000
    session.refresh()
    assert requests[1][0].endswith("/state")
    assert requests[2][1]["expected_epoch"] == (2 if committed else 1)
    assert requests[2][1]["rotation_id"] != requests[0][1]["rotation_id"]
    assert (
        json.loads(session.path.read_text())["current"]["token"] == requests[2][1]["control_token"]
    )
