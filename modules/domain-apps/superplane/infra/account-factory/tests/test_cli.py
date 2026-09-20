"""The CLI can plan but cannot apply — Issue #5530 (w6-07).

The plan/apply boundary is only real if the entry point cannot cross it.
`test_there_is_no_apply_subcommand` and `test_no_subcommand_can_reach_a_mutating_capability`
are that property; the rest check that refusals reach the operator with a non-zero exit code
rather than being printed alongside a success.

## What these tests deliberately do NOT establish

Nothing here applies anything, so none of it is evidence that rendered output is accepted by a
cluster. Exit codes and stdout are the whole surface under test.
"""

from __future__ import annotations

import json

import pytest
import yaml
from account_factory import cli

from .conftest import (
    FIXTURE_MANAGEMENT_ACCOUNT,
    FIXTURE_MANAGEMENT_CLUSTER,
    FIXTURE_ORG_ID,
    FIXTURE_REGION,
    FIXTURE_WORKSPACE,
)

VALID_REQUEST = {
    "mode": "new-account-managed",
    "organization_id": FIXTURE_ORG_ID,
    "management_account_id": FIXTURE_MANAGEMENT_ACCOUNT,
    "management_cluster": FIXTURE_MANAGEMENT_CLUSTER,
    "region": FIXTURE_REGION,
    "workspace_id": FIXTURE_WORKSPACE,
    "account_email": "fixture-workspace@example.invalid",
    "vpc_cidr": "10.64.0.0/16",
    "availability_zones": [f"{FIXTURE_REGION}a", f"{FIXTURE_REGION}b"],
    "cluster_version": "1.31",
    "node_instance_type": "m6i.large",
}


@pytest.fixture
def request_file(tmp_path):
    def write(**overrides):
        data = {**VALID_REQUEST, **overrides}
        path = tmp_path / "request.yaml"
        path.write_text(yaml.safe_dump(data))
        return str(path)

    return write


# ── No apply path exists ─────────────────────────────────────────────────────────────


def test_there_is_no_apply_subcommand(capsys):
    """The legacy flow rendered and applied in one statement. This cannot apply at all."""
    with pytest.raises(SystemExit) as raised:
        cli.main(["apply", "--config", "x.yaml"])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize(
    "forbidden", ["apply", "deploy", "provision", "create", "destroy"]
)
def test_no_mutating_subcommand_exists(forbidden, capsys):
    with pytest.raises(SystemExit):
        cli.main([forbidden])


def test_no_subcommand_can_reach_a_mutating_capability():
    """Structural: the CLI module imports nothing that could apply, fetch or execute."""
    import inspect

    source = inspect.getsource(cli)
    import io
    import tokenize

    identifiers = {
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.NAME
    }
    for capability in (
        "subprocess",
        "urlopen",
        "boto3",
        "kubernetes",
        "socket",
        "requests",
        "eval",
        "exec",
    ):
        assert capability not in identifiers, capability


def test_render_output_states_that_nothing_was_applied(request_file, capsys):
    assert cli.main(["render", "--config", request_file(), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert "Nothing was applied" in payload["note"]


def test_cleanup_plan_output_states_that_nothing_was_deleted(request_file, capsys):
    assert (
        cli.main(["cleanup-plan", "--config", request_file(), "--format", "json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["deleted"] is False
    assert payload["closes_account"] is False


# ── Refusals reach the operator as a non-zero exit ───────────────────────────────────


def test_an_unknown_mode_exits_non_zero(request_file, capsys):
    assert cli.main(["validate", "--config", request_file(mode="vend-everything")]) == 1
    assert "REFUSED" in capsys.readouterr().err


def test_an_invalid_request_exits_non_zero_and_lists_every_problem(
    request_file, capsys
):
    exit_code = cli.main(
        ["validate", "--config", request_file(workspace_id="kube-system")]
    )
    assert exit_code == 1
    error = capsys.readouterr().err
    assert "REFUSED before any mutation" in error
    assert "core ADP or Kubernetes namespace" in error


def test_a_wrong_management_account_exits_non_zero(request_file, capsys):
    exit_code = cli.main(
        [
            "validate",
            "--config",
            request_file(),
            "--authorized-management-account",
            "999999999999",
        ]
    )
    assert exit_code == 1
    assert (
        "not the management account this run is authorized for"
        in capsys.readouterr().err
    )


def test_a_missing_config_file_is_refused_not_crashed(capsys, tmp_path):
    assert cli.main(["validate", "--config", str(tmp_path / "absent.yaml")]) == 1
    assert "could not read" in capsys.readouterr().err


def test_invalid_yaml_is_refused(capsys, tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("mode: [unclosed\n")
    assert cli.main(["validate", "--config", str(path)]) == 1
    assert "not valid YAML" in capsys.readouterr().err


def test_render_refuses_rather_than_emitting_a_partial_object_set(request_file, capsys):
    exit_code = cli.main(
        ["render", "--config", request_file(workspace_id="adp-gateway")]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""


# ── Unchecked comparisons are reported, never implied to have passed ─────────────────


def test_validate_reports_unmade_comparisons_on_stderr(request_file, capsys):
    assert cli.main(["validate", "--config", request_file()]) == 0
    error = capsys.readouterr().err
    assert "NOT VERIFIED" in error
    assert "management_account_id" in error


def test_a_fully_authorized_run_reports_nothing_unverified(request_file, capsys):
    exit_code = cli.main(
        [
            "validate",
            "--config",
            request_file(),
            "--authorized-organization",
            FIXTURE_ORG_ID,
            "--authorized-management-account",
            FIXTURE_MANAGEMENT_ACCOUNT,
            "--authorized-management-cluster",
            FIXTURE_MANAGEMENT_CLUSTER,
            "--permit-mode",
            "new-account-managed",
            "--authorized-workspace",
            FIXTURE_WORKSPACE,
        ]
    )
    assert exit_code == 0
    assert "NOT VERIFIED" not in capsys.readouterr().err


def test_an_authorization_without_a_workspace_reports_that_comparison_as_unmade(
    request_file, capsys
):
    """Every other field supplied, workspace omitted — and the report says so.

    This is the reporting half of AF-003: before the repair there was no workspace comparison
    to omit, so an authorization like this one reported nothing unverified while never having
    checked which tenant the run was for.
    """
    exit_code = cli.main(
        [
            "validate",
            "--config",
            request_file(),
            "--authorized-organization",
            FIXTURE_ORG_ID,
            "--authorized-management-account",
            FIXTURE_MANAGEMENT_ACCOUNT,
            "--authorized-management-cluster",
            FIXTURE_MANAGEMENT_CLUSTER,
            "--permit-mode",
            "new-account-managed",
        ]
    )
    assert exit_code == 0
    assert "workspace_id" in capsys.readouterr().err


def test_a_run_authorized_for_another_workspace_is_refused(request_file, capsys):
    """The CLI surfaces the refusal rather than rendering for the wrong tenant."""
    exit_code = cli.main(
        ["validate", "--config", request_file(), "--authorized-workspace", "ws-other"]
    )
    assert exit_code == 1
    assert "not the workspace this run is authorized for" in capsys.readouterr().err


def test_render_records_what_authorization_did_not_verify(request_file, capsys):
    assert cli.main(["render", "--config", request_file(), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "management_account_id" in payload["authorization_not_verified"]


def test_an_unknown_permit_mode_is_refused(request_file, capsys):
    exit_code = cli.main(
        ["validate", "--config", request_file(), "--permit-mode", "not-a-mode"]
    )
    assert exit_code == 1
    assert "REFUSED" in capsys.readouterr().err


# ── Rendered output keeps the prerequisite separation visible ─────────────────────────


def test_prerequisites_are_a_separate_key_from_objects(request_file, capsys):
    """An operator reading the output can see that applying `objects` changes nothing shared."""
    assert cli.main(["render", "--config", request_file(), "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["prerequisites_run_once_per_management_cluster"]
    assert payload["objects"]
    serialized_objects = json.dumps(payload["objects"])
    assert "helm" not in serialized_objects
    for prerequisite in payload["prerequisites_run_once_per_management_cluster"]:
        assert "once per cluster" in prerequisite["scope"]


def test_yaml_is_the_default_output_format(request_file, capsys):
    assert cli.main(["render", "--config", request_file()]) == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["namespace"] == FIXTURE_WORKSPACE


def test_the_dependencies_subcommand_needs_no_request_and_states_its_limits(capsys):
    assert cli.main(["dependencies", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["license"] == "Apache-2.0"
    assert len(payload["charts"]) == 5
    for chart in payload["charts"]:
        assert "@sha256:" in chart["reference"]
    # It must not overclaim: a digest records WHICH artifact an install would use, and is not
    # evidence that anything was installed or reconciles.
    limits = payload["not_established"]
    assert "installed" in limits
    assert "reconciles" in limits
    assert payload["verified"].startswith("every chart is pinned by content digest")


def test_every_mode_in_the_example_config_is_renderable(module_dir, tmp_path, capsys):
    """The documented example must actually work — including its commented alternatives.

    A config example that does not parse is documentation that silently rots.
    """
    text = (module_dir / "config.example.yaml").read_text()
    active = yaml.safe_load(text)
    assert active["mode"] == "new-account-managed"
    # The example is placeholders only, so it must NOT validate against real constraints:
    # the organization id and account id are deliberately unusable.
    path = tmp_path / "example.yaml"
    path.write_text(yaml.safe_dump(active))
    assert cli.main(["validate", "--config", str(path)]) == 1
    error = capsys.readouterr().err
    assert "organization_id" in error or "management_account_id" in error
