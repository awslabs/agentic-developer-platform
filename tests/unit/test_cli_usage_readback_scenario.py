"""Safety/grading checks for the existing harness's read-only usage diagnostic."""

import importlib.util
from pathlib import Path

import pytest

REMOTE = Path(__file__).resolve().parents[1] / "e2e/cli_uplift/remote"
FIXTURE = {
    "run_id": "owned-run",
    "request_id": "request",
    "start": "2026-09-25T00:00:00Z",
    "end": "2026-09-26T00:00:00Z",
}


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.syspath_prepend(str(REMOTE))
    spec = importlib.util.spec_from_file_location(
        "usage_diagnostic", REMOTE / "usage_readback.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(
        self,
        *,
        missing=False,
        wrong_link=False,
        truncated=False,
        duplicate=False,
        bad_exit=False,
    ):
        self.calls = []
        self.missing, self.wrong_link = missing, wrong_link
        self.truncated, self.duplicate, self.bad_exit = truncated, duplicate, bad_exit

    def payload(self, *, summary=False, continuation=False):
        rows = (
            []
            if self.missing
            else [
                {
                    "id": "two" if continuation and not self.duplicate else "one",
                    "request_id": "request",
                    "invocation_id": "foreign" if self.wrong_link else "owned-run",
                }
            ]
        )
        return {
            "status": "ok",
            "detail": {
                "scope": {"kind": "own", "coverage": "selected_run", "org_id": "org"},
                "items": [{"requests": 2}] if summary else rows,
                "complete": not self.truncated or continuation,
                "next_cursor": "cursor"
                if self.truncated and not continuation
                else None,
            },
        }

    def json(self, args):
        self.calls.append(args)
        return self.payload(summary=args[1] == "summary")

    def run(self, args, **kwargs):
        self.calls.append(args)
        payload = self.payload(continuation="--cursor" in args)
        complete = payload["detail"]["complete"]
        payload["status"] = "ok" if complete else "pending"
        return (0 if self.bad_exit or complete else 4), payload


@pytest.mark.parametrize("missing", ["run_id", "request_id", "start", "end"])
def test_refuses_unspecified_fixture_before_any_cli_call(scenario, missing):
    fixture = {key: value for key, value in FIXTURE.items() if key != missing}
    cli = Client()
    with pytest.raises(scenario.common.RemoteError):
        scenario.exercise(cli, fixture, {})
    assert cli.calls == []


@pytest.mark.parametrize(
    "client,match",
    [
        (Client(missing=True), "no visible usage"),
        (Client(wrong_link=True), "linkage mismatch"),
        (Client(truncated=True, duplicate=True), "duplicated"),
        (Client(truncated=True, bad_exit=True), "exit code"),
    ],
)
def test_invalid_results_cannot_pass_diagnostic(scenario, client, match):
    with pytest.raises(scenario.common.RemoteError, match=match):
        scenario.exercise(client, FIXTURE, {})


def test_only_read_commands_and_existing_fixture_no_inference(scenario):
    cli, evidence = Client(truncated=True), {}
    scenario.exercise(cli, FIXTURE, evidence)
    assert evidence["continuation_exercised"] is True
    assert "acceptance remain open" in evidence["qualification"]
    assert {tuple(call[:2]) for call in cli.calls} == {
        ("usage", "summary"),
        ("usage", "requests"),
        ("usage", "request"),
        ("logs", "export"),
    }
    assert all("--run" in call and "owned-run" in call for call in cli.calls)
    assert all("--yes" not in call for call in cli.calls)


def test_invalid_time_window_rejected_before_reads(scenario):
    cli = Client()
    with pytest.raises(scenario.common.RemoteError):
        scenario.exercise(cli, {**FIXTURE, "end": FIXTURE["start"]}, {})
    assert cli.calls == []
