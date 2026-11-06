import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import actions_broker, mediated_github
from lib.mediated_github import MediatedConflict, MediatedRefused, MediatedUnavailable


@pytest.mark.parametrize("operation", ["actions_dispatch", "actions_rerun", "actions_runs", "actions_run", "actions_logs"])
def test_operation_uses_fixed_signed_gateway_path(monkeypatch, capsys, operation):
    calls = []

    def request(payload, *, path, conflict_codes):
        assert conflict_codes == frozenset(actions_broker.CONFLICT_ACTIONS)
        calls.append((payload, path))
        return {"dispatched": True}

    monkeypatch.setattr(actions_broker, "_request", request)
    monkeypatch.setattr(sys, "argv", ["actions_broker", operation])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"workflow":"deploy.yml"}'))
    actions_broker.main()
    assert calls == [({"workflow": "deploy.yml"}, "/internal/v1/agent/actions/" + operation.removeprefix("actions_"))]
    assert json.loads(capsys.readouterr().out) == {"dispatched": True}


@pytest.mark.parametrize("operation", ["../other", "actions_reset", "shell"])
def test_unknown_operation_refused_before_gateway_call(monkeypatch, operation):
    monkeypatch.setattr(sys, "argv", ["actions_broker", operation])
    with pytest.raises(SystemExit, match="unsupported Actions operation"):
        actions_broker.main()


@pytest.mark.parametrize("error", [MediatedRefused, MediatedConflict, MediatedUnavailable])
def test_transport_failure_reaches_agent_as_typed_error_without_traceback(monkeypatch, capsys, error):
    def request(*args, **kwargs):
        raise error("bounded transport reason")

    monkeypatch.setattr(actions_broker, "_request", request)
    monkeypatch.setattr(sys, "argv", ["actions_broker", "actions_dispatch"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"workflow":"deploy.yml"}'))
    actions_broker.main()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "bounded transport reason", "error_code": error.__name__}
    assert not captured.err


@pytest.mark.parametrize("operation", ["actions_dispatch", "actions_rerun"])
@pytest.mark.parametrize(
    "reason",
    ["operations_repair_merge_required", "delivery_target_busy", "delivery_dispatch_reconciliation_required"],
)
def test_gateway_conflict_reaches_agent_with_actionable_reason(monkeypatch, capsys, operation, reason):
    """Exercise the signed transport, HTTP error decoding and broker output."""
    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gateway.example.invalid")
    monkeypatch.setattr(mediated_github, "_identity_headers", lambda: {})
    monkeypatch.setattr(mediated_github, "_sign", lambda method, url, headers, data: headers)

    class Opener:
        def open(self, request, timeout=None):
            raise HTTPError(request.full_url, 409, "conflict", {}, io.BytesIO(json.dumps({"detail": reason}).encode()))

    monkeypatch.setattr(mediated_github, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(sys, "argv", ["actions_broker", operation])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"workflow":"deploy.yml"}'))
    actions_broker.main()
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result == {
        "error": reason,
        "error_code": "MediatedConflict",
        "reason_code": reason,
        "next_action": actions_broker.CONFLICT_ACTIONS[reason],
    }
    assert "branch moved" not in captured.out
    assert not captured.err


@pytest.mark.parametrize(
    "body",
    [
        b'{"detail":"secret_provider_value"}',
        b'{"detail":"operations_repair_merge_required: secret"}',
        b'{"detail":{"secret":"value"}}',
        b'[]',
        b'null',
        b'not json: secret',
        b'\xff',
        b'{"detail":"operations_repair_merge_required","extra":"' + b'x' * 1024 + b'"}',
    ],
)
def test_unknown_or_oversized_conflict_body_is_bounded_and_not_echoed(monkeypatch, capsys, body):
    reads = []

    class BoundedBody(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    class Opener:
        def open(self, request, timeout=None):
            raise HTTPError(request.full_url, 409, "secret", {}, BoundedBody(body))

    monkeypatch.setenv("ADP_GATEWAY_ENDPOINT", "https://gateway.example.invalid")
    monkeypatch.setattr(mediated_github, "_identity_headers", lambda: {})
    monkeypatch.setattr(mediated_github, "_sign", lambda method, url, headers, data: headers)
    monkeypatch.setattr(mediated_github, "build_opener", lambda *_: Opener())
    monkeypatch.setattr(sys, "argv", ["actions_broker", "actions_dispatch"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"workflow":"deploy.yml"}'))
    actions_broker.main()
    captured = capsys.readouterr()
    assert reads == [1025]
    assert json.loads(captured.out) == {
        "error": "the Actions operation conflicted; inspect current repair and workflow state",
        "error_code": "MediatedConflict",
    }
    assert not captured.err
