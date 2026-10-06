"""Selected vault transport preserves live approval and never uses ambient gh."""

import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from installation import runtime_cli
from installation.config import Refusal
from installation.runtime_approval import GitHubVaultReadAPI

from .test_runtime_approval import review_setup
from .test_runtime_cli import arguments

__all__ = ["review_setup", "arguments"]


def through_proxy(api):
    def proxy(method, url, **kwargs):
        assert method == "GET"
        assert url.startswith("https://api.github.com/repos/aws-e/adp")
        assert kwargs["service"] == "github" and kwargs["label"] == "private-approval"
        assert "Authorization" not in kwargs["headers"]
        return {
            "status": 200,
            "body": json.dumps(api.get(url.removeprefix("https://api.github.com/"))),
            "provenance_id": "broker-provenance",
        }

    return proxy


def test_entire_exact_head_approval_uses_vault_transport(review_setup):
    approval, args, state = review_setup
    approval.api = GitHubVaultReadAPI(
        "private-approval", proxy=through_proxy(approval.api)
    )
    result = approval.verify_plan(**args)
    assert result["approved"] is True and result["approver"] == "github:user:2"
    assert state["pull_reads"] == 2
    assert any("/reviews/100" in path for path in state["calls"])


@pytest.mark.parametrize("fault", ["head", "changes-requested", "permission"])
def test_vault_transport_preserves_live_authority_refusals(review_setup, fault):
    approval, args, state = review_setup
    approval.api = GitHubVaultReadAPI(
        "private-approval", proxy=through_proxy(approval.api)
    )
    if fault == "head":
        state["move_head"] = True
    if fault == "changes-requested":
        state["reviews"][0]["state"] = "CHANGES_REQUESTED"
    if fault == "permission":
        state["permission"]["role_name"] = "read"
    with pytest.raises(Refusal):
        approval.verify_plan(**args)


@pytest.mark.parametrize(
    "path",
    [
        "https://evil.test",
        "//evil.test",
        "repos/other/repo",
        "repos/aws-e/adp/issues",
        "repos/aws-e/adp/../secrets",
        "repos/aws-e/adp?token=secret",
        "repos/aws-e/adp/pulls/42/reviews?per_page=100&page=11",
        "repos/aws-e/adp/collaborators/%0Areviewer/permission",
    ],
)
def test_proxy_destination_and_scope_are_not_caller_controlled(path):
    proxy = Mock()
    with pytest.raises(Refusal):
        GitHubVaultReadAPI("private-approval", proxy=proxy).get(path)
    proxy.assert_not_called()


@pytest.mark.parametrize(
    "response",
    [
        {"status": 302, "headers": {"Location": "https://evil.test"}, "body": "{}"},
        {"status": 403, "body": "upstream-private-diagnostics"},
        {"status": True, "body": "{}"},
        {"status": 200, "body": {"fake": "already-decoded"}},
        {"status": 200, "body": "not-json"},
        {"status": 200, "body": '{"id":1,"id":2}'},
        {"status": 200, "body": " " * (2 * 1024 * 1024 + 1)},
        {"status": 200, "body": "é" * (1024 * 1024 + 1)},
        {"status": 200, "body": "\ud800"},
    ],
)
def test_failed_malformed_or_unbounded_responses_refuse_without_fallback(
    response, monkeypatch, capsys
):
    monkeypatch.setattr(
        "installation.runtime_approval.Commands",
        lambda: pytest.fail("ambient gh fallback"),
    )
    with pytest.raises(Refusal) as error:
        GitHubVaultReadAPI("private-approval", proxy=lambda *a, **k: response).get(
            "repos/aws-e/adp"
        )
    assert "upstream-private-diagnostics" not in str(error.value)
    assert capsys.readouterr().out == ""


def test_maintained_broker_client_is_lazy_and_error_diagnostics_are_not_exposed(
    monkeypatch, capsys
):
    package, client = ModuleType("adp_cred"), ModuleType("adp_cred.client")

    def unavailable(*a, **k):
        print("private-upstream-body", file=sys.stderr)
        raise SystemExit(1)

    client.proxy_http = unavailable
    monkeypatch.setitem(sys.modules, "adp_cred", package)
    monkeypatch.setitem(sys.modules, "adp_cred.client", client)
    monkeypatch.setattr(
        "installation.runtime_approval.Commands",
        lambda: pytest.fail("ambient gh fallback"),
    )
    with pytest.raises(Refusal, match="unavailable"):
        GitHubVaultReadAPI("private-approval").get("repos/aws-e/adp")
    assert "private-upstream-body" not in capsys.readouterr().err


@pytest.mark.parametrize("label", ["", "../label", "x\n", "a" * 129, None])
def test_explicit_connection_label_is_validated(label):
    with pytest.raises(Refusal):
        GitHubVaultReadAPI(label)


def test_cli_explicit_vault_choice_reaches_real_approval_before_preparation(
    arguments, review_setup, monkeypatch
):
    approval, args, state = review_setup
    arguments.output = approval.directory
    arguments.execute = arguments.resume = True
    arguments.approved_plan_sha256 = args["plan_sha256"]
    arguments.plan_review = "https://github.com/aws-e/adp/pull/42"
    arguments.github_vault_label = "private-approval"
    proxy = through_proxy(approval.api)
    monkeypatch.setattr(
        runtime_cli,
        "GitHubVaultReadAPI",
        lambda label: GitHubVaultReadAPI(label, proxy=proxy),
    )
    called = []

    def prepare(*a, **k):
        called.append(k["approval_check"].verify_plan(**args))
        return json.loads((approval.directory / "runtime-preparation.json").read_text())

    monkeypatch.setattr(runtime_cli.runtime_preparation, "prepare", prepare)
    runtime_cli.run(arguments, commands=object())
    assert called[0]["approved"] is True and state["pull_reads"] == 4


def test_cli_vault_failure_never_reaches_terraform_or_alternate_adapter(
    arguments, review_setup, monkeypatch
):
    approval, args, _ = review_setup
    arguments.output = approval.directory
    arguments.execute = arguments.resume = True
    arguments.approved_plan_sha256 = args["plan_sha256"]
    arguments.plan_review = "https://github.com/aws-e/adp/pull/42"
    arguments.github_vault_label = "private-approval"
    monkeypatch.setattr(
        runtime_cli,
        "GitHubVaultReadAPI",
        lambda label: GitHubVaultReadAPI(
            label, proxy=lambda *a, **k: {"status": 403, "body": "{}"}
        ),
    )
    monkeypatch.setattr(
        runtime_cli.runtime_preparation,
        "prepare",
        lambda *a, **k: pytest.fail("Terraform reached"),
    )
    with pytest.raises(Refusal, match="read failed"):
        runtime_cli.run(arguments, commands=object())
    with pytest.raises(Refusal, match="cannot be overridden"):
        runtime_cli.run(
            arguments, commands=object(), approval_api=SimpleNamespace(get=lambda _: {})
        )
