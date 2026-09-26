"""E23/E27 grading: no invisible tenant, mutation or failed read is a pass."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "tenant_scenario", remote / "tenant_isolation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, wrong=False):
        self.calls = []
        self.wrong = wrong

    def json(self, args):
        self.calls.append(args)
        if args[:2] == ["tenant", "list"]:
            data = {"items": [{"org_id": "a"}, {"org_id": "b"}]}
        elif args[:2] == ["tenant", "use"]:
            data = {"tenant_id": args[2]}
        elif "capabilities" in args:
            data = {"tenant": {"org_id": "foreign" if self.wrong else args[1]}}
        else:
            data = {"tenant_id": args[1], "selection_source": "flag"}
        return {"status": "ok", "detail": data}

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args == ["refresh"]:
            return 0, None
        return 4, {"status": "failed", "error": {"code": "tenant_selection_required"}}


def test_smoke_runs_without_default_writes_or_inference(scenario):
    cli, evidence = Client(), {}
    scenario.smoke(cli, evidence)
    assert evidence["tenant_id"] == "a"
    assert all(
        "use" not in call and "refresh" not in call and "select" not in call
        for call in cli.calls
    )


def test_two_tenant_fixture_requires_visible_distinct_memberships(scenario):
    for fixture in [{}, {"tenant_ids": ["a", "a"]}, {"tenant_ids": ["a", "foreign"]}]:
        cli = Client()
        with pytest.raises(scenario.common.RemoteError):
            scenario.isolated_reads(cli, fixture, {})
        assert all("use" not in call and "refresh" not in call for call in cli.calls)


def test_crossed_tenant_read_cannot_pass(scenario):
    with pytest.raises(scenario.common.RemoteError, match="crossed"):
        scenario.isolated_reads(Client(wrong=True), {"tenant_ids": ["a", "b"]}, {})


def test_two_tenant_scenario_refreshes_but_does_not_claim_inference(scenario):
    cli, evidence = Client(), {}
    scenario.isolated_reads(cli, {"tenant_ids": ["a", "b"]}, evidence)
    assert evidence["refreshed"] is True
    assert ["refresh"] in cli.calls
    assert "remain separate acceptance" in evidence["qualification"]
    assert not any("workspaces/select" in call or "task" in call for call in cli.calls)


@pytest.mark.parametrize(
    "failure",
    [None, "foreign_identity", "leaked_default", "command_error", "partial_write"],
)
@pytest.mark.parametrize("membership_count", [1, 2])
def test_user_switch_restores_original_stores_and_rejects_false_passes(
    scenario, tmp_path, monkeypatch, failure, membership_count
):
    import json

    directory = tmp_path / ".bedrock-gateway"
    directory.mkdir()
    originals = {
        "config.json": b'{"gateway_url":"https://original","client_id":"keep"}',
        "tokens.json": b'{"access_token":"original-fixture"}',
    }
    for name, content in originals.items():
        (directory / name).write_bytes(content)
    monkeypatch.setattr(
        scenario, "ordinary_session", lambda *args: {"access_token": "ordinary-fixture"}
    )
    if failure == "partial_write":

        def interrupted_write(*args):
            (directory / "config.json").write_text("partial")
            raise OSError("fixture write interrupted")

        monkeypatch.setattr(scenario, "_write_session", interrupted_write)

    class SwitchingClient:
        def __init__(self):
            self.saved = False

        def json(self, args):
            ordinary = (
                json.loads((directory / "tokens.json").read_text())["access_token"]
                == "ordinary-fixture"
            )
            if ordinary and failure == "command_error":
                raise scenario.common.RemoteError("fixture read refused")
            if args == ["tenant", "list"]:
                data = {
                    "items": [{"org_id": "native"}]
                    + ([{"org_id": "managed"}] if membership_count == 2 else [])
                }
            elif "capabilities" in args:
                data = {"tenant": {"org_id": "native"}}
            else:
                if args[:2] == ["tenant", "use"]:
                    self.saved = True
                data = {
                    "identity": "ordinary"
                    if ordinary and failure != "foreign_identity"
                    else "original",
                    "tenant_id": "native" if ordinary else "managed",
                    "selection_source": "saved_default"
                    if not ordinary or self.saved or failure == "leaked_default"
                    else "single_membership",
                }
            if args[:1] == ["--tenant"]:
                data["selection_source"] = "flag"
            return {"status": "ok", "detail": data}

        def run(self, args, **kwargs):
            if failure == "leaked_default":
                return 0, {"status": "ok", "detail": {"tenant_id": "managed"}}
            return 4, {
                "status": "failed",
                "error": {"code": "tenant_selection_required"},
            }

    evidence = {}
    fixture = {
        "ordinary_fixture_name": "owned-fixture",
        "ordinary_login_user_id": "ordinary",
        "ordinary_tenant_id": "native",
    }
    if failure:
        with pytest.raises((scenario.common.RemoteError, OSError)):
            scenario.user_switch_reads(
                SwitchingClient(),
                {"gateway_url": "https://gateway"},
                fixture,
                tmp_path,
                evidence,
            )
        assert "user_switch" not in evidence
    else:
        scenario.user_switch_reads(
            SwitchingClient(),
            {"gateway_url": "https://gateway"},
            fixture,
            tmp_path,
            evidence,
        )
        assert evidence["user_switch"]["original_default_preserved"]
    for name, content in originals.items():
        assert (directory / name).read_bytes() == content
        assert (directory / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "switch",
    [
        None,
        {},
        {"ordinary_fixture_name": "fixture"},
        {
            "ordinary_fixture_name": "fixture",
            "ordinary_login_user_id": "member",
            "ordinary_tenant_id": "native",
            "password": "not-allowed",
        },
    ],
)
def test_user_switch_fixture_requires_exact_nonsecret_identifiers(switch):
    import json
    from tests.e2e.cli_uplift.fixtures import parse
    from tests.e2e.cli_uplift.config import ConfigError

    with pytest.raises(ConfigError):
        parse(
            json.dumps(
                {"tenant_isolation": {"tenant_ids": ["a", "b"], "user_switch": switch}}
            )
        )


def test_user_switch_fixture_preserves_optional_existing_nightly_contract():
    import json
    from tests.e2e.cli_uplift.fixtures import parse

    fixture = {
        "tenant_isolation": {
            "tenant_ids": ["a", "b"],
            "user_switch": {
                "ordinary_fixture_name": "adp/owned-fixture",
                "ordinary_login_user_id": "member",
                "ordinary_tenant_id": "native",
            },
        }
    }
    assert parse(json.dumps(fixture)) == fixture
