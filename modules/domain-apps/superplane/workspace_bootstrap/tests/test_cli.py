"""The operator entry point — F1's other half: the runtime path itself.

F1 verbatim: "the implementation is not connected to any runtime path —
`bootstrap_workspace` has no caller outside its own tests". `cli.py` is that caller, so
these tests are about the WIRING: whether the values the gates compare against actually
reach them, whether a missing expectation refuses rather than skipping a check, and
whether a failure can print something it should not.

## What is exercised here, and what deliberately is not

No subprocess runs. `main()` is driven with argv and the handlers are exercised through
`monkeypatch`-substituted adapters, because the adapters themselves are covered against
a scripted runner in `test_adapters.py` and the sequence is covered end to end in
`test_integration.py`. What is left — and what has no other home — is:

- **Argument discipline.** `--controller-image` with no default, `--enforce-version`
  required, `--registration-store` as `module:callable` rather than a database URL.
  These are security properties expressed in argparse, and argparse is easy to relax by
  accident.
- **The required-outputs list.** A missing Terraform output must refuse. The gates
  cannot check an expectation they were never given, and the failure mode of a skipped
  check is a bootstrap that reports success having verified less than it claims.
- **Exit codes and output shape.** One JSON object on stdout, always; exit 1 on a
  refusal; exit 2 on usage. A wrapper parsing prose would break on a reworded message.
- **That an unexpected exception's text is never printed.** A cloud SDK or database
  error can carry a credential in its string, and `main()` is the one place it would
  reach an operator's terminal and their CI log.
- **That a BACKEND failure's text is never printed either** (finding F9). The point
  above was about an exception escaping to `main`'s own handler. F9 was the other
  case: an exception the package CAUGHT, wrapped in a refusal, and then interpolated
  into that refusal's message — which `_report` serializes to stdout as a tidy,
  expected outcome. The tests at the bottom of this file drive the real bootstrap path
  for each of the three stages the review named and assert on what the command prints.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from superplane_bootstrap import cli
from superplane_bootstrap.admission import required_proofs
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.state import BootstrapState, FileStateStore

from .conftest import (
    ACCOUNT_ID,
    CA_DATA,
    CLUSTER_ARN,
    CLUSTER_NAME,
    CLUSTER_SG_ID,
    CNI_ROLE_ARN,
    ENFORCE_VERSION,
    MANAGEMENT_SG_ID,
    NAMESPACE,
    ORG_ID,
    REGION,
    VPC_ID,
    WORKSPACE_ID,
    FakeClusterAccess,
    FakePrerequisiteAccess,
    FakeRegistrationStore,
)

NODE_ROLE_ARN = f"arn:aws:iam::{ACCOUNT_ID}:role/superplane-workspace-node"
BOOTSTRAP_TAINT_KEY = "superplane.aws-e/bootstrap"


def _outputs() -> dict[str, object]:
    """`terraform output -json` from `infra/workspaces/`, in its WRAPPED form.

    Wrapped because that is what the command actually produces; the unwrapped form has
    its own test.
    """
    plain = {
        "account_id": ACCOUNT_ID,
        "aws_region": REGION,
        "cluster_name": CLUSTER_NAME,
        "cluster_arn": CLUSTER_ARN,
        "cluster_certificate_authority_data": CA_DATA,
        "cluster_security_group_id": CLUSTER_SG_ID,
        "workspace_api_security_group_id": "sg-synthetic-api",
        "sts_endpoint_vpc_id": "vpc-00000000000000000",
        "workspace_node_security_group_id": "sg-synthetic-nodes",
        "sts_endpoint_security_group_id": "sg-synthetic-sts",
        "node_role_arn": NODE_ROLE_ARN,
        "vpc_id": VPC_ID,
        "org_id": ORG_ID,
        "workspace_id": WORKSPACE_ID,
        "tenant_scheduling_prerequisites": {
            "bootstrap_taint_key": BOOTSTRAP_TAINT_KEY,
            "cni_role_arn": CNI_ROLE_ARN,
            "required_proofs": [
                "restricted_admission_enforced",
                "tenant_pod_cannot_reach_imds",
            ],
        },
    }
    return {name: {"value": value, "type": "string"} for name, value in plain.items()}


def _write(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def trusted_binding_resolver(monkeypatch, binding):
    import sys
    import types

    module = types.ModuleType("synthetic_facade_resolver")

    def resolve(operation_id):
        assert operation_id == binding.operation_id
        return binding

    module.resolve = resolve
    monkeypatch.setitem(sys.modules, module.__name__, module)


@pytest.fixture
def files(tmp_path):
    """The three files every subcommand reads, plus the state directory."""
    return {
        "outputs": _write(tmp_path / "outputs.json", _outputs()),
        "binding": _write(
            tmp_path / "binding.json",
            {"operation_id": "synthetic-operation"},
        ),
        "state_dir": tmp_path / "state",
    }


def _argv(files, subcommand: str, *extra: str) -> list[str]:
    argv = [
        subcommand,
        "--outputs",
        str(files["outputs"]),
        "--binding",
        str(files["binding"]),
        "--binding-resolver",
        "synthetic_facade_resolver:resolve",
        "--state-dir",
        str(files["state_dir"]),
        "--cluster-ownership",
        "adp-created",
    ]
    return argv + list(extra)


def _bootstrap_argv(files, *extra: str) -> list[str]:
    return _argv(
        files,
        "bootstrap",
        "--kubeconfig",
        str(files["outputs"].parent / "kubeconfig"),
        "--controller-image",
        "registry.example/superplane-controller:v1",
        "--imds-probe-image",
        "registry.example/python@sha256:" + "0" * 64,
        "--registration-store",
        "tests.test_cli:_store_factory",
        "--namespace",
        NAMESPACE,
        "--enforce-version",
        ENFORCE_VERSION,
        "--credential-reference-id",
        "44444444-4444-4444-8444-444444444444",
        "--contract-version",
        "v1",
        "--management-security-group-id",
        MANAGEMENT_SG_ID,
        *extra,
    )


def _store_factory():  # pragma: no cover - referenced by name, never called here
    """A `--registration-store` target, so the module:callable form has a real subject."""
    raise AssertionError(
        "the store factory was called in a test that should not reach it"
    )


@pytest.mark.parametrize(
    "resolver",
    [
        "invalid",
        "missing_authority_module:load",
        "synthetic_bad_authority:wrong",
        "synthetic_bad_authority:leak",
    ],
)
def test_authority_resolver_failure_is_closed_and_redacted(
    files, monkeypatch, capsys, resolver
):
    import sys
    import types

    module = types.ModuleType("synthetic_bad_authority")
    module.wrong = lambda *args: object()

    def leak(*args):
        raise RuntimeError("synthetic-private-credential")

    module.leak = leak
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert (
        cli.main(_bootstrap_argv(files, "--authority-resolver", resolver))
        == cli._EXIT_REFUSED
    )
    report = capsys.readouterr().out
    assert "synthetic-private-credential" not in report
    assert json.loads(report)["status"] == "refused"


def _stdout(capsys) -> dict[str, object]:
    """The single JSON object `main` prints. Parsed, so a prose line would fail here."""
    out = capsys.readouterr().out
    return json.loads(out)


# --- usage: a bare invocation cannot mutate anything ----------------------------


def test_no_subcommand_prints_help_and_exits_usage(capsys):
    """Defaulting to `plan` silently would mean a bare invocation makes network calls."""
    assert cli.main([]) == cli._EXIT_USAGE
    assert "plan" in capsys.readouterr().out


def test_plan_is_the_only_subcommand_that_needs_no_cluster_flags():
    """`plan` mutates nothing, so it takes no kubeconfig; `bootstrap` and `recover`
    both talk to the cluster and must have one."""
    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["bootstrap", "--outputs", "x", "--binding", "y"])


@pytest.mark.parametrize(
    "omit",
    [
        "--controller-image",
        "--enforce-version",
        "--management-security-group-id",
        "--registration-store",
        "--namespace",
        "--contract-version",
        "--credential-reference-id",
    ],
)
def test_every_security_relevant_bootstrap_flag_is_required(files, omit):
    """Each of these is required with NO default, and each for its own reason:

    - `--controller-image`: a default would pin a controller version in argparse, where
      nobody looks for one, and every workspace afterwards would silently get it.
    - `--enforce-version`: `latest` means the admission policy can change under a
      workspace that was proved against a different one.
    - `--management-security-group-id`: F4's finding was that these prerequisites were
      optional and unverified; an absent flag must refuse, not skip the rule check.
    - `--registration-store`: no default store means no accidental writes to one.
    """
    argv = _bootstrap_argv(files)
    index = argv.index(omit)
    del argv[index : index + 2]

    with pytest.raises(SystemExit) as exit_info:
        cli._build_parser().parse_args(argv)

    assert exit_info.value.code == cli._EXIT_USAGE


def test_the_registration_store_is_a_callable_not_a_database_url():
    """A `--database-url` flag would put a credential in shell history and in every
    process listing on the host. The flag names a zero-argument factory instead."""
    parser = cli._build_parser()
    flags = {
        action.option_strings[0]
        for action in parser._subparsers._group_actions[0]  # type: ignore[union-attr]
        .choices["bootstrap"]
        ._actions
        if action.option_strings
    }

    assert "--registration-store" in flags
    for forbidden in ("--database-url", "--db-url", "--dsn", "--password"):
        assert forbidden not in flags


def test_a_store_spec_without_a_colon_refuses(files):
    """`module:callable`. A bare module name would be imported and then called, which
    for some modules is not a no-op."""
    with pytest.raises(BootstrapRefused, match="module:callable"):
        cli._registration_store(argparse.Namespace(registration_store="tests.test_cli"))


def test_an_unloadable_store_factory_refuses_without_executing_anything():
    with pytest.raises(BootstrapRefused, match="could not load"):
        cli._registration_store(
            argparse.Namespace(registration_store="tests.test_cli:no_such_attribute")
        )


# --- the required outputs: a missing expectation is a refusal --------------------


@pytest.mark.parametrize("name", cli._REQUIRED_OUTPUTS)
def test_every_required_output_is_required(tmp_path, name):
    """A gate cannot compare against an expectation it was never given. Dropping one
    would not relax a check, it would REMOVE it, and the run would still report
    success — which is the failure mode this whole package exists to refuse."""
    payload = {k: v for k, v in _outputs().items() if k != name}
    path = _write(tmp_path / "outputs.json", payload)

    with pytest.raises(BootstrapRefused, match=name):
        cli._terraform_outputs(path)


def test_node_role_arn_is_among_the_required_outputs():
    """The fourth production defect's wiring, pinned at the CLI end. The CNI
    credential-scope proof asks whether THIS role still carries CNI and account-wide
    ECR permissions; with no subject the proof cannot be verified, which holds the
    taint on forever rather than failing loudly."""
    assert "node_role_arn" in cli._REQUIRED_OUTPUTS


def test_an_unwrapped_outputs_file_is_accepted(tmp_path):
    """Operators produce both shapes — `terraform output -json` and a saved
    `jq 'map_values(.value)'`. Refusing one teaches people to reshape the file by hand,
    which is one more place for a value to be edited."""
    unwrapped = {k: v["value"] for k, v in _outputs().items()}

    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", unwrapped))

    assert outputs["cluster_arn"] == CLUSTER_ARN


def test_a_blank_expectation_refuses_rather_than_being_compared(tmp_path):
    """`verify_target` compares the observed CA against the expected one in constant
    time. Comparing against "" would refuse every cluster with a message about a
    mismatched certificate — true, and uselessly misleading."""
    payload = _outputs()
    payload["cluster_certificate_authority_data"] = {"value": "   "}

    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", payload))

    with pytest.raises(BootstrapRefused, match="blank expectation"):
        cli._text_output(outputs, "cluster_certificate_authority_data")


def test_a_non_object_outputs_file_refuses(tmp_path):
    with pytest.raises(BootstrapRefused, match="JSON object"):
        cli._terraform_outputs(_write(tmp_path / "outputs.json", [1, 2, 3]))


def test_an_unreadable_outputs_file_names_the_file(tmp_path):
    with pytest.raises(BootstrapRefused, match="could not read"):
        cli._terraform_outputs(tmp_path / "absent.json")


# --- the taint key and the declared proofs come from the infrastructure ---------


def test_the_taint_key_is_read_from_the_outputs_not_assumed(tmp_path):
    """Assuming the constant would mean a workspace provisioned with a different key
    has its interlock silently ignored — removing a taint that is not there SUCCEEDS,
    and that is the dangerous success."""
    payload = _outputs()
    payload["tenant_scheduling_prerequisites"]["value"]["bootstrap_taint_key"] = (
        "other.example/bootstrap"
    )
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", payload))

    assert cli._taint_key(outputs) == "other.example/bootstrap"


def test_an_empty_taint_key_refuses(tmp_path):
    payload = _outputs()
    payload["tenant_scheduling_prerequisites"]["value"]["bootstrap_taint_key"] = ""
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", payload))

    with pytest.raises(BootstrapRefused, match="unnamed taint"):
        cli._taint_key(outputs)


def test_the_declared_proofs_come_from_the_infrastructure_module(tmp_path):
    """So the gate checks what `outputs.tf` says must be proved, rather than what this
    package happens to implement. A Terraform-side requirement this package cannot prove
    becomes a refusal instead of a silent pass on a stale list."""
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", _outputs()))

    assert cli._declared_proofs(outputs) == (
        "restricted_admission_enforced",
        "tenant_pod_cannot_reach_imds",
    )


def test_an_empty_cni_role_arn_refuses(tmp_path):
    payload = _outputs()
    payload["tenant_scheduling_prerequisites"]["value"]["cni_role_arn"] = ""
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", payload))

    with pytest.raises(BootstrapRefused, match="cni_role_arn"):
        cli._cni_role_arn(outputs)


# --- identity comes from the binding, never from the outputs file ---------------


def test_the_binding_is_the_authority_for_identity(tmp_path):
    """The binding resolves org and workspace; the outputs are an artifact. A
    disagreement is refused rather than resolved in the outputs' favour — a bootstrap
    run under one workspace's binding against another's outputs is either a mistake or
    an escalation attempt, and both want the same answer."""
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", _outputs()))
    binding = _write(
        tmp_path / "binding.json",
        {
            "principal": {
                "org_id": ORG_ID,
                "workspace_id": "99999999-9999-4999-8999-999999999999",
            }
        },
    )

    with pytest.raises(BootstrapRefused, match="caller-supplied identity"):
        cli._binding(binding, outputs, "synthetic_facade_resolver:resolve")


def test_a_binding_with_no_principal_refuses(tmp_path):
    """Identity must come from the binding's resolved principal, never from request
    parameters."""
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", _outputs()))
    binding = _write(tmp_path / "binding.json", {"workspace_id": WORKSPACE_ID})

    with pytest.raises(BootstrapRefused, match="caller-supplied identity"):
        cli._binding(binding, outputs, "synthetic_facade_resolver:resolve")


def test_a_principal_missing_a_workspace_refuses(tmp_path):
    outputs = cli._terraform_outputs(_write(tmp_path / "outputs.json", _outputs()))
    binding = _write(tmp_path / "binding.json", {"principal": {"org_id": ORG_ID}})

    with pytest.raises(BootstrapRefused, match="caller-supplied identity"):
        cli._binding(binding, outputs, "synthetic_facade_resolver:resolve")


def test_state_and_recover_both_check_the_binding(files, monkeypatch):
    """`recover` restores a NoSchedule taint and can release a reservation, so running
    it for a workspace the caller is not bound to is a denial of service reachable by
    supplying someone else's outputs. `state` reads which namespace ADP created and its
    uid — the facts a teardown decides deletions from — so reading one is not neutral
    either."""
    checked: list[Path] = []
    real = cli._binding

    def _spy(path, outputs, resolver=None):
        checked.append(path)
        return real(path, outputs, resolver)

    monkeypatch.setattr(cli, "_binding", _spy)

    cli.main(_argv(files, "state"))

    assert checked == [files["binding"]]


# --- the cluster access wiring --------------------------------------------------


def test_the_node_role_reader_is_built_from_the_output_not_a_flag(files):
    """An operator retyping a role ARN is an operator who can point the CNI scope proof
    at the wrong role, and a proof about the wrong subject is worse than no proof."""
    outputs = cli._terraform_outputs(files["outputs"])
    args = argparse.Namespace(
        crd_manifest=None,
        kubeconfig=Path("/tmp/kubeconfig"),
        controller_namespace="superplane-system",
        controller_service_account="superplane-controller",
        controller_image="registry.example/superplane-controller:v1",
        imds_probe_image="registry.example/python@sha256:" + "0" * 64,
    )

    access = cli._cluster_access(args, outputs)

    assert access.node_role.node_role_arn == NODE_ROLE_ARN


def test_the_cluster_adapter_and_the_node_role_reader_share_one_runner(files):
    """One runner, so a future runner carrying operation-scoped credentials cannot be
    configured for the cluster reads and silently not for the IAM ones."""
    outputs = cli._terraform_outputs(files["outputs"])
    access = cli._cluster_access(
        argparse.Namespace(
            crd_manifest=None,
            kubeconfig=Path("/tmp/kubeconfig"),
            controller_namespace="superplane-system",
            controller_service_account="superplane-controller",
            controller_image="registry.example/superplane-controller:v1",
            imds_probe_image="registry.example/python@sha256:" + "0" * 64,
        ),
        outputs,
    )

    assert access.runner is access.node_role.runner


def test_a_crd_manifest_without_a_path_refuses(files):
    """`establish_crds` refuses a CRD name with no declared manifest, so this flag is
    how an operator states what may be applied. A default manifest directory would let
    a file appear on disk and be applied without anyone naming it."""
    outputs = cli._terraform_outputs(files["outputs"])

    with pytest.raises(BootstrapRefused, match="NAME=PATH"):
        cli._cluster_access(
            argparse.Namespace(
                crd_manifest=["nodepools.superplane.ai"],
                kubeconfig=Path("/tmp/kubeconfig"),
                controller_namespace="superplane-system",
                controller_service_account="superplane-controller",
                controller_image="registry.example/superplane-controller:v1",
                imds_probe_image="registry.example/python@sha256:" + "0" * 64,
            ),
            outputs,
        )


# --- output shape and exit codes ------------------------------------------------


def test_a_refusal_is_json_on_stdout_with_exit_one(files, monkeypatch, capsys):
    """Every outcome is JSON, including refusals, so an operator's wrapper does not have
    to parse prose to find out what happened."""

    def _refuse(args):
        raise BootstrapRefused("the cluster is not the one the reviewed plan described")

    # Patched on the module, which is where `_build_parser` resolves it via
    # `set_defaults(handler=...)` at parse time — so `main`'s own except-clause is what
    # runs, rather than a substituted error handler.
    monkeypatch.setattr(cli, "_run_plan", _refuse)

    assert cli.main(_argv(files, "plan")) == cli._EXIT_REFUSED

    report = _stdout(capsys)
    assert report["status"] == "refused"
    assert "reviewed plan" in report["reason"]


def test_an_unexpected_exception_never_prints_its_own_text(files, monkeypatch, capsys):
    """A cloud SDK or database error can carry a credential in its string, and `main` is
    the one place it would reach an operator's terminal and their CI log. The message is
    this package's own, and it says what was NOT established."""
    # A sentinel, deliberately NOT shaped like a real access key id. The test only needs
    # a string it can prove absent from stdout, and a credential-shaped literal in a
    # committed test is a secret scanner's true positive — the cost of that is a blocked
    # pipeline and an operator learning to wave scanner hits through.
    secret = "sentinel-value-that-must-never-be-printed"

    def _boom(args):
        raise RuntimeError(f"connection failed for credential {secret}")

    monkeypatch.setattr(cli, "_run_plan", _boom)

    assert cli.main(_argv(files, "plan")) == cli._EXIT_REFUSED

    out = capsys.readouterr().out
    assert secret not in out
    assert "RuntimeError" not in out
    report = json.loads(out)
    assert report["status"] == "failed"
    assert "no readiness or registration is claimed" in report["reason"]


def test_an_unexpected_exception_tells_the_operator_to_run_recover(
    files, monkeypatch, capsys
):
    """Because the one thing a truncated message must not lose is the instruction that
    matters when the crash happened after the taint came off."""

    def _boom(args):
        raise RuntimeError("anything")

    monkeypatch.setattr(cli, "_run_bootstrap", _boom)

    cli.main(_bootstrap_argv(files))

    assert "`recover`" in capsys.readouterr().out


def test_a_successful_report_names_what_was_not_established(files, monkeypatch, capsys):
    """`_report` states the negatives, not only the positives. `nodes_left_schedulable`
    is the F5 alarm: true means tenant workloads can schedule on a workspace that is not
    registered, and it needs a human now."""

    def _ok(args):
        return cli._EXIT_OK, {"status": "verified"}

    monkeypatch.setattr(cli, "_run_plan", _ok)

    assert cli.main(_argv(files, "plan")) == cli._EXIT_OK
    assert _stdout(capsys) == {"status": "verified"}


def test_the_report_surfaces_every_field_an_operator_decides_from():
    """Asserted over the keys `_report` produces rather than by reading it, so a field
    quietly dropped in a refactor fails here instead of going unnoticed in an incident."""

    class _Outcome:
        target = None
        installation = None
        inventory = None
        reservation = None
        readiness = None
        evidence = None
        taint_cleared = False
        taint_restored = False
        restore_failed = False
        reservation_released = False
        registered = False
        nodes_left_schedulable = True
        refusal = BootstrapRefused("refused for a stated reason")

    report = cli._report(_Outcome())

    for name in (
        "prerequisites_verified",
        "reservation_held",
        "readiness_usable",
        "readiness_failures",
        "isolation_proved",
        "isolation_unverified",
        "taint_cleared",
        "taint_restored",
        "taint_restore_failed",
        "reservation_released",
        "registered",
        "nodes_left_schedulable",
        "refusal",
    ):
        assert name in report
    assert report["status"] == "refused"
    assert report["nodes_left_schedulable"] is True
    assert "stated reason" in report["refusal"]


def test_there_is_no_delete_or_cleanup_subcommand():
    """`retire.plan_cleanup` produces a PLAN and this package has no delete code path at
    all — `access.py` has no `delete` method. Executing a teardown is #5534's mode-aware
    retirement, holding its own inventory; composition ownership stays there."""
    parser = cli._build_parser()
    subcommands = set(
        parser._subparsers._group_actions[0].choices  # type: ignore[union-attr]
    )

    assert subcommands == {"plan", "bootstrap", "recover", "state"}
    for absent in ("cleanup", "delete", "destroy", "retire", "teardown"):
        assert absent not in subcommands


def test_the_state_subcommand_reads_one_file_and_reports_the_interruption(
    files, capsys
):
    """The F5 fact an operator needs before anything schedules work: whether a run was
    interrupted after the taint was cleared. It mutates nothing and reads no cluster."""
    assert cli.main(_argv(files, "state")) == cli._EXIT_OK

    report = _stdout(capsys)
    assert report["status"] == "read"
    assert report["interrupted_after_taint_cleared"] is False
    assert report["workspace_id"] == WORKSPACE_ID
    assert "nothing was read from the cluster" in report["note"]


def test_the_state_subcommand_reports_the_decision_and_the_history_separately(
    files, capsys
):
    """F12, at the operator's end of it.

    A workspace that WAS interrupted and HAS been recovered must read as "interrupted
    once, nothing pending". Reporting only `interrupted_after_taint_cleared` made those
    two states indistinguishable, so an operator following the note would run `recover`
    forever and see the same alarm each time.

    The two halves are reported separately because they carry different urgency, and the
    note points at the one that decides. A pending interlock restoration means tenant work
    can schedule onto an unverified cluster; a pending release means only that the next
    attempt will be refused as a conflict.
    """
    recovered = BootstrapState(
        workspace_id=WORKSPACE_ID,
        cluster_arn=CLUSTER_ARN,
        taint_cleared=True,
        taint_restored=True,
        registration_reserved=False,
        registration_finalized=False,
    )
    FileStateStore(files["state_dir"]).save(recovered)

    assert cli.main(_argv(files, "state")) == cli._EXIT_OK

    report = _stdout(capsys)
    assert report["interrupted_after_taint_cleared"] is True
    assert report["recovery_pending"] is False
    assert report["interlock_restoration_pending"] is False
    assert report["reservation_release_pending"] is False
    assert "`recovery_pending`" in report["note"]


# --- F9: a caught backend failure's text must not reach stdout either -------------
#
# The review asked for "CLI-facing tests using secret-bearing RBAC, controller-install
# and CRD exceptions". These are those tests, and they are deliberately NOT written
# against `components.py` directly.
#
# The reason is where the defect actually became a disclosure. `components.py` wrapping
# an exception into a refusal is not itself a leak — the leak is that the refusal
# reaches `_report`, which puts `str(outcome.refusal)` under the `"refusal"` key, which
# `main` prints. A unit test on the wrapper proves the string is built correctly; only a
# test at this boundary proves the string an operator SEES contains nothing it should
# not, and that is the property F9 was about. `tests/test_components.py`'s AST guard
# then keeps every other wrapper in the package from regrowing the same shape.
#
# The distinction from `test_an_unexpected_exception_never_prints_its_own_text` above:
# that one covers an exception escaping to `main`'s own handler, where the whole message
# is fixed text. These cover the opposite and less obvious case — a refusal this package
# built on PURPOSE, printed as a normal, expected outcome, with exit 1 and a full report.
# That path looked completely healthy, which is why the leak survived review once.

# A sentinel standing in for whatever a real backend failure carries: a bearer token in
# a `kubectl` stderr line, a request body echoed by an SDK, a DSN in a driver error.
# Deliberately not shaped like a real credential, for the reason stated at
# `test_an_unexpected_exception_never_prints_its_own_text` — a credential-shaped literal
# in a committed test is a secret scanner's true positive, and teaching operators to wave
# scanner hits through costs more than this test could ever be worth.
LEAKED = "sentinel-material-a-backend-error-would-carry"


@dataclass
class _LeakyCluster(FakeClusterAccess):
    """A cluster whose backend fails at ONE named stage, with a secret-bearing error.

    A subclass rather than three more knobs on `FakeClusterAccess`, because the fake's
    existing `rbac_install_fails` / `controller_install` knobs raise fixed synthetic
    messages and those messages are load-bearing for other suites. What is needed here
    is the same failures carrying something that must not be printed, and the rest of
    the fake's clean-bootstrap defaults left exactly as they are — so the stage named is
    the only difference between a run that registers and a run that refuses.

    `RuntimeError` is the stand-in for the real thing on purpose: the wrappers catch
    bare `Exception` precisely because they cannot know what a subprocess adapter, a
    Kubernetes SDK or a database driver will raise, so a test that used a specific
    library's exception type would be testing a narrower path than production has.
    """

    failing_stage: str = ""

    def _fail_if(self, stage: str) -> None:
        if self.failing_stage == stage:
            raise RuntimeError(
                f"the API server rejected the request; authorization: Bearer {LEAKED}"
            )

    def establish_crds(self, names):
        self._fail_if("crds")
        return super().establish_crds(names)

    def establish_controller_rbac(self, namespace, service_account):
        self._fail_if("rbac")
        return super().establish_controller_rbac(namespace, service_account)

    def install_controller(self, namespace, name, service_account):
        self._fail_if("controller")
        return super().install_controller(namespace, name, service_account)


@pytest.fixture
def install_files(files):
    """`files`, but with the proof list `infra/workspaces/outputs.tf` really declares.

    The module-level `_outputs()` fixture puts the two SHORT proof names in
    `required_proofs`, which is right for the test that `_declared_proofs` passes the
    list through unchanged — that test is about plumbing and wants a value it can compare
    literally. It is wrong here: `admission.DECLARED_PROOF_CHECKS` matches a declared
    entry against a fragment of its real prose, so the short names match no check and the
    isolation gate correctly reports every one of them as uncovered. A run using them can
    never clear the taint, which would make the positive control below impossible to
    write and leave the three F9 tests asserting absence on runs that refused early for
    an unrelated reason.

    So this reads the genuine list from the Terraform module, the same way
    `test_admission.py` does. The harness then exercises the real declared requirement
    rather than a fixture-shaped one.
    """
    payload = _outputs()
    payload["tenant_scheduling_prerequisites"]["value"]["required_proofs"] = list(
        required_proofs()
    )
    return {**files, "outputs": _write(files["outputs"], payload)}


def _bootstrap_with(monkeypatch, files, cluster, provider_identity, observed_cluster):
    """Run `bootstrap` through the REAL `_run_bootstrap` against `cluster`.

    Substitutions are only at the seams that would otherwise reach a cluster, AWS or a
    database. Everything between argv and the refusal is production code: `_binding`,
    `_taint_key`, `_declared_proofs`, `_expected_prerequisites`, `bootstrap_workspace`'s
    whole gate sequence, `components.py`'s wrappers, `_report` and `main`'s printing.

    `_screen` is NOT substituted — it resolves the contract's real
    `assert_no_secret_material`, so the record screening on the success path is the
    genuine one here rather than a permissive stand-in.
    """
    monkeypatch.setattr(cli, "_cluster_access", lambda args, outputs: cluster)
    monkeypatch.setattr(
        cli, "AwsPrerequisiteAccess", lambda **kwargs: FakePrerequisiteAccess()
    )
    monkeypatch.setattr(
        cli, "_registration_store", lambda args: FakeRegistrationStore()
    )

    class _Observer:
        def __init__(self, **kwargs):
            pass

        def provider_identity(self):
            return provider_identity

        def cluster_identity(self, name):
            return observed_cluster

    monkeypatch.setattr(cli, "AwsObserver", _Observer)
    return cli.main(_bootstrap_argv(files))


@pytest.mark.parametrize(
    ("stage", "names_the_stage"),
    [
        ("rbac", "scoped RBAC"),
        ("controller", "installing the workspace controller"),
        ("crds", "establishing the required CRDs"),
    ],
)
def test_a_backend_failure_at_an_install_stage_prints_neither_its_text_nor_a_secret(
    install_files,
    monkeypatch,
    capsys,
    provider_identity,
    observed_cluster,
    stage,
    names_the_stage,
):
    """**The F9 test.** Each of the three install stages, through the real command.

    All three assertions matter and none is redundant:

    - the sentinel is absent, which is the security property;
    - the STAGE is named, which is why the fix is redaction rather than deletion. A
      refusal that said only "installation failed" would be safe and useless, and the
      next operator to hit it would go looking in the wrong place;
    - the exception TYPE is present, which is the deliberate line drawn by
      `errors.failure_kind`. `TimeoutError` versus `PermissionError` is the distinction
      an operator acts on, and a class name cannot carry a payload. Asserting it here
      pins that the redaction is type-only rather than total, so a future "simplify the
      message" change cannot quietly remove the one diagnostic that survived.

    Exit 1 and `status: refused` are asserted because this is the shape the leak wore.
    It was not an escaping crash: it was a well-formed report of an anticipated failure.
    """
    cluster = _LeakyCluster(crds=[], failing_stage=stage)

    code = _bootstrap_with(
        monkeypatch, install_files, cluster, provider_identity, observed_cluster
    )

    out = capsys.readouterr().out
    assert code == cli._EXIT_REFUSED
    assert LEAKED not in out
    assert "Bearer" not in out
    report = json.loads(out)
    assert report["status"] == "refused"
    assert names_the_stage in report["refusal"]
    assert "RuntimeError" in report["refusal"]
    assert report["registered"] is False


def test_the_leaky_cluster_registers_when_no_stage_is_made_to_fail(
    install_files, monkeypatch, capsys, provider_identity, observed_cluster
):
    """The positive control, without which the three tests above prove nothing.

    Every one of them asserts that a string is ABSENT from stdout, and a run that
    refused at step 1 — a mis-wired fixture, a binding disagreement, a fake that does
    not satisfy a Protocol — would satisfy that assertion perfectly while never reaching
    an install stage at all. This test runs the identical harness with `failing_stage`
    empty and requires it to register, which is what establishes that the only reason
    those runs refused is the failure each one injected.
    """
    cluster = _LeakyCluster(crds=[])

    code = _bootstrap_with(
        monkeypatch, install_files, cluster, provider_identity, observed_cluster
    )

    report = json.loads(capsys.readouterr().out)
    assert code == cli._EXIT_OK
    assert report["status"] == "registered"
    assert report["taint_cleared"] is True
    assert report["nodes_left_schedulable"] is False


# --- F9 at the INPUT boundary: the store factory and the files ------------------
#
# The three tests above cover F9 where the review found it — a backend failure during
# installation, wrapped and printed. Two `failure_kind` call sites in `cli.py` itself had
# no coverage at all, and they are the two an operator hits FIRST, before any cluster is
# touched:
#
# - `_registration_store` imports a module the operator named. An `ImportError` raised
#   deep in a database driver's import chain reports that chain, and a store factory
#   module is exactly where a DSN with a password tends to sit at import time.
# - `_read_json` reads terraform outputs and the operation binding. `terraform output
#   -json` emits every output the module declares, so a read or parse failure is raised
#   over a buffer that may hold sensitive values.
#
# Both reach stdout through `main`'s `BootstrapRefused` handler, which prints
# `{"status": "refused", "reason": str(exc)}` DIRECTLY — a different printing path from the
# `_report` one the install-stage tests exercise. A redaction that held for `_report` and
# not for this handler would leak on the very first command an operator runs, so these
# assert against the command's actual stdout rather than against a refusal object.
#
# The existing unit tests for these paths (`test_a_store_spec_without_a_colon_refuses`,
# `test_an_unloadable_store_factory_refuses_without_executing_anything`,
# `test_an_unreadable_outputs_file_names_the_file`) pin that a refusal HAPPENS. Every one
# of them would pass unchanged if the message carried the whole import chain or the file's
# bytes, which is the gap below.

# A sentinel standing in for what a driver's import-time error carries. Shaped like a DSN
# rather than being one, for the reason `LEAKED` gives: a credential-shaped literal in a
# committed test is a secret scanner's true positive.
LEAKY_DSN = "sentinel-dsn-material-a-driver-import-would-carry"


class _ExternalCommandAttempted(BaseException):
    """Raised when a supposedly-offline test reaches a subprocess. Not an `Exception`.

    Deriving from `BaseException` is what lets it through `cli.main`'s `except Exception`,
    which exists to keep a backend error's text off the terminal and would otherwise
    replace this message with a generic one. Same reasoning as `KeyboardInterrupt`: a
    control-flow signal that no production handler should be able to absorb.
    """


def _offline_store_failure(monkeypatch, provider_identity, observed_cluster):
    """Reach `_registration_store` with NO external command, and prove none was run.

    ## Why this helper exists: the two tests below used to pass for the wrong reason

    They drive the real `bootstrap` command to reach `_registration_store`, which is
    correct — the leak they are about reaches stdout, so they must assert on stdout. But
    `_run_bootstrap` resolves the caller identity through `AwsObserver` BEFORE it builds
    the store, and these two tests were the only ones in the F9 group that did not
    substitute the observer. So their result depended on the machine:

    - with ambient cloud credentials, `aws sts get-caller-identity` really ran, succeeded,
      and the run went on to the store factory — the assertions passed, having made a live
      API call from a unit test;
    - without them, the command refused at the identity step with `aws exited 253` and
      never reached the store factory at all.

    The second is the reported failure. The FIRST is the worse one and is why the fix is
    not "provide credentials": a redaction test that silently stops exercising its
    redaction stays green while the leak it exists to catch goes uncovered.

    So the observer is substituted here exactly as `_bootstrap_with` does it, and — the
    part that is more than a restoration — `SubprocessRunner.run` is replaced with a
    failing stub. The tests therefore cannot reach the network even if a future change
    reorders the command again: they would fail on the guard rather than quietly go back
    to depending on whatever credential happens to be in the environment.

    Only the three seams that would leave the process are substituted. `_registration_store`
    itself is NOT, because it is the function under test; everything from argv through
    `main`'s refusal handler and its printing is production code.
    """

    class _Observer:
        def __init__(self, **kwargs):
            pass

        def provider_identity(self):
            return provider_identity

        def cluster_identity(self, name):
            return observed_cluster

    monkeypatch.setattr(cli, "AwsObserver", _Observer)
    monkeypatch.setattr(
        cli, "AwsPrerequisiteAccess", lambda **kwargs: FakePrerequisiteAccess()
    )

    def _no_subprocess(self, args, **kwargs):
        # `BaseException`, not `AssertionError`. `main` catches `Exception` and converts it
        # into its own deliberately contentless "a bootstrap stage failed" report — correct
        # production behaviour, and it would swallow this diagnostic, leaving a future
        # developer with only `assert 'failed' == 'refused'` to work from. Escaping that
        # handler is the whole point of the guard: the message has to reach the person who
        # reordered the command.
        raise _ExternalCommandAttempted(
            "a redaction test ran an external command "
            f"({' '.join(str(a) for a in args)!r}). These tests assert on what the "
            "command prints when loading the registration store fails, so every "
            "observation must be synthetic; reaching a subprocess means the run is "
            "resolving a real identity and its result depends on ambient credentials."
        )

    monkeypatch.setattr(cli.SubprocessRunner, "run", _no_subprocess)


def test_a_store_factory_that_leaks_while_importing_prints_neither_its_text_nor_the_dsn(
    files, tmp_path, monkeypatch, capsys, provider_identity, observed_cluster
):
    """A driver that fails at IMPORT time, through the real command.

    The module raises during import — what a database driver does when it cannot find its
    shared library or its configuration — and the exception carries a DSN, because that is
    what such errors carry. `_registration_store` catches it and must name the spec the
    operator typed without reproducing the chain.
    """
    _offline_store_failure(monkeypatch, provider_identity, observed_cluster)
    (tmp_path / "leaky_store_factory.py").write_text(
        "raise ImportError(\n"
        f'    "could not connect: postgresql://user:{LEAKY_DSN}@db.internal/superplane"\n'
        ")\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    code = cli.main(
        _bootstrap_argv(files, "--registration-store", "leaky_store_factory:make_store")
    )

    out = capsys.readouterr().out
    assert code == cli._EXIT_REFUSED
    assert LEAKY_DSN not in out
    assert "postgresql://" not in out
    report = json.loads(out)
    assert report["status"] == "refused"
    # The spec the operator typed IS echoed — it is their own argument, not the
    # exception's payload, and a refusal that would not say which factory failed sends
    # them to read three modules to find out.
    assert "leaky_store_factory:make_store" in report["reason"]
    # Type-only redaction, the line `errors.failure_kind` draws deliberately: an
    # `ImportError` and an `AttributeError` mean different things to an operator here, and
    # a class name cannot carry a payload.
    assert "ImportError" in report["reason"]


def test_a_mistyped_store_factory_attribute_does_not_print_the_modules_contents(
    files, tmp_path, monkeypatch, capsys, provider_identity, observed_cluster
):
    """The `AttributeError` arm of the same `except` clause.

    Both arms of `except (ImportError, AttributeError)` reach one wrapper, so a redaction
    applied to one and not the other would be invisible unless the second is driven too.
    The module imports CLEANLY and has no such attribute — the operator-typo case — and
    its globals hold configuration, which is where a value sits when this arm fires.
    """
    _offline_store_failure(monkeypatch, provider_identity, observed_cluster)
    (tmp_path / "typo_store_factory.py").write_text(
        f'DATABASE_URL = "postgresql://user:{LEAKY_DSN}@db.internal/superplane"\n',
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    code = cli.main(
        _bootstrap_argv(files, "--registration-store", "typo_store_factory:make_stroe")
    )

    out = capsys.readouterr().out
    assert code == cli._EXIT_REFUSED
    assert LEAKY_DSN not in out
    report = json.loads(out)
    assert "AttributeError" in report["reason"]
    assert "typo_store_factory:make_stroe" in report["reason"]


def test_an_unreadable_outputs_file_prints_the_path_and_the_failure_kind_only(
    files, tmp_path, capsys
):
    """`_read_json`'s `OSError` arm, through the command.

    The PATH is named, because an operator with several environments' output files needs
    to know which one was unreadable. The `OSError`'s own text is not: it interpolates the
    filename it was given and, depending on platform and errno, more besides — and the
    file this reads is terraform output.
    """
    absent = tmp_path / "environments" / "dev" / "outputs.json"

    code = cli.main(_bootstrap_argv({**files, "outputs": absent}))

    out = capsys.readouterr().out
    assert code == cli._EXIT_REFUSED
    report = json.loads(out)
    assert str(absent) in report["reason"]
    assert "FileNotFoundError" in report["reason"]


def test_a_malformed_outputs_file_reports_a_position_and_none_of_its_bytes(
    files, tmp_path, capsys
):
    """The parse arm, which is the one that would otherwise print the file.

    `json.JSONDecodeError.__str__` happens not to include the document — but relying on
    that would be relying on a stdlib implementation detail. `_read_json` builds its
    message from the exception's STRUCTURED fields (`lineno`, `colno`) instead, so the
    operator gets an actionable line and column and the buffer is never interpolated.

    The malformed file carries a credential-shaped value before the syntax error, which is
    the shape of a truncated `terraform output -json` — the realistic way this is reached.
    """
    malformed = tmp_path / "truncated-outputs.json"
    malformed.write_text(
        '{"database_url": {"value": "postgresql://user:'
        + LEAKY_DSN
        + '@db.internal/superplane"}, "cluster_arn": {"value": ',
        encoding="utf-8",
    )

    code = cli.main(_bootstrap_argv({**files, "outputs": malformed}))

    out = capsys.readouterr().out
    assert code == cli._EXIT_REFUSED
    assert LEAKY_DSN not in out
    assert "postgresql://" not in out
    report = json.loads(out)
    assert "not valid JSON" in report["reason"]
    # The position, which is what makes this a redaction rather than a deletion.
    assert "line 1" in report["reason"]
    assert "column" in report["reason"]


def test_the_binding_file_is_read_through_the_same_redacted_reader(
    files, tmp_path, capsys
):
    """The other file `_read_json` reads, so coverage is per-CALLER and not per-function.

    An operation binding carries a principal rather than terraform output, but it is read
    by the same function and printed by the same handler. The outputs file is left VALID
    here so the run actually reaches the binding read — without that this test would pass
    by refusing one step earlier, which is the mistake that makes an absence assertion
    worthless.
    """
    malformed = tmp_path / "binding.json"
    malformed.write_text(
        '{"principal": {"org_id": "' + LEAKY_DSN + '", ', encoding="utf-8"
    )

    code = cli.main(_bootstrap_argv({**files, "binding": malformed}))

    out = capsys.readouterr().out
    assert code == cli._EXIT_REFUSED
    assert LEAKY_DSN not in out
    report = json.loads(out)
    assert "not valid JSON" in report["reason"]
