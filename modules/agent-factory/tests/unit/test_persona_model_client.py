"""The saved-model producer transport authenticates the exact selection request."""

import base64
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.credentials import Credentials

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "webhook-ingress/lambda/common/persona_model_client.py"
spec = importlib.util.spec_from_file_location("pmm_selection_client", SOURCE)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


def test_transport_copies_are_identical():
    assert (
        SOURCE.read_bytes()
        == (ROOT / "gateway/lambdas/ingest/persona_model_client.py").read_bytes()
    )


@pytest.mark.parametrize("refusal", [None, "static", "persona", "source", "oversize"])
def test_authenticated_selection_and_refusal(monkeypatch, refusal):
    monkeypatch.setenv(
        "ADP_AGENT_CONTROL_ENDPOINT",
        "https://api123.execute-api.us-east-1.amazonaws.com/dev/internal/v1/agent",
    )
    creds = Credentials("example", "example", None if refusal == "static" else "temporary")
    monkeypatch.setattr(
        client.botocore.session,
        "get_session",
        lambda: SimpleNamespace(get_credentials=lambda: creds),
    )
    envelope = {"tenant_id": "tenant", "persona": "developer", "model_requested": "chosen"}
    receipt = {
        "persona": "developer",
        "principal_id": "canonical-human",
        "model": "chosen",
        "source": "explicit-direct",
    }
    if refusal in {"persona", "source"}:
        receipt[refusal] = "unexpected"
    raw = b"x" * 262145 if refusal == "oversize" else json.dumps(receipt).encode()

    def open_request(request, timeout):
        assert 0 < timeout <= 6
        assert request.full_url.endswith("/persona-model/resolve")
        assert json.loads(request.data) == {
            "tenant_id": "tenant",
            "user_id": "root-human",
            "persona": "developer",
            "direct_model": "chosen",
        }
        proof = json.loads(base64.b64decode(request.headers["X-adp-producer-proof"]))
        assert proof["x-adp-work-invocation"] == hashlib.sha256(request.data).hexdigest()
        assert "x-adp-work-invocation;" in proof["authorization"]
        response = Mock(status=200)
        response.read.return_value = raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    opener = SimpleNamespace(open=Mock(side_effect=open_request))
    monkeypatch.setattr(client.urllib.request, "build_opener", lambda *args: opener)
    if refusal:
        with pytest.raises(client.ModelSelectionError):
            client.select_persona_model(envelope, user_id="root-human")
    else:
        result = client.select_persona_model(envelope, user_id="root-human")
        assert result["model_resolved"] == "chosen"
        assert result["model_selection"] == receipt
        assert "model_selection" not in envelope
    if refusal == "static":
        opener.open.assert_not_called()
