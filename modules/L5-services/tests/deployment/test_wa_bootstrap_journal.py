"""An uncertain Product acknowledgement cannot create fresh admission intent."""

import json
import importlib.util
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from qualification_well_architected_journal import Journal
from l5_services.adapters.well_architected.qualification import ProductReader


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("OWNER_TOKEN", "private-owner-token")
    selected = {"gateway_url": "https://selected.example.invalid", "token_env": "OWNER_TOKEN",
                "scope": {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}}
    directory = tmp_path.resolve() / "journal"
    return selected, directory, {"fixture_sha256": "a" * 64, "run_id": 42}


def test_lost_response_replays_exact_product_intent_then_only_reads_accepted_id(setup):
    selected, directory, context = setup
    calls = []

    def respond(request):
        calls.append(request)
        intent = list(directory.glob("*-intent.json"))
        assert len(intent) == 1 and json.loads(intent[0].read_text())["body"] == {"source": "pinned"}
        if len(calls) == 1:
            raise httpx.ReadTimeout("PRIVATE-REMOTE-BODY", request=request)
        return httpx.Response(200, json={"run_id": "original-run", "state": "accepted" if request.method == "POST" else "succeeded"})

    with httpx.Client(base_url=selected["gateway_url"], transport=httpx.MockTransport(respond)) as client:
        reader = ProductReader(client, selected["scope"])
        with pytest.raises(httpx.ReadTimeout):
            Journal(directory, context).post(reader, selected, "runs", {"source": "pinned"}, "stable-key")
        assert not list(directory.glob("*-ack.json"))
        assert Journal(directory, context).post(reader, selected, "runs", {"source": "pinned"}, "stable-key")["run_id"] == "original-run"
        assert Journal(directory, context).post(reader, selected, "runs", {"source": "pinned"}, "stable-key")["state"] == "succeeded"
    assert [call.method for call in calls] == ["POST", "POST", "GET"]
    assert calls[0].content == calls[1].content and calls[0].headers["Idempotency-Key"] == calls[1].headers["Idempotency-Key"]
    assert calls[2].url.path.endswith("/runs/original-run")
    assert all(path.stat().st_mode & 0o077 == 0 for path in directory.iterdir())
    assert "private-owner-token" not in "".join(path.read_text() for path in directory.iterdir())


@pytest.mark.parametrize("change", ["token", "body", "key", "gateway", "release"])
def test_changed_principal_or_intent_is_rejected_before_replay(setup, monkeypatch, change):
    selected, directory, context = setup
    calls = []

    def respond(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response", request=request)

    with httpx.Client(base_url=selected["gateway_url"], transport=httpx.MockTransport(respond)) as client:
        reader = ProductReader(client, selected["scope"])
        with pytest.raises(httpx.ReadTimeout):
            Journal(directory, context).post(reader, selected, "runs", {"source": "pinned"}, "stable-key")
        body, key = {"source": "pinned"}, "stable-key"
        if change == "token":
            monkeypatch.setenv("OWNER_TOKEN", "other-actor-token")
        elif change == "body":
            body["source"] = "new-source"
        elif change == "key":
            key = "new-key"
        elif change == "gateway":
            selected["gateway_url"] = "https://other.invalid"
        else:
            context["run_id"] = 43
        with pytest.raises(ValueError, match="conflict"):
            Journal(directory, context).post(reader, selected, "runs", body, key)
    assert len(calls) == 1


def test_actionable_failure_does_not_echo_private_provider_error(capsys):
    spec = importlib.util.spec_from_file_location("wa_release_errors_test", Path(__file__).resolve().parents[2] / "scripts/qualification_well_architected_release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)

    assert release.failure("bootstrap", httpx.ReadTimeout("PRIVATE-RESPONSE")) == 1
    text = capsys.readouterr().err
    assert "transport_response_unknown" in text and "bootstrap-journal" in text and "idempotency key" in text
    assert "PRIVATE-" not in text


def test_ordinary_journal_keeps_its_original_size_bound(setup):
    _, directory, context = setup
    journal = Journal(directory, context)
    with pytest.raises(ValueError, match="exceeds its bound"):
        journal.retain("large.json", {"value": "x" * (256 * 1024)})
    assert not (directory / "large.json").exists()
