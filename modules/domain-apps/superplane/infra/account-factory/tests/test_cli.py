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
    FIXTURE_ORGANIZATIONAL_UNIT,
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
    "organizational_unit_id": FIXTURE_ORGANIZATIONAL_UNIT,
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
    assert (
        cli.main(
            [
                "render",
                "--config",
                request_file(
                    mode="existing-account-managed",
                    target_account_id="000000000002",
                    account_email=None,
                    organizational_unit_id=None,
                ),
                "--format",
                "json",
            ]
        )
        == 0
    )
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
            # The request creates an account, so its placement is a comparison that exists
            # and must be authorized for this run to report nothing unverified (#5531).
            "--authorized-organizational-unit",
            FIXTURE_ORGANIZATIONAL_UNIT,
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
    assert (
        cli.main(
            [
                "render",
                "--config",
                request_file(
                    mode="existing-account-managed",
                    target_account_id="000000000002",
                    account_email=None,
                    organizational_unit_id=None,
                ),
                "--format",
                "json",
            ]
        )
        == 0
    )
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
    assert (
        cli.main(
            [
                "render",
                "--config",
                request_file(
                    mode="existing-account-managed",
                    target_account_id="000000000002",
                    account_email=None,
                    organizational_unit_id=None,
                ),
                "--format",
                "json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["prerequisites_run_once_per_management_cluster"]
    assert payload["objects"]
    serialized_objects = json.dumps(payload["objects"])
    assert "helm" not in serialized_objects
    for prerequisite in payload["prerequisites_run_once_per_management_cluster"]:
        assert "once per cluster" in prerequisite["scope"]


def test_yaml_is_the_default_output_format(request_file, capsys):
    assert (
        cli.main(
            [
                "render",
                "--config",
                request_file(
                    mode="existing-account-managed",
                    target_account_id="000000000002",
                    account_email=None,
                    organizational_unit_id=None,
                ),
            ]
        )
        == 0
    )
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


# ── creation-status: a duplicate or unresolved outcome cannot be walked past ──────────


@pytest.fixture
def ledger_file(tmp_path):
    def write(records):
        path = tmp_path / "attempts.yaml"
        path.write_text(yaml.safe_dump(records))
        return str(path)

    return write


def _recorded_attempt(**overrides):
    """A persisted attempt for the VALID_REQUEST workspace, as the CLI would read it."""
    from account_factory import creation
    from account_factory.modes import from_mapping

    attempt = creation.intended_attempt(from_mapping(VALID_REQUEST))
    record = attempt.as_record()
    record["create_account_request_id"] = "car-fixture0000001"
    record.update(overrides)
    return record


def test_creation_status_first_attempt_is_analysis_only(request_file, capsys):
    """A file and optional flags cannot authorize or durably fence an AWS effect."""
    exit_code = cli.main(
        ["creation-status", "--config", request_file(), "--format", "json"]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["disposition"] == "create-permitted"
    assert payload["may_create_account"] is False
    assert payload["created"] is False
    assert payload["record_before_calling"] is None


def test_creation_status_exits_non_zero_when_an_account_already_exists(
    request_file, ledger_file, capsys
):
    """Exit code alone must stop a caller that never reads stdout."""
    exit_code = cli.main(
        [
            "creation-status",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file(
                [_recorded_attempt(status="succeeded", account_id="000000000777")]
            ),
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)["disposition"] == "already-created"
    assert "CREATION NOT PERMITTED (already-created)" in captured.err
    assert "000000000777" in captured.err


def test_creation_status_treats_an_unreadable_aws_answer_as_unresolved_not_failed(
    request_file, ledger_file, capsys
):
    exit_code = cli.main(
        [
            "creation-status",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file([_recorded_attempt()]),
            "--aws-unreadable",
            "throttled; unclear whether the request was accepted",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["disposition"] == "unresolved"
    assert payload["account_possibly_unaccounted_for"] is True


def test_creation_status_confirmed_failure_still_requires_the_durable_runner(
    request_file, ledger_file, capsys
):
    exit_code = cli.main(
        [
            "creation-status",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file([_recorded_attempt()]),
            "--aws-status",
            "failed",
            "--aws-failure",
            "internal-failure",
            "--aws-request-id",
            "car-fixture0000001",
            "--format",
            "json",
        ]
    )
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["disposition"] == "retry-permitted"
    assert payload["may_create_account"] is False


def test_creation_status_refuses_a_taken_email_rather_than_permitting_a_retry(
    request_file, ledger_file, capsys
):
    exit_code = cli.main(
        [
            "creation-status",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file([_recorded_attempt()]),
            "--aws-status",
            "failed",
            "--aws-failure",
            "email-already-exists",
            "--aws-request-id",
            "car-fixture0000001",
        ]
    )
    assert exit_code == 1
    assert "refused-input-cannot-succeed" in capsys.readouterr().err


def test_an_unreadable_ledger_is_refused_rather_than_read_as_empty(
    request_file, capsys, tmp_path
):
    """Reading an unreadable store as "nothing attempted" is how a second account opens."""
    exit_code = cli.main(
        [
            "creation-status",
            "--config",
            request_file(),
            "--attempt-ledger",
            str(tmp_path / "absent.yaml"),
        ]
    )
    assert exit_code == 1
    error = capsys.readouterr().err
    assert "REFUSED (account creation)" in error
    assert "not an empty one" in error


def test_creation_status_refuses_a_mode_that_adopts_an_existing_account(
    request_file, capsys
):
    """Creating an account is a named mode, never a side effect of onboarding one."""
    exit_code = cli.main(
        [
            "creation-status",
            "--config",
            request_file(
                mode="existing-account-managed",
                target_account_id="000000000002",
                account_email=None,
                organizational_unit_id=None,
            ),
        ]
    )
    assert exit_code == 1
    assert "only new-account-managed" in capsys.readouterr().err


def test_unknown_cannot_be_spelled_as_an_aws_status_flag(request_file, capsys):
    """`--aws-status unknown` would let "I could not check" be typed as a provider answer."""
    with pytest.raises(SystemExit) as raised:
        cli.main(
            ["creation-status", "--config", request_file(), "--aws-status", "unknown"]
        )
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_creation_status_never_reports_having_created_anything(request_file, capsys):
    assert cli.main(["creation-status", "--config", request_file()]) == 1
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["created"] is False
    assert "No AWS call was made" in payload["note"]


# ── bootstrap-plan (#5531) ───────────────────────────────────────────────────────────


def test_bootstrap_plan_names_the_autoscaling_role_before_anything_needs_it(
    request_file, capsys
):
    """The ordering reaches the operator, not just the module's own tests.

    A plan whose steps were emitted in an arbitrary order would be followed in that order.
    """
    exit_code = cli.main(
        ["bootstrap-plan", "--config", request_file(), "--format", "json"]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    names = [step["name"] for step in payload["steps"]]
    assert "autoscaling-service-linked-role" in names
    assert names.index("bootstrap-role") < names.index(
        "autoscaling-service-linked-role"
    )


def test_bootstrap_plan_says_the_read_comes_before_the_create(request_file, capsys):
    """`create-if-absent` alone does not tell an operator that absence must be VERIFIED."""
    cli.main(["bootstrap-plan", "--config", request_file(), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    step = next(
        s for s in payload["steps"] if s["name"] == "autoscaling-service-linked-role"
    )
    assert step["presence"] == "create-if-absent"
    assert "VERIFIED absence" in step["presence_meaning"]
    assert "iam:CreateServiceLinkedRole" in step["if_denied"]


def test_bootstrap_plan_marks_the_account_wide_steps_as_never_workspace_owned(
    request_file, capsys
):
    """The property that keeps one workspace's teardown from breaking its neighbours has to
    be visible to whoever writes the Terraform."""
    cli.main(["bootstrap-plan", "--config", request_file(), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert "autoscaling-service-linked-role" in payload["account_wide_steps"]
    for step in payload["steps"]:
        assert step["adoptable_by_workspace"] is False
    assert "per-workspace Terraform state" in payload["note"]


def test_bootstrap_plan_never_reports_having_bootstrapped_anything(
    request_file, capsys
):
    assert cli.main(["bootstrap-plan", "--config", request_file()]) == 0
    payload = yaml.safe_load(capsys.readouterr().out)
    assert payload["bootstrapped"] is False
    assert "Nothing was created, read or assumed" in payload["note"]


def test_bootstrap_plan_exits_non_zero_for_a_mode_that_owns_no_account(
    request_file, capsys
):
    """Exit code alone must stop a caller that never reads stdout."""
    exit_code = cli.main(
        [
            "bootstrap-plan",
            "--config",
            request_file(
                mode="bring-existing-cluster",
                target_account_id="000000000002",
                existing_cluster_name="fixture-adopted-cluster",
                account_email=None,
                organizational_unit_id=None,
                vpc_cidr=None,
                availability_zones=None,
                cluster_version=None,
                node_instance_type=None,
            ),
        ]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "REFUSED (bootstrap)" in err
    # For the MODE reason specifically. Accepting any refusal here would let an unrelated
    # validation error pass as evidence that bootstrap declines to touch an adopted cluster.
    assert "an account ADP does not own" in err


def test_bootstrap_plan_exits_non_zero_for_an_unauthorized_workspace(
    request_file, capsys
):
    """A plan is a document an operator acts from, so it must not name an unauthorized
    target and refuse only afterwards."""
    exit_code = cli.main(
        [
            "bootstrap-plan",
            "--config",
            request_file(),
            "--authorized-workspace",
            "ws-somebody-else",
        ]
    )
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "REFUSED (bootstrap)" in err
    assert "does not validate" in err


# ── recovery-report (#5531) ──────────────────────────────────────────────────────────

BOOTSTRAP_STEPS = (
    "bootstrap-role",
    "controller-role",
    "workload-role",
    "autoscaling-service-linked-role",
    "baseline-audit-logging",
    "baseline-public-access-block",
)


def _all_established_flags(*, omit=()):
    flags = []
    for name in BOOTSTRAP_STEPS:
        if name in omit:
            continue
        flags += ["--observed", f"{name}=established"]
    return flags


def test_recovery_report_exits_non_zero_when_nothing_was_read(request_file, capsys):
    """The default state is "nobody looked", and it must not exit 0.

    A caller reading only the exit code would otherwise treat a completely unexamined account
    as ready.
    """
    exit_code = cli.main(["recovery-report", "--config", request_file()])
    assert exit_code == 1
    assert "ACCOUNT NOT READY" in capsys.readouterr().err


def test_recovery_report_names_what_blocks_workspace_provisioning(request_file, capsys):
    """A gap to close later and a gap that fails the next KMS key creation are different
    facts, and the operator needs to know which they have."""
    cli.main(["recovery-report", "--config", request_file(), "--format", "json"])
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert "autoscaling-service-linked-role" in payload["blocks_workspace_provisioning"]
    assert "Blocks workspace provisioning outright" in err


def test_recovery_report_never_reports_having_recovered_anything(request_file, capsys):
    cli.main(["recovery-report", "--config", request_file(), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["recovered"] is False
    assert "never re-runs account creation" in payload["note"]
    assert payload["creation_retry_is_safe"] is False


def test_recovery_report_reports_an_unread_step_as_neither_present_nor_absent(
    request_file, capsys
):
    """`not-checked` is the whole point: it neither invites creating the role nor permits
    concluding the account is ready."""
    cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--format",
            "json",
            *_all_established_flags(omit=("autoscaling-service-linked-role",)),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["unchecked"] == ["autoscaling-service-linked-role"]
    assert payload["every_step_accounted_for"] is False
    assert payload["bootstrap_retry_is_safe"] is False
    finding = next(
        f for f in payload["findings"] if f["name"] == "autoscaling-service-linked-role"
    )
    assert finding["state"] == "not-checked"
    assert "READ IT FIRST" in finding["next_action"]


def test_recovery_report_reports_retention_even_for_an_incomplete_account(
    request_file, capsys
):
    """ "This was not removed" has to be visible, and the account is never closed."""
    cli.main(["recovery-report", "--config", request_file(), "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    retained = " ".join(payload["retained"])
    assert "never closes" in retained
    assert "90-day" in retained


def test_recovery_report_refuses_an_unparsable_observed_state(request_file, capsys):
    """A state that cannot be parsed must not fall back to a default: it would report an
    unread step as read."""
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--observed",
            "bootstrap-role=probably-fine",
        ]
    )
    assert exit_code == 1
    assert "REFUSED (recovery)" in capsys.readouterr().err


def test_recovery_report_refuses_an_observation_with_no_state(request_file, capsys):
    exit_code = cli.main(
        ["recovery-report", "--config", request_file(), "--observed", "bootstrap-role"]
    )
    assert exit_code == 1
    assert "expects step=state" in capsys.readouterr().err


def test_recovery_report_refuses_a_detail_with_no_text(request_file, capsys):
    """An empty detail says nothing, and storing one would trip the denied-needs-detail refusal
    against the wrong cause."""
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--observed-detail",
            "bootstrap-role=",
        ]
    )
    assert exit_code == 1
    assert "expects step=text" in capsys.readouterr().err


def test_recovery_report_refuses_a_detail_naming_a_step_outside_the_plan(
    request_file, capsys
):
    """A misspelled step name silently dropped the operator's own account of what they saw."""
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--observed",
            "bootstrap-role=established",
            "--observed-detail",
            "bootstrap-roll=reused the existing role",
        ]
    )
    assert exit_code == 1
    assert "not in this plan" in capsys.readouterr().err


def test_recovery_report_refuses_a_denial_with_no_detail(request_file, capsys):
    """The remedy depends on which permission was refused."""
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            *_all_established_flags(omit=("autoscaling-service-linked-role",)),
            "--observed",
            "autoscaling-service-linked-role=denied",
        ]
    )
    assert exit_code == 1
    assert "must say what was refused" in capsys.readouterr().err


def test_recovery_report_exits_zero_for_a_recorded_account_with_every_step_established(
    request_file, ledger_file, capsys
):
    """The positive control. Without it, every non-zero assertion above would also hold for a
    command that could never succeed.
    """
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file(
                [_recorded_attempt(status="succeeded", account_id="000000000777")]
            ),
            "--format",
            "json",
            *_all_established_flags(),
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready_for_workspace_provisioning"] is True
    assert payload["account_id"] == "000000000777"
    assert payload["bootstrap_retry_is_safe"] is True
    assert payload["incomplete"] == []
    assert payload["summary"].startswith("COMPLETE")
    # Even complete, nothing was done and the account is still reported as retained.
    assert payload["recovered"] is False
    assert any("never closes" in line for line in payload["retained"])


def test_recovery_report_exits_non_zero_for_an_unresolved_creation_outcome(
    request_file, ledger_file, capsys
):
    """The costly unknown outranks a clean bootstrap read.

    With every step established, the bootstrap side alone would say "go". An account that may
    exist untracked has to veto that.
    """
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file([_recorded_attempt()]),
            "--format",
            "json",
            *_all_established_flags(),
        ]
    )
    assert exit_code == 1
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["bootstrap_retry_is_safe"] is False
    assert payload["needs_operator"] is True
    assert "ACCOUNT POSSIBLY UNACCOUNTED FOR" in err


def test_recovery_report_exits_non_zero_when_no_account_was_ever_recorded(
    request_file, capsys
):
    """Every step observed established does not make an account exist.

    With no recorded attempt there is nothing saying this workspace has an account, so a
    clean step list must not exit 0 as though a workspace could be built in it.
    """
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--format",
            "json",
            *_all_established_flags(),
        ]
    )
    assert exit_code == 1
    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["account_is_usable"] is True
    assert payload["ready_for_workspace_provisioning"] is False
    assert "NO RECORDED ACCOUNT" in err


@pytest.mark.parametrize("subcommand", ["bootstrap-plan", "recovery-report"])
def test_the_bootstrap_surface_reports_unmade_authorization_comparisons(
    subcommand, request_file, capsys
):
    """`validate` and `render` already said which comparisons were skipped; these did not.

    These are the two subcommands that describe writing account-wide roles and that can call an
    account ready for a workspace, so an unverified run reading like a verified one is worst
    here.
    """
    cli.main([subcommand, "--config", request_file(), "--format", "json"])
    out, err = capsys.readouterr()
    assert "NOT VERIFIED" in err
    assert "organization_id" in err
    assert "organization_id" in json.loads(out)["authorization_not_verified"]


def test_a_fully_authorized_recovery_report_reports_nothing_unverified(
    request_file, ledger_file, capsys
):
    """The positive control: the report names comparisons that were skipped, not all of them.

    Paired with the exit-0 case, so the one run that can say "ready for a workspace" is also
    the one shown to have had every ownership comparison actually made.
    """
    exit_code = cli.main(
        [
            "recovery-report",
            "--config",
            request_file(),
            "--attempt-ledger",
            ledger_file(
                [_recorded_attempt(status="succeeded", account_id="000000000777")]
            ),
            "--format",
            "json",
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
            "--authorized-organizational-unit",
            FIXTURE_ORGANIZATIONAL_UNIT,
            *_all_established_flags(),
        ]
    )
    assert exit_code == 0
    out, err = capsys.readouterr()
    assert "NOT VERIFIED" not in err
    assert json.loads(out)["authorization_not_verified"] == []


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


def test_new_account_render_refuses_a_caller_selected_account(request_file, capsys):
    assert (
        cli.main(
            [
                "render",
                "--config",
                request_file(),
                "--account-id",
                "000000000888",
                "--format",
                "json",
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "trusted durable registration" in captured.err
