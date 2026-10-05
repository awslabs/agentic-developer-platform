"""Actual GitLab CLI serializers, reviewed scope and uncertain mutation handling."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest

from src.gitlab.routes import Configure, Connect, Project

spec = importlib.util.spec_from_file_location("gitlab_cli", Path(__file__).parents[2] / "cli/adp-gitlab.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
OP = str(uuid4())
PROVIDER = "a" * 24
REV = "b" * 64


def current():
    return {
        "contract": cli.CONTRACT,
        "org_id": "tenant",
        "configuration": {"provider_id": PROVIDER, "revision": None},
        "providers": [{"id": PROVIDER, "revision": REV, "url": "https://gitlab.example.test"}],
    }


def args(action, extra=()):
    area = ["admin", "gitlab"] if action == "configure" else ["gitlab"]
    target = ["--provider", PROVIDER] if action == "configure" else ["--project-id", "42", "--repo", "group/project"]
    if action == "connect":
        target += ["--credential", str(uuid4())]
    return cli.parser().parse_args([*area, action, *target, "--operation-id", OP, "--expect-provider-revision", REV, *extra])


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(cli.common, "ensure_can_mutate", Mock())
    return Mock()


@pytest.mark.parametrize("action", ["configure", "connect", "disconnect"])
def test_preview_only_reads_adp_and_serializes_real_server_schema(api, action):
    api.request.return_value = current()
    result = cli.execute(args(action, ["--dry-run"]), api)
    assert result["status"] == "dry_run"
    model = {"configure": Configure, "connect": Connect, "disconnect": Project}[action]
    model.model_validate_json(json.dumps(result["detail"]["request"]))
    assert [c.args[0] for c in api.request.call_args_list] == ["GET"]


@pytest.mark.parametrize("ack", [None, {}, OSError("lost")])
def test_unknown_ack_is_not_replayed_or_claimed_success(api, ack):
    api.request.side_effect = [current(), ack]
    result = cli.execute(args("connect", ["--yes"]), api)
    assert result["status"] == "pending"
    assert result["detail"]["outcome"] == "unknown"
    assert [c.args[0] for c in api.request.call_args_list] == ["GET", "POST"]


def test_foreign_ack_remains_unknown(api):
    api.request.side_effect = [current(), {"contract": cli.CONTRACT, "org_id": "foreign", "operation_id": OP}]
    assert cli.execute(args("connect", ["--yes"]), api)["status"] == "pending"


def test_stale_provider_refuses_mutation(api):
    api.request.return_value = current()
    request = args("connect", ["--yes"])
    request.expect_provider_revision = "c" * 64
    with pytest.raises(cli.common.CliError):
        cli.execute(request, api)
    assert api.request.call_count == 1


def test_cli_has_no_arbitrary_host_or_secret_flags():
    text = cli.parser().format_help()
    for flag in ["--host", "--url", "--token", "--pat"]:
        with pytest.raises(cli.common.CliError):
            cli.parser().parse_args(["gitlab", "status", flag, "secret"])
    assert "secret" not in text
