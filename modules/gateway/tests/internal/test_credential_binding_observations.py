"""The real resolver must emit a denominator and outcomes on every decision."""

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.internal import credential_binding as binding


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize(
    "invocation,registry,expected",
    [
        (None, "", (0, 0, 1)),
        ("run-a", "", (0, 0, 1)),
        ("run-a", "alice", (1, 0, 0)),
        ("run-a", "bob", (1, 1, 0)),
    ],
)
def test_every_binding_result_has_one_coherent_observation(monkeypatch, capsys, enforce, invocation, registry, expected):
    monkeypatch.setenv("BG_ENVIRONMENT", "test")
    monkeypatch.setattr(binding, "_lookup_authorized_user", lambda **_: registry)
    settings = SimpleNamespace(enforce_credential_binding=enforce, webhook_events_table="events", aws_region="us-east-1")
    verified = binding.BindingResult(registry, True, False, registry, "run-a", "tenant") if registry else None
    refused = not invocation or registry != "alice"
    if refused:
        with pytest.raises(HTTPException) as error:
            binding.resolve_credential_binding(invocation_id=invocation, body_user_id="alice", settings=settings, verified_binding=verified)
        assert error.value.status_code == 403
    else:
        result = binding.resolve_credential_binding(invocation_id=invocation, body_user_id="alice", settings=settings, verified_binding=verified)
        assert result.resolved_user_id == (registry or "alice")
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["Environment"] == "test"
    assert event["CredentialAuthorizationChecked"] == 1
    assert (
        tuple(event[key] for key in ("CredentialAuthorizationFromRegistry", "CredentialAuthorizationDrift", "CredentialAuthorizationFallback"))
        == expected
    )
    emf = event["_aws"]["CloudWatchMetrics"][0]
    assert emf["Dimensions"] == [["Environment"]]
    assert len(emf["Metrics"]) == 4
    assert all(type(event[m["Name"]]) is int for m in emf["Metrics"])
    assert all(value not in lines[0] for value in ("alice", "bob", "run-a"))
