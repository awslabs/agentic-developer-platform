"""Remote format checks run against the real served CLI export serializer."""

import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def modules(monkeypatch):
    root = Path(__file__).parents[2]
    remote = root / "tests/e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    loaded = []
    for name, path in (
        ("remote_usage_formats", remote / "usage_exports.py"),
        ("served_usage_formats", root / "modules/gateway/cli/adp-usage.py"),
    ):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
    return loaded


def page(*, record="first", complete=True, cursor=None, unknown=False):
    return {
        "status": "ok" if complete else "pending",
        "detail": {
            "scope": {"kind": "own", "org_id": "tenant", "principal_id": "human"},
            "start": "2026-09-26T00:00:00Z",
            "end": "2026-09-26T01:00:00Z",
            "complete": complete,
            "next_cursor": cursor,
            "items": []
            if record is None
            else [
                {
                    "id": record,
                    "model": "=unsafe-formula",
                    "request_id": "request",
                    "cost": {
                        "status": "unknown" if unknown else "estimated",
                        "amount": None if unknown else "0.000000000000000001",
                        "currency": "USD",
                        "settlement": "unknown",
                    },
                }
            ],
        },
    }


def emitted(cli, value, mode):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.emit_export(value, mode)
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("mode", ["ndjson", "csv"])
@pytest.mark.parametrize("unknown,empty", [(False, False), (True, False), (True, True)])
def test_real_export_serializer_round_trips_precision_unknown_and_empty(
    modules, mode, unknown, empty
):
    remote, cli = modules
    rows, meta = remote.parse(
        mode,
        *emitted(cli, page(record=None if empty else "record", unknown=unknown), mode),
    )
    assert meta["complete"] is True
    if empty:
        assert rows == []
    elif mode == "csv":
        assert rows[0]["cost_amount"] == ("" if unknown else "0.000000000000000001")
        assert rows[0]["model"] == "'=unsafe-formula"
    else:
        assert rows[0]["cost"]["amount"] == (
            None if unknown else "0.000000000000000001"
        )


@pytest.mark.parametrize(
    "fault",
    [
        "exit",
        "scope",
        "cursor",
        "unknown_zero",
        "float_amount",
        "private_field",
        "extra_record",
    ],
)
def test_export_rejects_misleading_or_private_output(modules, fault):
    remote, cli = modules
    value = page(complete=False, cursor="next")
    if fault == "scope":
        value["detail"]["scope"]["kind"] = "managed"
    if fault == "cursor":
        value["detail"]["next_cursor"] = None
    if fault == "unknown_zero":
        value["detail"]["items"][0]["cost"].update(status="unknown", amount="0")
    if fault == "float_amount":
        value["detail"]["items"][0]["cost"]["amount"] = 0.125
    if fault == "private_field":
        value["detail"]["items"][0]["access_token"] = "never-record"
    if fault == "extra_record":
        value["detail"]["items"].append(copy.deepcopy(value["detail"]["items"][0]))
    code, out, err = emitted(cli, value, "ndjson")
    with pytest.raises(remote.common.RemoteError):
        remote.parse("ndjson", 0 if fault == "exit" else code, out, err)


@pytest.mark.parametrize(
    "fault",
    [
        "duplicate_continuation",
        "record_after_continuation",
        "record_scope",
        "csv_formula",
        "csv_missing_column",
        "csv_no_stderr",
    ],
)
def test_export_rejects_wire_framing_errors(modules, fault):
    remote, cli = modules
    mode = "csv" if fault.startswith("csv") else "ndjson"
    code, out, err = emitted(cli, page(), mode)
    if fault == "duplicate_continuation":
        out += out.splitlines()[-1] + "\n"
    if fault == "record_after_continuation":
        out += out.splitlines()[0] + "\n"
    if fault == "record_scope":
        frames = [json.loads(line) for line in out.splitlines()]
        frames[0]["scope"]["principal_id"] = "foreign"
        out = "\n".join(json.dumps(frame) for frame in frames)
    if fault == "csv_formula":
        out = out.replace("'=unsafe-formula", "=unsafe-formula")
    if fault == "csv_missing_column":
        out = out.replace("settlement", "private_column")
    if fault == "csv_no_stderr":
        err = ""
    with pytest.raises(remote.common.RemoteError):
        remote.parse(mode, code, out, err)


@pytest.mark.parametrize(
    "fault", [None, "duplicate", "scope_change", "cursor_stuck", "window_change"]
)
def test_bounded_cli_continuation_keeps_flags_and_never_retains_rows(
    modules, monkeypatch, fault
):
    remote, served = modules
    calls = []
    flags = ["--start", "2026-09-26T00:00:00Z", "--end", "2026-09-26T01:00:00Z"]
    client = SimpleNamespace(
        binary="/isolated/adp",
        env={"BG_CONFIG_DIR": "/isolated/config"},
        timeout=30,
        transcript=[],
    )

    def invoke(argv, **kwargs):
        calls.append(argv)
        assert kwargs == {"env": client.env, "timeout": 30}
        assert argv[1:3] == ["logs", "export"] and argv[3:7] == flags
        assert argv[argv.index("--page-size") + 1] == "1"
        assert argv[argv.index("--max-pages") + 1] == "1"
        resumed = "--cursor" in argv
        value = page(
            record="first" if not resumed or fault == "duplicate" else "second",
            complete=resumed and fault != "cursor_stuck",
            cursor=None if resumed and fault != "cursor_stuck" else "next",
        )
        if resumed and fault == "scope_change":
            value["detail"]["scope"]["principal_id"] = "foreign"
        if fault == "window_change":
            value["detail"]["end"] = "2026-09-26T02:00:00Z"
        return emitted(served, value, argv[argv.index("--format") + 1])

    monkeypatch.setattr(remote.common, "bounded", invoke)
    evidence = {}
    if fault:
        with pytest.raises(remote.common.RemoteError):
            remote.exercise(client, flags, evidence)
    else:
        remote.exercise(client, flags, evidence)
        assert len(calls) == 4
        assert all(
            row
            == {
                "pages": 2,
                "records_observed": 2,
                "complete": True,
                "continuation_exercised": True,
            }
            for row in evidence["export_formats"].values()
        )
        assert "first" not in json.dumps(
            evidence
        ) and "unsafe-formula" not in json.dumps(evidence)


def test_usage_exports_alias_selects_existing_case_without_domain_dependencies():
    from tests.e2e.cli_uplift import cases, stages

    assert [case.id for case in cases.suite_cases("usage-exports")] == ["E21"]
    assert stages.JOURNEY_DRIVERS["E21"] == "story_usage"
    combined = cases.resolve_suites(("login", "usage-exports", "hosted-coding"))
    assert [case.id for case in combined] == ["E01", "E21", "E42", "C01"]
    matrix = cases.new_matrix(("usage-exports",))
    cases.record(matrix, "E21", cases.FAILED)
    assert cases.accept(matrix, ("usage-exports",))[0] == cases.FAILED
    assert "E21" in {case.id for case in cases.suite_cases("nightly")}


@pytest.mark.parametrize("fault", [None, "tenant", "owner"])
def test_export_checks_explicit_fixture_scope_before_accepting_records(
    modules, monkeypatch, fault
):
    remote, served = modules
    client = SimpleNamespace(binary="/isolated/adp", env={}, timeout=30, transcript=[])
    flags = ["--start", "2026-09-26T00:00:00Z", "--end", "2026-09-26T01:00:00Z"]

    def invoke(argv, **kwargs):
        value = page()
        value["detail"]["scope"] = {
            "kind": "own",
            "org_id": "wrong" if fault == "tenant" else "tenant",
            "user_id": "wrong" if fault == "owner" else "human",
        }
        return emitted(served, value, argv[argv.index("--format") + 1])

    monkeypatch.setattr(remote.common, "bounded", invoke)
    evidence = {"usage_owner": {"org_id": "tenant", "user_id": "human"}}
    if fault:
        with pytest.raises(remote.common.RemoteError, match="verified owner"):
            remote.exercise(client, flags, evidence)
        assert "export_formats" not in evidence
    else:
        remote.exercise(client, flags, evidence)
        assert all(
            v["records_observed"] == 1 for v in evidence["export_formats"].values()
        )


@pytest.mark.parametrize("fault", [None, "coverage", "row", "missing"])
def test_selected_run_exports_reject_broad_scope_and_foreign_rows(
    modules, monkeypatch, fault
):
    remote, served = modules
    run = "57e3ed64-0794-4df0-828f-565479f9ddac"
    client = SimpleNamespace(binary="/isolated/adp", env={}, timeout=30, transcript=[])
    flags = [
        "--start",
        "2026-09-26T00:00:00Z",
        "--end",
        "2026-09-26T01:00:00Z",
        "--run",
        run,
    ]
    calls = []

    def invoke(argv, **kwargs):
        assert argv[argv.index("--run") + 1] == run
        mode = argv[argv.index("--format") + 1]
        calls.append(mode)
        value = page()
        value["detail"]["scope"]["coverage"] = (
            "direct_identity_records" if fault == "coverage" else "selected_run"
        )
        if fault != "missing":
            value["detail"]["items"][0]["invocation_id"] = (
                "other" if fault == "row" else run
            )
        return emitted(served, value, mode)

    monkeypatch.setattr(remote.common, "bounded", invoke)
    evidence = {"usage_run_id": run}
    if fault:
        with pytest.raises(remote.common.RemoteError):
            remote.exercise(client, flags, evidence)
    else:
        remote.exercise(client, flags, evidence)
        assert calls == ["ndjson", "csv"]
        assert all(
            item["records_observed"] == 1
            for item in evidence["export_formats"].values()
        )
